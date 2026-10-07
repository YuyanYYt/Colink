"""Private bounded execution metadata and durable, non-restartable receipts.

The journal never stores log bodies or launches/re-adopts recorded PIDs. A new
owner marks unfinished jobs interrupted; that is not proof their old processes
were killed. Ordinary reads preserve expired receipts. Explicit maintenance may
retire expired, verified, unreferenced work; an old plan still cannot launch.
"""

import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from code_context.local_control import private_directory
from code_context.scanner import _identity
from code_context.source_access import SourceError

MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
RECEIPT_SECONDS = 24 * 60 * 60
TERMINAL_STATES = frozenset(
    {"exited", "timeout", "cancelled", "interrupted", "resource_limit", "stop_failed", "failed"}
)
SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE plans (
    plan_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT NOT NULL,
    epoch TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
    created REAL NOT NULL, expires_at REAL NOT NULL, metadata TEXT NOT NULL,
    consumed_by TEXT
);
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL UNIQUE REFERENCES plans(plan_id),
    project_id TEXT NOT NULL, source_id TEXT NOT NULL, epoch TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE, digest TEXT NOT NULL, state TEXT NOT NULL,
    service INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    completed REAL, metadata TEXT NOT NULL, snapshot TEXT NOT NULL
);
CREATE TABLE receipts (
    request_id TEXT PRIMARY KEY, kind TEXT NOT NULL, project_id TEXT NOT NULL,
    source_id TEXT NOT NULL, epoch TEXT NOT NULL, digest TEXT NOT NULL,
    target_id TEXT NOT NULL, created REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX jobs_by_scope ON jobs(project_id,source_id,epoch,created);
"""


class ExecutionStoreError(SourceError):
    """Content-free failures; never authorize work after a journal failure."""


def _text(value, limit=2048):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or "\x00" in value:
        raise ExecutionStoreError("INVALID_EXECUTION_BINDING")
    try:
        if len(value.encode("utf-8")) > limit:
            raise ValueError
    except (ValueError, UnicodeError):
        raise ExecutionStoreError("INVALID_EXECUTION_BINDING") from None
    return value


def _epoch(value):
    if type(value) not in (int, str) or (type(value) is int and value < 0):
        raise ExecutionStoreError("INVALID_EXECUTION_BINDING")
    return _text(str(value))


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ExecutionStoreError("INVALID_EXECUTION_DIGEST")
    return value


def _metadata(value):
    try:
        if not isinstance(value, dict):
            raise ValueError
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ExecutionStoreError("EXECUTION_METADATA_LIMIT") from None


class ExecutionStore:
    def __init__(
        self,
        root: Path,
        *,
        max_metadata_bytes=MAX_METADATA_BYTES,
        receipt_seconds=RECEIPT_SECONDS,
        clock=None,
    ):
        if (
            type(max_metadata_bytes) is not int
            or not 256 * 1024 <= max_metadata_bytes <= MAX_METADATA_BYTES
            or type(receipt_seconds) is not int
            or not 1 <= receipt_seconds <= RECEIPT_SECONDS
        ):
            raise ExecutionStoreError("INVALID_EXECUTION_STORAGE_BUDGET")
        self.directory = private_directory(Path(root))
        self.root = self.directory.root
        self.max_metadata_bytes = max_metadata_bytes
        # DELETE journaling may retain every original database page while a
        # transaction grows or vacuums the database. Keep half the physical
        # budget available, plus room for journal headers/page checksums/lease.
        self.database_budget_bytes = ((max_metadata_bytes - 65536) // 8192) * 4096
        self.receipt_seconds = receipt_seconds
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self.db = None
        self._lease = None
        self._lease_identity = self._db_identity = None
        try:
            with self.directory.root_fd() as parent:
                self._lease = os.open(
                    "execution.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                self._private(os.fstat(self._lease))
                self._lease_identity = _identity(os.fstat(self._lease))
                try:
                    fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise ExecutionStoreError("EXECUTION_STORE_ALREADY_OPEN") from None
                fd = os.open(
                    "execution.sqlite3",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                try:
                    info = os.fstat(fd)
                    self._private(info)
                    self._db_identity = _identity(info)
                    fresh = info.st_size == 0
                finally:
                    os.close(fd)
                self._check_auxiliary(parent)
            self.db = sqlite3.connect(
                self.root / "execution.sqlite3", isolation_level=None, check_same_thread=False
            )
            self.db.row_factory = sqlite3.Row
            if not fresh:
                try:
                    version = self.db.execute(
                        "SELECT value FROM settings WHERE key='schema_version'"
                    ).fetchone()
                except sqlite3.Error:
                    raise ExecutionStoreError("EXECUTION_SCHEMA_INVALID") from None
                if version is None or version[0] != "1":
                    raise ExecutionStoreError("EXECUTION_SCHEMA_INVALID")
            self.db.execute("PRAGMA page_size=4096")
            self.db.execute("PRAGMA auto_vacuum=FULL")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA temp_store=MEMORY")
            self.db.execute("PRAGMA cache_size=-1024")
            # With no cache spills a rollback journal has a single header;
            # <=4096 pages * 8 bytes of page metadata fit the reserved 64KiB.
            self.db.execute("PRAGMA cache_spill=OFF")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute(f"PRAGMA max_page_count={self.database_budget_bytes // 4096}")
            if fresh:
                self.db.executescript("BEGIN IMMEDIATE;" + SCHEMA)
                self.db.execute("INSERT INTO settings VALUES('schema_version','1')")
                self.db.execute("COMMIT")
            if (
                self.db.execute("PRAGMA page_size").fetchone()[0] != 4096
                or self.db.execute("PRAGMA auto_vacuum").fetchone()[0] != 1
                or self.db.execute("PRAGMA integrity_check(1)").fetchone()[0] != "ok"
            ):
                raise ExecutionStoreError("EXECUTION_STORE_CORRUPT")
            self._check()
            self.interrupt_unfinished()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _private(info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ExecutionStoreError("UNSAFE_EXECUTION_STATE")

    def _check_auxiliary(self, parent):
        total = 0
        for name in ("execution.sqlite3", "execution.lock"):
            try:
                total += os.stat(name, dir_fd=parent, follow_symlinks=False).st_size
            except FileNotFoundError:
                continue
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                info = os.stat("execution.sqlite3" + suffix, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            self._private(info)
            if suffix in {"-wal", "-shm"}:
                raise ExecutionStoreError("UNEXPECTED_EXECUTION_JOURNAL")
            total += info.st_size
        if total > self.max_metadata_bytes:
            raise ExecutionStoreError("EXECUTION_METADATA_CAPACITY")

    def _check(self):
        if self.db is None:
            raise ExecutionStoreError("EXECUTION_STORE_CLOSED")
        with self.directory.root_fd() as parent:
            for name, expected in (
                ("execution.lock", self._lease_identity),
                ("execution.sqlite3", self._db_identity),
            ):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                self._private(info)
                if _identity(info) != expected:
                    raise ExecutionStoreError("EXECUTION_STORE_REPLACED")
            self._check_auxiliary(parent)
        if self.db.execute("PRAGMA page_count").fetchone()[0] * 4096 > self.database_budget_bytes:
            raise ExecutionStoreError("EXECUTION_METADATA_CAPACITY")

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                self._check()
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self._check()
                self.db.execute("COMMIT")
            except BaseException as exc:
                if self.db is not None and self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                if isinstance(exc, (sqlite3.Error, OSError)):
                    raise ExecutionStoreError("EXECUTION_STORE_COMMIT_FAILED") from None
                raise

    def _reserve_metadata(self, extra_bytes):
        pages = self.db.execute("PRAGMA page_count").fetchone()[0] * 4096
        unfinished = self.db.execute(
            "SELECT count(*) FROM jobs WHERE state IN ('queued','running')"
        ).fetchone()[0]
        # Reserve each outstanding job's final snapshot and metadata, plus page
        # overhead, before launch. No automatic deletion to obtain headroom.
        if (
            pages + unfinished * (2 * MAX_RECORD_BYTES + 16 * 1024) + extra_bytes
            > self.database_budget_bytes
        ):
            raise ExecutionStoreError("EXECUTION_METADATA_CAPACITY")

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        for name in ("metadata", "snapshot"):
            if name in value:
                value[name] = json.loads(value[name])
        if "service" in value:
            value["service"] = bool(value["service"])
        return value

    def _receipt(self, project_id, source_id, epoch, request_id, digest, kind=None):
        scope = (
            _text(project_id),
            _text(source_id),
            _epoch(epoch),
            _text(request_id),
            _digest(digest),
        )
        row = self.db.execute("SELECT * FROM receipts WHERE request_id=?", (scope[3],)).fetchone()
        if row is None:
            return None
        if (
            (row["project_id"], row["source_id"], row["epoch"], row["digest"])
            != (scope[0], scope[1], scope[2], scope[4])
            or kind is not None
            and row["kind"] != kind
        ):
            raise ExecutionStoreError("EXECUTION_REQUEST_CONFLICT")
        result = dict(row)
        result["expired"] = self._clock() >= row["expires_at"]
        return result

    def get_receipt(self, project_id, source_id, epoch, request_id, digest):
        with self._lock:
            self._check()
            return self._receipt(project_id, source_id, epoch, request_id, digest)

    def _insert_receipt(self, db, kind, scope, target_id, now):
        db.execute(
            "INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                scope[3],
                kind,
                scope[0],
                scope[1],
                scope[2],
                scope[4],
                target_id,
                now,
                now + self.receipt_seconds,
            ),
        )

    def create_plan(
        self,
        plan_id,
        project_id,
        source_id,
        epoch,
        request_id,
        digest,
        metadata=None,
        expires_at=None,
    ):
        plan_id = _text(plan_id)
        raw = _metadata({} if metadata is None else metadata)
        scope = (
            _text(project_id),
            _text(source_id),
            _epoch(epoch),
            _text(request_id),
            _digest(digest),
        )
        now = self._clock()
        expires_at = now + 900 if expires_at is None else expires_at
        if type(expires_at) not in (int, float) or not now < expires_at <= now + RECEIPT_SECONDS:
            raise ExecutionStoreError("INVALID_EXECUTION_EXPIRY")
        with self._transaction() as db:
            receipt = self._receipt(*scope, kind="plan")
            if receipt:
                if receipt["expired"]:
                    raise ExecutionStoreError("EXECUTION_REQUEST_EXPIRED")
                plan = self._row(
                    db.execute(
                        "SELECT * FROM plans WHERE plan_id=?", (receipt["target_id"],)
                    ).fetchone()
                )
                plan["expired"] = now >= plan["expires_at"]
                return plan
            self._reserve_metadata(2 * len(raw.encode()) + 32 * 1024)
            db.execute(
                "INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (plan_id, *scope, now, expires_at, raw),
            )
            self._insert_receipt(db, "plan", scope, plan_id, now)
        return self.get_plan(plan_id)

    def get_plan(self, plan_id):
        with self._lock:
            self._check()
            row = self._row(
                self.db.execute("SELECT * FROM plans WHERE plan_id=?", (_text(plan_id),)).fetchone()
            )
            if row is not None:
                row["expired"] = self._clock() >= row["expires_at"]
            return row

    def reserve_job(
        self,
        job_id,
        plan_id,
        project_id,
        source_id,
        epoch,
        request_id,
        digest,
        metadata=None,
        service=False,
    ):
        job_id, plan_id = _text(job_id), _text(plan_id)
        if type(service) is not bool:
            raise ExecutionStoreError("INVALID_EXECUTION_JOB")
        raw = _metadata({} if metadata is None else metadata)
        scope = (
            _text(project_id),
            _text(source_id),
            _epoch(epoch),
            _text(request_id),
            _digest(digest),
        )
        now = self._clock()
        with self._transaction() as db:
            receipt = self._receipt(*scope, kind="job")
            if receipt:
                if receipt["expired"]:
                    raise ExecutionStoreError("EXECUTION_REQUEST_EXPIRED")
                return self._row(
                    db.execute(
                        "SELECT * FROM jobs WHERE job_id=?", (receipt["target_id"],)
                    ).fetchone()
                ), True
            plan = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if (
                plan is None
                or (plan["project_id"], plan["source_id"], plan["epoch"]) != scope[:3]
                or plan["expires_at"] <= now
                or plan["consumed_by"] is not None
            ):
                raise ExecutionStoreError("EXECUTION_PLAN_UNAVAILABLE")
            self._reserve_metadata(2 * MAX_RECORD_BYTES + len(raw.encode()) + 32 * 1024)
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, plan_id, *scope, "queued", int(service), now, now, None, raw, "{}"),
            )
            db.execute("UPDATE plans SET consumed_by=? WHERE plan_id=?", (job_id, plan_id))
            self._insert_receipt(db, "job", scope, job_id, now)
        return self.get_job(job_id), False

    def get_job(self, job_id):
        with self._lock:
            self._check()
            return self._row(
                self.db.execute("SELECT * FROM jobs WHERE job_id=?", (_text(job_id),)).fetchone()
            )

    def record_job_receipt(self, job_id, project_id, source_id, epoch, request_id, digest):
        """Bind another idempotent request to existing work without restarting it."""
        job_id = _text(job_id)
        scope = (
            _text(project_id),
            _text(source_id),
            _epoch(epoch),
            _text(request_id),
            _digest(digest),
        )
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise ExecutionStoreError("EXECUTION_JOB_UNKNOWN")
            if (job["project_id"], job["source_id"], job["epoch"]) != scope[:3]:
                raise ExecutionStoreError("EXECUTION_REQUEST_CONFLICT")
            receipt = self._receipt(*scope, kind="job")
            if receipt is not None:
                if receipt["expired"]:
                    raise ExecutionStoreError("EXECUTION_REQUEST_EXPIRED")
                if receipt["target_id"] != job_id:
                    raise ExecutionStoreError("EXECUTION_REQUEST_CONFLICT")
                return receipt
            self._reserve_metadata(16 * 1024)
            self._insert_receipt(db, "job", scope, job_id, self._clock())
            return self._receipt(*scope, kind="job")

    def update_completed_snapshot(self, job_id, snapshot):
        """Record bounded follow-up/writeback data without changing job outcome."""
        job_id = _text(job_id)
        raw = _metadata(snapshot)
        with self._transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise ExecutionStoreError("EXECUTION_JOB_UNKNOWN")
            if job["state"] not in TERMINAL_STATES:
                raise ExecutionStoreError("EXECUTION_JOB_NOT_COMPLETED")
            if (
                snapshot.get("state", job["state"]) != job["state"]
                or snapshot.get("job_id", job_id) != job_id
            ):
                raise ExecutionStoreError("INVALID_EXECUTION_TERMINAL_STATE")
            growth = max(0, len(raw.encode()) - len(job["snapshot"].encode()))
            self._reserve_metadata(growth + 16 * 1024)
            db.execute(
                "UPDATE jobs SET snapshot=?,updated=? WHERE job_id=?",
                (raw, self._clock(), job_id),
            )
        return self.get_job(job_id)

    def list_jobs(
        self,
        project_id=None,
        source_id=None,
        epoch=None,
        limit=100,
        *,
        active_first=False,
        completed_since=None,
    ):
        if (
            type(limit) is not int
            or not 1 <= limit <= 1000
            or type(active_first) is not bool
            or completed_since is not None
            and (type(completed_since) not in (int, float) or not math.isfinite(completed_since))
        ):
            raise ExecutionStoreError("INVALID_EXECUTION_JOB_LIMIT")
        filters, parameters = [], []
        for name, value in (("project_id", project_id), ("source_id", source_id), ("epoch", epoch)):
            if value is not None:
                filters.append(name + "=?")
                parameters.append(_epoch(value) if name == "epoch" else _text(value))
        if completed_since is not None:
            # Active and unverified jobs remain discoverable regardless of age.
            filters.append(
                "(completed IS NULL OR completed>=? OR state='stop_failed' "
                "OR json_extract(snapshot,'$.cleanup_verified') IS NOT 1 "
                "OR json_extract(snapshot,'$.writeback.state') IN ('awaiting_start','applying'))"
            )
            parameters.append(completed_since)
        clause = " WHERE " + " AND ".join(filters) if filters else ""
        priority = (
            "CASE WHEN completed IS NULL THEN 0 WHEN state='stop_failed' "
            "OR json_extract(snapshot,'$.cleanup_verified') IS NOT 1 THEN 1 ELSE 2 END,"
            if active_first
            else ""
        )
        with self._lock:
            self._check()
            return [
                self._row(row)
                for row in self.db.execute(
                    "SELECT * FROM jobs"
                    + clause
                    + " ORDER BY "
                    + priority
                    + "created DESC,job_id LIMIT ?",
                    (*parameters, limit),
                )
            ]

    def now(self):
        return self._clock()

    def expired_job_candidates(self, *, limit=100):
        """Bounded identifiers for the coordinator to check for active references."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ExecutionStoreError("INVALID_EXECUTION_JOB_LIMIT")
        with self._lock:
            self._check()
            return [
                row[0]
                for row in self.db.execute(
                    "SELECT job_id FROM jobs WHERE completed<=? AND updated<=? "
                    "AND state!='stop_failed' "
                    "AND json_extract(snapshot,'$.cleanup_verified')=1 "
                    "AND json_extract(snapshot,'$.workspace_retired')=1 "
                    "AND COALESCE(json_extract(snapshot,'$.writeback.state'),'') "
                    "NOT IN ('awaiting_start','applying') ORDER BY completed,job_id LIMIT ?",
                    (
                        self._clock() - self.receipt_seconds,
                        self._clock() - self.receipt_seconds,
                        limit,
                    ),
                )
            ]

    def collect_expired(self, reclaimable_job_ids=(), *, limit=100):
        """Explicit GC after the caller proves there are no workspace/cache refs.

        Receipt reads alone never run GC. After deletion a request identifier
        may bind to a fresh explicit plan; a request for the old plan is unknown.
        Unknown, unfinished, unverified or pending-writeback jobs are preserved.
        """
        if (
            not isinstance(reclaimable_job_ids, (list, tuple, set, frozenset))
            or len(reclaimable_job_ids) > 1000
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ExecutionStoreError("INVALID_EXECUTION_GC_REQUEST")
        permitted = {_text(job_id) for job_id in reclaimable_job_ids}
        now = self._clock()
        cutoff = now - self.receipt_seconds
        reclaimed, plans, receipts = [], 0, 0
        with self._transaction() as db:
            # Read one bounded batch. The durable proof is rechecked inside the
            # same transaction that deletes all associated records.
            for job_id in sorted(permitted)[:limit]:
                job = self._row(
                    db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                )
                if (
                    not job
                    or job["state"] not in TERMINAL_STATES - {"stop_failed"}
                    or job["completed"] is None
                    or max(job["completed"], job["updated"]) > cutoff
                    or job["snapshot"].get("cleanup_verified") is not True
                    or job["snapshot"].get("workspace_retired") is not True
                    or job["snapshot"].get("writeback", {}).get("state")
                    in {"awaiting_start", "applying"}
                ):
                    continue
                plan = db.execute(
                    "SELECT * FROM plans WHERE plan_id=?", (job["plan_id"],)
                ).fetchone()
                if (
                    plan is None
                    or plan["created"] > cutoff
                    or plan["expires_at"] > now
                    or db.execute(
                        "SELECT 1 FROM receipts WHERE target_id IN (?,?) AND expires_at>? LIMIT 1",
                        (job_id, job["plan_id"], now),
                    ).fetchone()
                ):
                    continue
                receipts += db.execute(
                    "DELETE FROM receipts WHERE target_id IN (?,?)", (job_id, job["plan_id"])
                ).rowcount
                db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
                db.execute("DELETE FROM plans WHERE plan_id=?", (job["plan_id"],))
                reclaimed.append(job_id)
                plans += 1
            unused = db.execute(
                "SELECT plan_id FROM plans WHERE consumed_by IS NULL AND created<=? "
                "AND expires_at<=? AND NOT EXISTS (SELECT 1 FROM receipts "
                "WHERE target_id=plans.plan_id AND expires_at>?) ORDER BY created,plan_id LIMIT ?",
                (cutoff, now, now, max(0, limit - plans)),
            ).fetchall()
            for row in unused:
                receipts += db.execute("DELETE FROM receipts WHERE target_id=?", (row[0],)).rowcount
                plans += db.execute("DELETE FROM plans WHERE plan_id=?", (row[0],)).rowcount
        return {"jobs": len(reclaimed), "plans": plans, "receipts": receipts, "job_ids": reclaimed}

    def mark_started(self, job_id, snapshot=None):
        raw = _metadata({} if snapshot is None else snapshot)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (_text(job_id),)).fetchone()
            if row is None or row["state"] not in {"queued", "running"}:
                raise ExecutionStoreError("EXECUTION_JOB_NOT_STARTABLE")
            db.execute(
                "UPDATE jobs SET state='running',updated=?,snapshot=? WHERE job_id=?",
                (self._clock(), raw, job_id),
            )
        return self.get_job(job_id)

    def complete_job(self, job_id, snapshot=None, metadata=None, state=None):
        """Persist the final snapshot and optionally replace bounded metadata."""
        snapshot = {} if snapshot is None else snapshot
        raw = _metadata(snapshot)
        state = state or snapshot.get("state")
        if state not in TERMINAL_STATES:
            raise ExecutionStoreError("INVALID_EXECUTION_TERMINAL_STATE")
        metadata_raw = _metadata(metadata) if metadata is not None else None
        now = self._clock()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (_text(job_id),)).fetchone()
            if row is None:
                raise ExecutionStoreError("EXECUTION_JOB_UNKNOWN")
            if row["state"] in TERMINAL_STATES:
                return self._row(row)
            db.execute(
                "UPDATE jobs SET state=?,updated=?,completed=?,snapshot=?,metadata=? "
                "WHERE job_id=?",
                (
                    state,
                    now,
                    now,
                    raw,
                    metadata_raw if metadata_raw is not None else row["metadata"],
                    job_id,
                ),
            )
        return self.get_job(job_id)

    def interrupt_unfinished(self):
        now = self._clock()
        with self._transaction() as db:
            rows = db.execute(
                "SELECT job_id,snapshot FROM jobs WHERE state IN ('queued','running')"
            ).fetchall()
            for row in rows:
                snapshot = json.loads(row["snapshot"])
                snapshot.update(
                    state="interrupted", reason="runtime_restarted", cleanup_verified=False
                )
                db.execute(
                    "UPDATE jobs SET state='interrupted',updated=?,completed=?,snapshot=? "
                    "WHERE job_id=?",
                    (now, now, _metadata(snapshot), row["job_id"]),
                )
        return len(rows)

    def usage(self):
        with self._lock:
            self._check()
            return {
                "database_bytes": self.db.execute("PRAGMA page_count").fetchone()[0] * 4096,
                "max_metadata_bytes": self.max_metadata_bytes,
                "database_budget_bytes": self.database_budget_bytes,
                "receipts": self.db.execute("SELECT count(*) FROM receipts").fetchone()[0],
                "jobs": self.db.execute("SELECT count(*) FROM jobs").fetchone()[0],
            }

    def close(self):
        with self._lock:
            if self.db is not None:
                self.db.close()
                self.db = None
            if self._lease is not None:
                os.close(self._lease)
                self._lease = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

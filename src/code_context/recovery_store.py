"""Private bounded recovery objects and durable metadata, not a source mirror.

Only the future write coordinator will own source edits. This component never
touches a source project and never deletes unknown files or referenced objects.
Objects are registered before creation and verified before being marked ready.
DELETE journaling, FULL auto-vacuum and physical allocation accounting prevent
an ever-growing WAL or content copy for every successful operation.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from code_context.local_control import private_directory
from code_context.policy import MAX_FILE_BYTES
from code_context.scanner import _identity, _version
from code_context.source_access import SourceError

MIB = 1024 * 1024
OBJECT_NAME = re.compile(r"[a-f0-9]{64}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL, source_id TEXT NOT NULL,
    state TEXT NOT NULL, created REAL NOT NULL, completed REAL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifest (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL, sha256 TEXT NOT NULL, version TEXT NOT NULL,
    PRIMARY KEY (task_id, path)
);
CREATE TABLE IF NOT EXISTS baseline_directories (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL, version TEXT NOT NULL,
    PRIMARY KEY (task_id, path)
);
-- Additive schema-1 extension: old databases and pending writes stay intact.
CREATE TABLE IF NOT EXISTS move_baselines (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL, binding TEXT NOT NULL,
    PRIMARY KEY (task_id, path)
);
CREATE TABLE IF NOT EXISTS move_mappings (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL, origin_path TEXT, kind TEXT NOT NULL, binding TEXT NOT NULL,
    PRIMARY KEY (task_id, path)
);
CREATE TABLE IF NOT EXISTS move_absences (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL, parent_identity TEXT NOT NULL,
    PRIMARY KEY (task_id, path)
);
CREATE TABLE IF NOT EXISTS files (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL, kind TEXT NOT NULL, origin_hash TEXT, origin_mode INTEGER,
    last_hash TEXT, last_version TEXT, directory_identity TEXT,
    PRIMARY KEY (task_id, path)
);
CREATE TABLE IF NOT EXISTS file_attributes (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL, origin_record TEXT, last_record TEXT NOT NULL,
    PRIMARY KEY (task_id, path)
);
CREATE TABLE IF NOT EXISTS operations (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
    metadata TEXT NOT NULL, result TEXT, sequence INTEGER NOT NULL,
    PRIMARY KEY (task_id, request_id), UNIQUE(task_id, sequence)
);
CREATE TABLE IF NOT EXISTS operation_attributes (
    task_id TEXT NOT NULL, request_id TEXT NOT NULL,
    before_record TEXT, after_record TEXT NOT NULL,
    PRIMARY KEY (task_id, request_id),
    FOREIGN KEY (task_id, request_id) REFERENCES operations(task_id, request_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS objects (
    sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, state TEXT NOT NULL, identity TEXT
);
CREATE TABLE IF NOT EXISTS object_refs (
    owner TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES objects(sha256)
);
CREATE INDEX IF NOT EXISTS refs_by_hash ON object_refs(sha256);
"""


class RecoveryError(SourceError):
    """Content-free storage failures; source must remain unchanged."""


def encode_metadata(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 256 * 1024:
            raise ValueError
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RecoveryError("RECOVERY_METADATA_LIMIT: use bounded valid metadata") from None


class RecoveryStore:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes=64 * MIB,
        max_peak_bytes=128 * MIB,
        max_metadata_bytes=16 * MIB,
        min_free_bytes=16 * MIB,
    ):
        if (
            any(
                type(n) is not int
                for n in (max_bytes, max_peak_bytes, max_metadata_bytes, min_free_bytes)
            )
            or max_bytes < 256 * 1024
            or max_peak_bytes < max_bytes
            or not 128 * 1024 <= max_metadata_bytes <= max_bytes // 2
            or min_free_bytes < 0
        ):
            raise RecoveryError("INVALID_RECOVERY_BUDGET: invalid storage limits")
        self.directory = private_directory(root)
        self.root = self.directory.root
        self.objects = private_directory(self.root / "objects")
        self.max_bytes = max_bytes
        self.max_peak_bytes = max_peak_bytes
        self.max_metadata_bytes = max_metadata_bytes
        self.min_free_bytes = min_free_bytes
        self.lock = threading.RLock()
        self.lease = None
        self.lease_identity = None
        self.db = None
        self.db_identity = None
        try:
            with self.directory.root_fd() as parent:
                fd = os.open(
                    "recovery.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                self.lease = fd
                self._private(os.fstat(fd))
                self.lease_identity = _identity(os.fstat(fd))
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise RecoveryError(
                        "RECOVERY_ALREADY_OPEN: stop the current coordinator"
                    ) from None
                fd = os.open(
                    "recovery.sqlite3", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                try:
                    info = os.fstat(fd)
                    self._private(info)
                    self.db_identity = _identity(info)
                    new_database = info.st_size == 0
                finally:
                    os.close(fd)
                for suffix in ("-journal", "-wal", "-shm"):
                    try:
                        info = os.stat(
                            "recovery.sqlite3" + suffix, dir_fd=parent, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue
                    self._private(info)
            self.db = sqlite3.connect(
                self.root / "recovery.sqlite3", isolation_level=None, check_same_thread=False
            )
            self.db.row_factory = sqlite3.Row
            if not new_database:
                try:
                    schema = self.db.execute(
                        "SELECT value FROM settings WHERE key='schema_version'"
                    ).fetchone()
                except sqlite3.Error:
                    raise RecoveryError(
                        "RECOVERY_SCHEMA_INVALID: preserve unknown database"
                    ) from None
                if schema is None or schema[0] != "1":
                    raise RecoveryError("RECOVERY_SCHEMA_INVALID: preserve unknown database")
            self.db.execute("PRAGMA page_size=4096")
            self.db.execute("PRAGMA auto_vacuum=FULL")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA temp_store=MEMORY")
            self.db.execute("PRAGMA cache_size=-1024")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute(f"PRAGMA max_page_count={max_metadata_bytes // 4096}")
            self.db.executescript(SCHEMA)
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('schema_version','1')")
            if self.db.execute("PRAGMA auto_vacuum").fetchone()[0] != 1:
                raise RecoveryError("RECOVERY_SCHEMA_INVALID: required storage layout missing")
            if self.db.execute("PRAGMA integrity_check(1)").fetchone()[0] != "ok":
                raise RecoveryError("RECOVERY_CORRUPT: preserve recovery materials")
            self.reserve()
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
            raise RecoveryError("UNSAFE_RECOVERY_FILE: private owned regular file required")

    def _check(self):
        if self.db is None:
            raise RecoveryError("RECOVERY_CLOSED: reopen locally")
        with self.directory.root_fd() as parent:
            lease = os.stat("recovery.lock", dir_fd=parent, follow_symlinks=False)
            self._private(lease)
            if _identity(lease) != self.lease_identity:
                raise RecoveryError("RECOVERY_LEASE_REPLACED: preserve materials and restart")
            info = os.stat("recovery.sqlite3", dir_fd=parent, follow_symlinks=False)
            self._private(info)
            if _identity(info) != self.db_identity:
                raise RecoveryError("RECOVERY_REPLACED: preserve materials and restart")
        self.objects.ensure_available()

    def usage(self):
        """Count allocated blocks, logical database pages and all real objects.

        Unknown entries cause a refusal, never automatic adoption/deletion.
        Existing owned SQLite journals are counted in peak, not resident bytes.
        """
        with self.lock:
            self._check()
            main = ancillary = objects = 0
            with self.directory.root_fd() as parent:
                entries = os.listdir(parent)
                if len(entries) > 8:
                    raise RecoveryError("UNKNOWN_RECOVERY_FILE: inspect private storage")
                for name in entries:
                    if name == "objects":
                        continue
                    if name not in {
                        "recovery.lock",
                        "recovery.sqlite3",
                        "recovery.sqlite3-journal",
                        "recovery.sqlite3-wal",
                        "recovery.sqlite3-shm",
                    }:
                        raise RecoveryError("UNKNOWN_RECOVERY_FILE: inspect private storage")
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    self._private(info)
                    size = max(info.st_size, info.st_blocks * 512)
                    if name.endswith(("-journal", "-wal", "-shm")):
                        ancillary += size
                    else:
                        main += size
            rows = {row["sha256"]: row for row in self.db.execute("SELECT * FROM objects")}
            if len(rows) > 4096:
                raise RecoveryError("RECOVERY_OBJECT_LIMIT: storage object limit reached")
            with self.objects.root_fd() as parent:
                entries = os.listdir(parent)
                if len(entries) > 4096:
                    raise RecoveryError("RECOVERY_OBJECT_LIMIT: storage object limit reached")
                for name in entries:
                    if name not in rows:
                        raise RecoveryError("UNKNOWN_RECOVERY_OBJECT: preserve unregistered files")
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    self._private(info)
                    expected = rows[name]["identity"]
                    if expected and tuple(json.loads(expected)) != _identity(info):
                        raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve recovery materials")
                    objects += max(info.st_size, info.st_blocks * 512)
            page_bytes = self.db.execute("PRAGMA page_count").fetchone()[0] * 4096
            resident = max(main, page_bytes) + objects
            return {
                "resident_bytes": resident,
                "peak_bytes": resident + ancillary,
                "database_page_bytes": page_bytes,
                "object_bytes": objects,
                "sqlite_auxiliary_bytes": ancillary,
                "object_count": len(rows),
                "max_bytes": self.max_bytes,
                "max_peak_bytes": self.max_peak_bytes,
            }

    def reserve(self, *, object_bytes=0, source_temp_bytes=0, metadata_bytes=0):
        values = (object_bytes, source_temp_bytes, metadata_bytes)
        if any(type(v) is not int or v < 0 for v in values):
            raise RecoveryError("INVALID_RECOVERY_RESERVATION: nonnegative byte counts required")
        usage = self.usage()
        if usage["database_page_bytes"] + metadata_bytes > self.max_metadata_bytes:
            raise RecoveryError(
                "RECOVERY_METADATA_CAPACITY: preserve headroom for completion and rollback"
            )
        # Reserve a full before-image plus SQLite's journal-growth headroom.
        resident = usage["resident_bytes"] + object_bytes + metadata_bytes
        peak = max(
            usage["peak_bytes"] + object_bytes + source_temp_bytes + 3 * metadata_bytes,
            resident + 2 * self.max_metadata_bytes + source_temp_bytes,
        )
        if resident > self.max_bytes or peak > self.max_peak_bytes:
            raise RecoveryError("RECOVERY_CAPACITY: source unchanged; protected materials retained")
        if (
            shutil.disk_usage(self.root).free
            < self.min_free_bytes + object_bytes + source_temp_bytes + 3 * metadata_bytes
        ):
            raise RecoveryError("RECOVERY_DISK_SPACE: source unchanged; free space required")
        return usage

    @contextmanager
    def transaction(self):
        with self.lock:
            self._check()
            self.reserve()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.reserve()
                self.db.execute("COMMIT")
            except BaseException as exc:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                if isinstance(exc, (sqlite3.Error, OSError)):
                    raise RecoveryError(
                        "RECOVERY_COMMIT_FAILED: preserve materials; source must not be changed"
                    ) from None
                raise

    def query(self, sql, parameters=()):
        with self.lock:
            self._check()
            return [dict(row) for row in self.db.execute(sql, parameters)]

    def put_blob(self, raw: bytes, owner: str):
        if (
            not isinstance(raw, bytes)
            or len(raw) > MAX_FILE_BYTES
            or not isinstance(owner, str)
            or not 1 <= len(owner) <= 2048
        ):
            raise RecoveryError("INVALID_RECOVERY_OBJECT: bounded content and owner required")
        sha = hashlib.sha256(raw).hexdigest()
        with self.lock:
            previous = self.query("SELECT * FROM objects WHERE sha256=?", (sha,))
            if previous:
                self.read_blob(sha)
                with self.transaction() as db:
                    db.execute("INSERT OR REPLACE INTO object_refs VALUES(?,?)", (owner, sha))
                return sha
            self.reserve(object_bytes=((len(raw) + 4095) // 4096) * 4096, metadata_bytes=16384)
            with self.transaction() as db:
                if db.execute("SELECT count(*) FROM objects").fetchone()[0] >= 4096:
                    raise RecoveryError("RECOVERY_OBJECT_LIMIT: storage object limit reached")
                db.execute("INSERT INTO objects VALUES(?,?,?,NULL)", (sha, len(raw), "pending"))
                db.execute("INSERT OR REPLACE INTO object_refs VALUES(?,?)", (owner, sha))
            try:
                with self.objects.root_fd() as parent:
                    fd = os.open(
                        sha,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    try:
                        identity = encode_metadata(_identity(os.fstat(fd)))
                        with self.transaction() as db:
                            db.execute(
                                "UPDATE objects SET identity=? WHERE sha256=?", (identity, sha)
                            )
                        with os.fdopen(fd, "w+b", closefd=False) as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(fd)
                            stream.seek(0)
                            if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                                raise RecoveryError(
                                    "RECOVERY_VERIFY_FAILED: source must remain unchanged"
                                )
                        os.fsync(parent)
                    finally:
                        os.close(fd)
                with self.transaction() as db:
                    db.execute("UPDATE objects SET state='ready' WHERE sha256=?", (sha,))
                self.read_blob(sha)
                return sha
            except OSError:
                raise RecoveryError(
                    "RECOVERY_SAVE_FAILED: source unchanged; incomplete object retained"
                ) from None

    def read_blob(self, sha, *, _recovering=False):
        if not isinstance(sha, str) or OBJECT_NAME.fullmatch(sha) is None:
            raise RecoveryError("INVALID_RECOVERY_OBJECT: invalid object identifier")
        with self.lock:
            self._check()
            rows = self.query("SELECT * FROM objects WHERE sha256=?", (sha,))
            states = {"ready", "pending", "retiring"} if _recovering else {"ready"}
            if not rows or rows[0]["state"] not in states:
                raise RecoveryError("RECOVERY_NOT_READY: preserve materials and recover locally")
            if not rows[0]["identity"]:
                raise RecoveryError("RECOVERY_IDENTITY_UNAVAILABLE: preserve unconfirmed object")
            try:
                with self.objects.root_fd() as parent:
                    fd = os.open(sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    with os.fdopen(fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        self._private(info)
                        if (
                            tuple(json.loads(rows[0]["identity"])) != _identity(info)
                            or info.st_size > MAX_FILE_BYTES
                        ):
                            raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
                        raw = stream.read(MAX_FILE_BYTES + 1)
                        after = os.fstat(stream.fileno())
                        actual = os.stat(sha, dir_fd=parent, follow_symlinks=False)
                        self._private(actual)
                        if (
                            _version(info) != _version(after)
                            or _version(actual) != _version(info)
                            or len(raw) != rows[0]["size"]
                            or hashlib.sha256(raw).hexdigest() != sha
                        ):
                            raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
                return raw
            except OSError:
                raise RecoveryError("RECOVERY_OBJECT_UNAVAILABLE: preserve materials") from None

    def release_owner(self, owner: str):
        """Coordinator calls this only after durable commit/retirement, never by MCP."""
        with self.transaction() as db:
            db.execute("DELETE FROM object_refs WHERE owner=?", (owner,))

    def collect_unreferenced(self):
        """Remove only registered objects after their final reference is released."""
        with self.lock:
            self._check()
            rows = self.query(
                "SELECT * FROM objects WHERE sha256 NOT IN (SELECT sha256 FROM object_refs)"
            )
            removed = 0
            for row in rows:
                with self.objects.root_fd() as parent:
                    try:
                        info = os.stat(row["sha256"], dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        if row["state"] not in {"pending", "retiring"}:
                            raise RecoveryError(
                                "RECOVERY_OBJECT_UNAVAILABLE: preserve materials"
                            ) from None
                    else:
                        self._private(info)
                        if not row["identity"] or tuple(json.loads(row["identity"])) != _identity(
                            info
                        ):
                            raise RecoveryError(
                                "RECOVERY_OBJECT_CHANGED: object cannot be reclaimed"
                            )
                        self.read_blob(row["sha256"], _recovering=True)
                        with self.transaction() as db:
                            db.execute(
                                "UPDATE objects SET state='retiring' WHERE sha256=?",
                                (row["sha256"],),
                            )
                        if _version(
                            os.stat(row["sha256"], dir_fd=parent, follow_symlinks=False)
                        ) != _version(info):
                            raise RecoveryError(
                                "RECOVERY_OBJECT_CHANGED: object cannot be reclaimed"
                            )
                        os.unlink(row["sha256"], dir_fd=parent)
                        os.fsync(parent)
                with self.transaction() as db:
                    db.execute("DELETE FROM objects WHERE sha256=?", (row["sha256"],))
                removed += 1
            return removed

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None
            if self.lease is not None:
                os.close(self.lease)
                self.lease = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

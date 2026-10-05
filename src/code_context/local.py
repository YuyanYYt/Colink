"""A local-only source producer; MCP queries never scan or modify the source."""

import fcntl
import json
import os
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from code_context.client import LocalState, SyncError, prepare_batch, watch_source
from code_context.models import validate_project
from code_context.scanner import Scanner, ScanResult
from code_context.storage import MirrorError, MirrorStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


def read_local_mirror_status(data_dir: Path) -> dict:
    """Inspect persisted state and the live writer lock, without creating either."""
    data = data_dir.expanduser().resolve()
    database = data / "local.sqlite3"
    result = {"initialized": database.exists(), "running": False, "data_dir": str(data)}
    lock_path = data / "local.lock"
    try:
        with lock_path.open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result["running"] = True
    except FileNotFoundError:
        pass
    if not database.exists():
        result.update(
            initialized=False, status="initializing" if result["running"] else "not_running"
        )
        return result
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta = dict(db.execute("SELECT key, value FROM meta")) if "meta" in tables else {}
        if not {"meta", "files", "outbox"} <= tables or not {"identity", "revision"} <= meta.keys():
            # Startup creates the SQLite file before committing its schema/identity.
            # Do not create/repair it from a status query or hide an offline partial DB.
            result.update(
                initialized=False,
                status="initializing" if result["running"] else "incomplete_state",
            )
            return result
        result.update(json.loads(meta["identity"]))
        result.update(json.loads(meta.get("runtime", "{}")))
        result.update(
            revision=int(meta["revision"]),
            tracked_files=db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            pending=db.execute("SELECT 1 FROM outbox WHERE id=1").fetchone() is not None,
        )
    finally:
        db.close()
    if not result["running"]:
        result["status"] = "not_running"
    return result


class LocalMirror:
    """One root/project per data directory, with a durable, idempotent local outbox."""

    def __init__(self, root: Path, project_id: str, data_dir: Path, reconcile_seconds=60):
        if not 1 <= reconcile_seconds <= 86400:
            raise SyncError("reconciliation interval must be between one second and one day")
        self.project_id = validate_project(project_id)
        self.data_dir = data_dir.expanduser().resolve()
        self.scanner = Scanner(root.expanduser(), excluded_roots=(self.data_dir,))
        self.root = self.scanner.root
        self.reconcile_seconds = reconcile_seconds
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = (self.data_dir / "local.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise SyncError("another local mirror is already using this data directory") from exc
        try:
            self.state = LocalState(
                self.data_dir / "local.sqlite3",
                {"root": str(self.root), "project_id": self.project_id, "transport": "local"},
            )
            self.store = MirrorStore(self.data_dir / "server" / "mirror.sqlite3")
            projects = self.store.list_projects()["projects"]
            if any(p["project_id"] != self.project_id for p in projects):
                raise SyncError("local mode requires an isolated data directory for one project")
        except BaseException:
            self._lock.close()
            raise
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._runtime: dict = {}
        self._syncing = False
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _record(self, status: str, **values):
        self._runtime = {
            **self._runtime,
            "status": status,
            "updated_at": _now(),
            "process_id": os.getpid(),
            **values,
        }
        with self.state.connection() as db:
            db.execute(
                "INSERT INTO meta VALUES('runtime', ?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (json.dumps(self._runtime),),
            )

    def flush(self) -> dict | None:
        batch = self.state.pending()
        if batch is None:
            return None
        result = self.store.apply(self.project_id, batch)
        self.state.acknowledge(batch, result["revision"])
        return result

    def sync_once(self, scanned: ScanResult | None = None) -> dict:
        self._syncing = True
        try:
            return self._sync_once(scanned)
        finally:
            self._syncing = False

    def _sync_once(self, scanned: ScanResult | None = None) -> dict:
        # Recover a committed-but-not-acknowledged batch before considering new edits.
        self.flush()
        scanned = self.scanner.scan() if scanned is None else scanned
        batch = prepare_batch(scanned, self.state.hashes(), self.state.revision)
        if batch is not None:
            self.state.enqueue(batch)
            result = self.flush()
        else:
            result = {"project_id": self.project_id, "revision": self.state.revision}
        self._record("ready", last_success_at=_now())
        return {
            **result,
            "changed_files": len(batch.changes) if batch else 0,
            "status": "synced" if batch else "up_to_date",
            "skipped": scanned.skipped,
        }

    def _watch_event(self, result):
        status = result["status"]
        if status == "scan_retry":
            # Do not echo exception text, rejected content or skipped filenames.
            self._record("scan_retry")
        print(
            json.dumps(
                {
                    "status": status,
                    "project_id": self.project_id,
                    **{k: result[k] for k in ("revision", "changed_files") if k in result},
                }
            ),
            file=sys.stderr,
            flush=True,
        )

    def start(self):
        if self.worker is not None or self._closed:
            raise SyncError("local mirror cannot be started twice")
        # No MCP initialization succeeds until the first safe reconciliation finishes.
        self._watch_event(self.sync_once())

        def run():
            try:
                watch_source(
                    self.scanner,
                    self.sync_once,
                    self._watch_event,
                    self.stop_event,
                    self.reconcile_seconds,
                )
            except Exception:
                self._runtime["status"] = "failed"
                try:
                    self._record("failed")
                except sqlite3.Error:
                    pass
                print(
                    "Local source watcher failed; inspect local-status and restart.",
                    file=sys.stderr,
                    flush=True,
                )

        self.worker = threading.Thread(target=run, name="code-context-source", daemon=True)
        self.worker.start()

    def ensure_ready(self):
        if (
            self.worker is None
            or not self.worker.is_alive()
            or self.stop_event.is_set()
            or self._runtime.get("status") != "ready"
        ):
            raise MirrorError(
                "SOURCE_NOT_READY: local source is not ready; "
                "inspect connection_status or local-status and restart if needed"
            )

    def mcp_status(self) -> dict:
        """Read watcher state without scanning source, exposing paths, or updating metadata."""
        state = self._runtime.get("status", "starting")
        if self._closed or self.stop_event.is_set():
            state = "stopped"
        elif self.worker is not None and not self.worker.is_alive():
            state = "failed"
        elif self._syncing:
            state = "syncing"
        elif self.worker is None:
            state = "starting"
        return {
            "state": state,
            "last_sync_at": self._runtime.get("last_success_at"),
            "last_seen": _now(),
        }

    def close(self):
        if self._closed:
            return
        self.stop_event.set()
        if self.worker is not None:
            self.worker.join(timeout=20)
            if self.worker.is_alive():
                raise SyncError("source watcher did not stop; writer lock is retained until exit")
        try:
            self._record("stopped")
        finally:
            self._lock.close()
            self._closed = True

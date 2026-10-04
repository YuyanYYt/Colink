"""A single-writer sync client with an immutable, persistent outbox."""

import fcntl
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from watchfiles import watch

from code_context.models import FileChange, SyncBatch, validate_project
from code_context.policy import MAX_REQUEST_BYTES
from code_context.scanner import ScanError, Scanner, ScanResult


class SyncError(RuntimeError):
    """A permanent error: do not overwrite remote state or retry blindly."""


class RetryableSyncError(RuntimeError):
    """Keep the saved batch intact and retry it using the same request_id."""


def prepare_batch(scanned: ScanResult, baseline: dict[str, str], revision: int) -> SyncBatch | None:
    """Freeze the next delta, shared by local and HTTP producers."""
    changes = [
        FileChange(op="upsert", path=path, content=file.content, sha256=file.sha256)
        for path, file in sorted(scanned.files.items())
        if baseline.get(path) != file.sha256
    ]
    changes.extend(
        FileChange(op="delete", path=path)
        for path in sorted(baseline.keys() - scanned.files.keys())
    )
    if not changes and revision:
        return None
    return SyncBatch(
        request_id=uuid.uuid4().hex,
        base_revision=revision,
        mode="full" if revision == 0 else "delta",
        changes=changes,
    )


def normalize_server_url(server_url: str) -> str:
    parsed = urlsplit(server_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SyncError("server URL must not contain credentials, a query or a fragment")
    if not parsed.hostname or parsed.path not in ("", "/"):
        raise SyncError("server URL must be an origin without a path")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise SyncError("use HTTPS; plain HTTP is only accepted for loopback development")
    return server_url.rstrip("/")


def state_path(data_dir: Path, root: Path, project_id: str, server_url: str) -> Path:
    identity = json.dumps([str(root.resolve()), project_id, normalize_server_url(server_url)])
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return data_dir.expanduser().resolve() / "clients" / f"{project_id}-{suffix}.sqlite3"


def read_local_status(root: Path, project_id: str, server_url: str, data_dir: Path) -> dict:
    """Inspect an existing queue without authentication, writing state or taking the writer lock."""
    project_id = validate_project(project_id)
    server_url = normalize_server_url(server_url)
    root = root.expanduser().resolve()
    database = state_path(data_dir, root, project_id, server_url).expanduser().resolve()
    result = {
        "project_id": project_id,
        "root": str(root),
        "server_url": server_url,
        "state_database": str(database),
        "initialized": database.exists(),
        "revision": 0,
        "tracked_files": 0,
        "pending": False,
        "pending_changes": 0,
    }
    if not database.exists():
        return result
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        db.execute("BEGIN")
        revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        pending = db.execute("SELECT payload FROM outbox WHERE id=1").fetchone()
        count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        payload = json.loads(pending[0]) if pending else None
        result.update(
            revision=int(revision[0]),
            tracked_files=count,
            pending=pending is not None,
            pending_changes=len(payload["changes"]) if payload else 0,
        )
        return result
    finally:
        db.close()


class LocalState:
    def __init__(self, database: Path, identity: dict):
        self.database = database.expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise SyncError(f"unsupported client database schema: {version}")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT);
                PRAGMA user_version=1;
                """
            )
            encoded = json.dumps(identity, sort_keys=True)
            saved = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if saved and saved[0] != encoded:
                raise SyncError("client state belongs to a different project root or server")
            db.execute("INSERT OR IGNORE INTO meta VALUES('identity', ?)", (encoded,))
            db.execute("INSERT OR IGNORE INTO meta VALUES('revision', '0')")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @property
    def revision(self) -> int:
        with self.connection() as db:
            return int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])

    def hashes(self) -> dict[str, str]:
        with self.connection() as db:
            return dict(db.execute("SELECT path, sha256 FROM files"))

    def pending(self) -> SyncBatch | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM outbox WHERE id=1").fetchone()
        return SyncBatch.model_validate_json(row[0]) if row else None

    def enqueue(self, batch: SyncBatch) -> None:
        payload = batch.model_dump_json()
        if len(payload.encode()) > MAX_REQUEST_BYTES:
            raise SyncError("synchronization batch exceeds 20 MiB")
        with self.connection() as db:
            db.execute("INSERT INTO outbox VALUES(1, ?)", (payload,))

    def acknowledge(self, batch: SyncBatch, revision: int) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT payload FROM outbox WHERE id=1").fetchone()
            if not saved or SyncBatch.model_validate_json(saved[0]).request_id != batch.request_id:
                raise SyncError("outbox changed before acknowledgement")
            if revision != batch.base_revision + 1:
                raise SyncError("server returned an unexpected revision")
            if batch.mode == "full":
                db.execute("DELETE FROM files")
            for change in batch.changes:
                if change.op == "delete":
                    db.execute("DELETE FROM files WHERE path=?", (change.path,))
                else:
                    db.execute(
                        "INSERT INTO files VALUES(?, ?) ON CONFLICT(path) "
                        "DO UPDATE SET sha256=excluded.sha256",
                        (change.path, change.sha256),
                    )
            db.execute("UPDATE meta SET value=? WHERE key='revision'", (str(revision),))
            db.execute("DELETE FROM outbox WHERE id=1")


class SyncClient:
    def __init__(
        self,
        root: Path,
        project_id: str,
        server_url: str,
        sync_token: str,
        data_dir: Path,
        http_client: httpx.Client | None = None,
    ):
        if root.expanduser().is_symlink():
            raise SyncError("select a real project directory, not a symbolic link")
        self.root = root.expanduser().resolve()
        self.project_id = validate_project(project_id)
        self.server_url = normalize_server_url(server_url)
        if not sync_token:
            raise SyncError("set CODE_CONTEXT_SYNC_TOKEN before syncing")
        database = state_path(data_dir, self.root, self.project_id, self.server_url)
        self.scanner = Scanner(self.root, excluded_roots=(data_dir.expanduser().resolve(),))
        self.state = LocalState(
            database,
            {
                "root": str(self.root),
                "project_id": self.project_id,
                "server_url": self.server_url,
            },
        )
        self._lock = self.state.database.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise SyncError(
                "another sync client is already using this local project state"
            ) from exc
        self.http = http_client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        self._owns_http = http_client is None
        self.headers = {"Authorization": f"Bearer {sync_token}", "Content-Type": "application/json"}

    def close(self):
        if self._owns_http:
            self.http.close()
        self._lock.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self.http.request(
                method, self.server_url + path, headers=self.headers, **kwargs
            )
        except httpx.RequestError as exc:
            raise RetryableSyncError(
                "server unavailable; batch remains in the local queue"
            ) from exc
        if response.status_code >= 500 or response.status_code in {408, 429}:
            raise RetryableSyncError("server temporarily unavailable; queued batch is retained")
        if response.status_code == 409:
            raise SyncError(
                "remote revision changed; stop the other writer and inspect client state"
            )
        if response.status_code in {401, 403}:
            raise SyncError("server rejected authorization; check CODE_CONTEXT_SYNC_TOKEN")
        if response.status_code not in {200, 404}:
            raise SyncError(f"server rejected the request (HTTP {response.status_code})")
        return response

    def flush(self) -> dict | None:
        batch = self.state.pending()
        if batch is None:
            return None
        response = self._request(
            "POST", f"/api/projects/{self.project_id}/sync", content=batch.model_dump_json()
        )
        if response.status_code != 200:
            raise SyncError("synchronization endpoint not found")
        try:
            result = response.json()
            revision = result["revision"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RetryableSyncError("invalid acknowledgement; retain batch and retry") from exc
        if type(revision) is not int or result.get("project_id") != self.project_id:
            raise SyncError("server acknowledged a different project or invalid revision")
        self.state.acknowledge(batch, revision)
        return result

    def sync_once(self, scanned: ScanResult | None = None) -> dict:
        # Flush the frozen batch first: edits during downtime become a subsequent delta,
        # rather than changing the payload under an already-used idempotency key.
        self.flush()
        scanned = self.scanner.scan() if scanned is None else scanned
        baseline, revision = self.state.hashes(), self.state.revision
        batch = prepare_batch(scanned, baseline, revision)
        if batch is None:
            return {
                "project_id": self.project_id,
                "revision": revision,
                "changed_files": 0,
                "skipped": scanned.skipped,
                "status": "up_to_date",
            }
        self.state.enqueue(batch)
        result = self.flush()
        return {
            **result,
            "changed_files": len(batch.changes),
            "skipped": scanned.skipped,
            "status": "synced",
        }

    def watch(self, emit, stop_event=None, reconcile_seconds: float = 60):
        def emit_project(event):
            emit({**event, "project_id": self.project_id})

        watch_source(self.scanner, self.sync_once, emit_project, stop_event, reconcile_seconds)

    def status(self) -> dict:
        pending = self.state.pending()
        return {
            "project_id": self.project_id,
            "root": str(self.root),
            "server_url": self.server_url,
            "revision": self.state.revision,
            "tracked_files": len(self.state.hashes()),
            "pending": pending is not None,
            "pending_changes": len(pending.changes) if pending else 0,
            "state_database": str(self.state.database),
        }


def watch_source(scanner, sync_once, emit, stop_event=None, reconcile_seconds: float = 60):
    """Watch a source, with startup/periodic reconciliation and safe scan retries."""
    if not 1 <= reconcile_seconds <= 86400:
        raise SyncError("reconciliation interval must be between one second and one day")
    view = None
    next_scan, next_retry, attempts = 0.0, 0.0, 0

    def update(changes):
        nonlocal view, next_scan, next_retry, attempts
        now = time.monotonic()
        if view is None or now >= next_scan:
            view = scanner.scan()
            next_scan = now + reconcile_seconds
        elif changes:
            relative = set()
            for _, path in changes:
                candidate = Path(path)
                if candidate == scanner.root:
                    view = scanner.scan()
                    break
                relative.add(candidate.relative_to(scanner.root).as_posix())
            else:
                view = scanner.refresh(view, relative)
        if now < next_retry:
            return
        try:
            result = sync_once(view)
            if result["changed_files"] or attempts:
                emit(result)
            attempts, next_retry = 0, 0.0
        except RetryableSyncError:
            attempts += 1
            delay = min(2 ** min(attempts - 1, 5), 30)
            next_retry = time.monotonic() + delay
            emit({"status": "retrying", "retry_in_seconds": delay, "pending": True})

    def safely_update(changes):
        nonlocal next_scan
        try:
            update(changes)
        except ScanError as exc:
            # Never treat incomplete scans as deletions. Retry from a full view.
            next_scan = 0.0
            emit({"status": "scan_retry", "reason": str(exc)})

    safely_update(set())
    # A second scan after watcher initialization closes the initial startup gap.
    next_scan = 0.0
    for changes in watch(
        scanner.root,
        watch_filter=scanner.watch_filter,
        debounce=1000,
        step=200,
        rust_timeout=1000,
        yield_on_timeout=True,
        stop_event=stop_event,
    ):
        if stop_event is not None and stop_event.is_set():
            break
        safely_update(changes)

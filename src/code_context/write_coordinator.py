"""Controlled writes with local grants, comparison origins and crash recovery.

Permission, declared scope, source identity and persistent recovery capacity are
independent checks. Origin text is saved on first actual edit for Diff and safe
interrupted-operation recovery, not a user-facing code-undo service. The legacy
rollback engine only supports retained old recovery records and compatibility tests.
"""

import hashlib
import json
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import ExitStack

from code_context.policy import MAX_FILES, validate_path
from code_context.recovery_store import RecoveryStore, encode_metadata
from code_context.scanner import _version
from code_context.source_access import SourceError

REQUEST = re.compile(r"[A-Za-z0-9_-]{8,64}")
TASK = re.compile(r"wt_[a-f0-9]{32}")
TERMINAL = {"completed", "rolled_back"}
PENDING = {"prepared", "committing", "rollback_prepared"}
MAX_OPERATIONS = 1024
RETENTION_SECONDS = 7 * 24 * 3600


class WriteError(SourceError):
    """Safe, content-free authorization, precondition and task errors."""


def request_digest(parameters):
    try:
        raw = json.dumps(
            parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise WriteError("INVALID_WRITE_REQUEST: use bounded valid parameters") from None
    if len(raw) > 8 * 1024 * 1024:
        raise WriteError("WRITE_REQUEST_LIMIT: request exceeds its byte budget")
    return hashlib.sha256(raw).hexdigest()


def validate_request(request_id):
    if not isinstance(request_id, str) or REQUEST.fullmatch(request_id) is None:
        raise WriteError("INVALID_REQUEST_ID: use a stable 8-64 character request identifier")


class WriteCoordinator:
    def __init__(
        self,
        store: RecoveryStore,
        source_provider,
        *,
        control_alive=None,
        on_change=None,
        clock=time.time,
        baseline_seconds=15,
        baseline_bytes=512 * 1024 * 1024,
    ):
        self.store = store
        self.source_provider = source_provider
        self.control_alive = control_alive or (lambda: False)
        self.on_change = on_change or (lambda _project: None)
        self.clock = clock
        self.baseline_seconds = baseline_seconds
        self.baseline_bytes = baseline_bytes
        self.lock = threading.RLock()
        self.stop_requested = threading.Event()
        self.stop_requested.set()
        self.grants = {}
        self.closed = False
        self.inflight_project = None
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES('next_task_request',?)",
                ("req_" + uuid.uuid4().hex,),
            )
        from code_context.write_deletion import WriteDeletion
        from code_context.write_diff import TaskDiff
        from code_context.write_operations import WriteOperations
        from code_context.write_recovery import WriteRecovery
        from code_context.write_rollback import WriteRollback

        self.operations = WriteOperations(self)
        self.diff = TaskDiff(self)
        self.recovery = WriteRecovery(self)
        self.rollback = WriteRollback(self)
        self.deletion = WriteDeletion(self)

    def _pending(self):
        return bool(
            self.store.query(
                "SELECT task_id FROM operations "
                "WHERE state IN ('prepared','committing','rollback_prepared') LIMIT 1"
            )
            or self.store.query(
                "SELECT task_id FROM tasks "
                "WHERE state IN ('recovery_required','rolling_back') LIMIT 1"
            )
        )

    def _alive(self):
        try:
            return not self.closed and self.control_alive() is True
        except Exception:
            return False

    def enable(self, project_ids):
        """Local control only; never register this method as an MCP tool."""
        self.stop_requested.set()
        with self.lock:
            self.grants = {}
            if not self._alive():
                raise WriteError("LOCAL_CONTROL_UNAVAILABLE: reconnect locally before enabling")
            if self._pending():
                raise WriteError("WRITE_RECOVERY_REQUIRED: complete local recovery first")
            if (
                not isinstance(project_ids, list)
                or not 1 <= len(project_ids) <= 64
                or any(not isinstance(project, str) for project in project_ids)
                or len(set(project_ids)) != len(project_ids)
            ):
                raise WriteError("INVALID_WRITE_SCOPE: choose explicit locally readable projects")
            grants = {}
            for project in project_ids:
                if not isinstance(project, str):
                    raise WriteError(
                        "INVALID_WRITE_SCOPE: choose explicit locally readable projects"
                    )
                source = self.source_provider(project)
                source.ensure_available()
                grants[project] = source.source_id
            self._retire_expired()
            self.grants = grants
            self.stop_requested.clear()
            return self.status()

    def disable(self):
        # Reject new requests before waiting for an already committing operation.
        self.stop_requested.set()
        with self.lock:
            self.grants = {}

    def _authorized(self, project_id, *, _allow_pending=False):
        if self.stop_requested.is_set() or not self._alive():
            self.grants = {}
            raise WriteError("WRITE_DISABLED: enable this project locally for this connection")
        source = self.source_provider(project_id)
        source.ensure_available()
        if self.grants.get(project_id) != source.source_id:
            raise WriteError("WRITE_NOT_AUTHORIZED: this project/source is not locally writable")
        if not _allow_pending and self._pending():
            raise WriteError("WRITE_RECOVERY_REQUIRED: preserve materials and recover locally")
        return source

    def status(self):
        with self.lock:
            if not self._alive():
                self.grants = {}
                self.stop_requested.set()
            tasks = self.store.query(
                "SELECT task_id,project_id,state,created,completed FROM tasks ORDER BY created DESC"
            )
            active = next((row for row in tasks if row["state"] not in TERMINAL), None)
            return {
                "write_enabled": bool(self.grants) and not self.stop_requested.is_set(),
                "write_available": True,  # Core ready; permission is independent and default-off.
                "write_projects": sorted(self.grants),
                "active_task": active,
                "recent_task": next((row for row in tasks if row["state"] in TERMINAL), None),
                "recovery_required": self._pending(),
                "next_task_request_id": self.store.query(
                    "SELECT value FROM settings WHERE key='next_task_request'"
                )[0]["value"],
                "recovery_storage": self.store.usage(),
            }

    def _task(self, project_id, task_id, source):
        if not isinstance(task_id, str) or TASK.fullmatch(task_id) is None:
            raise WriteError("INVALID_TASK_ID: use a task returned by begin_write_task")
        rows = self.store.query("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if not rows:
            raise WriteError("WRITE_TASK_EXPIRED: retired task/request cannot execute again")
        task = rows[0]
        if task["project_id"] != project_id or task["source_id"] != source.source_id:
            raise WriteError("WRITE_TASK_SCOPE: task belongs to another project/source")
        if (
            task["state"] in TERMINAL
            and task["completed"] is not None
            and self.clock() - task["completed"] > RETENTION_SECONDS
        ):
            raise WriteError("WRITE_TASK_EXPIRED: retained request cannot execute again")
        return task

    def _scope(self, paths):
        if paths is None:
            return None
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= 1000
            or any(not isinstance(path, str) for path in paths)
        ):
            raise WriteError("INVALID_TASK_SCOPE: use 1-1000 relative paths or omit scope")
        try:
            for path in paths:
                validate_path(path)
        except ValueError:
            raise WriteError("INVALID_TASK_SCOPE: paths must stay within the project") from None
        if len(set(paths)) != len(paths):
            raise WriteError("INVALID_TASK_SCOPE: task paths must be unique")
        return sorted(paths)

    def _declared_missing_parent(self, source, path, scope):
        """Read-only validation of explicitly declared future directory ancestors."""
        parts = path.split("/")
        with source.root_fd() as root, ExitStack() as stack:
            spec = source._ignore(root)
            if source.scanner._path_problem(path, spec):
                raise SourceError("PATH_EXCLUDED: path is outside the allowed source policy")
            parent = root
            for index, name in enumerate(parts[:-1]):
                prefix = "/".join(parts[: index + 1])
                info = source.scanner._stat(parent, name, prefix)
                if info is None:
                    if all(
                        "/".join(parts[:size]) in scope for size in range(index + 1, len(parts))
                    ):
                        return True
                    raise WriteError("INVALID_TASK_SCOPE: declare all new parent directories")
                if not stat.S_ISDIR(info.st_mode):
                    raise WriteError("WRITE_BASELINE_UNSAFE: real directory parents required")
                parent = stack.enter_context(source.scanner._directory(parent, name, prefix, info))
        return False

    def _baseline(self, source, scope):
        started = time.monotonic()
        directories = {}
        if scope is None:
            metadata = source.manifest()
            if metadata["partial"]:
                raise WriteError("WRITE_BASELINE_PARTIAL: use a narrower declared task scope")
            paths = [item["path"] for item in metadata["files"]]
            for path in metadata["watch_directories"]:
                if path:
                    with source.parent_fd(path, directory=True) as (parent, name):
                        info = source.scanner._stat(parent, name, path)
                        if info is None or not stat.S_ISDIR(info.st_mode):
                            raise WriteError("WRITE_BASELINE_CHANGED: origin directory changed")
                        directories[path] = encode_metadata(_version(info))
        else:
            paths = scope
        rows, consumed = [], 0
        for path in paths:
            if time.monotonic() - started > self.baseline_seconds:
                raise WriteError("WRITE_BASELINE_TIME_LIMIT: use a narrower declared task scope")
            if scope is not None and self._declared_missing_parent(source, path, scope):
                continue
            # parent_fd reapplies current source/ignore/registered-child boundaries.
            with source.parent_fd(path) as (parent, name):
                before = source.scanner._stat(parent, name, path)
                if before is None:
                    continue  # Explicit future file; creation will require absence.
                if stat.S_ISDIR(before.st_mode) and scope is not None:
                    directories[path] = encode_metadata(_version(before))
                    continue  # Existing directories remain origins, not task-created objects.
                if not stat.S_ISREG(before.st_mode):
                    raise WriteError("WRITE_BASELINE_UNSAFE: allowed real text files required")
                consumed += before.st_size
                if consumed > self.baseline_bytes:
                    raise WriteError(
                        "WRITE_BASELINE_BYTE_LIMIT: use a narrower declared task scope"
                    )
                sha = source.fingerprint(path)
                after = source.scanner._stat(parent, name, path)
                if after is None or _version(before) != _version(after):
                    raise WriteError("WRITE_BASELINE_CHANGED: source changed while preparing task")
                if sha is not None:
                    rows.append((path, sha, encode_metadata(_version(after))))
        if len(rows) > MAX_FILES:
            raise WriteError("WRITE_BASELINE_FILE_LIMIT: use a narrower declared task scope")
        # No whole-repository atomic snapshot is claimed. All late joins must
        # still match these captured values at first actual modification.
        for path, sha, version in rows:
            if time.monotonic() - started > self.baseline_seconds:
                raise WriteError("WRITE_BASELINE_TIME_LIMIT: use a narrower declared task scope")
            with source.parent_fd(path) as (parent, name):
                info = source.scanner._stat(parent, name, path)
                if (
                    info is None
                    or encode_metadata(_version(info)) != version
                    or source.fingerprint(path) != sha
                ):
                    raise WriteError("WRITE_BASELINE_CHANGED: source changed while preparing task")
        for path, version in directories.items():
            if time.monotonic() - started > self.baseline_seconds:
                raise WriteError("WRITE_BASELINE_TIME_LIMIT: use a narrower declared task scope")
            with source.parent_fd(path, directory=True) as (parent, name):
                info = source.scanner._stat(parent, name, path)
                if info is None or encode_metadata(_version(info)) != version:
                    raise WriteError("WRITE_BASELINE_CHANGED: origin directory changed")
        source.ensure_available()
        return rows, list(directories.items())

    def reserve_growth(
        self,
        task_id=None,
        *,
        object_bytes=0,
        source_temp_bytes=0,
        metadata_bytes=0,
        future_body_bytes=None,
        additional_files=0,
        source=None,
    ):
        """Ordinary admission preserves room for completion/recovery/whole undo.

        Coordinator serialization makes this a logical reservation: no other
        growing write may spend it. Post-intent durable bookkeeping and local
        recovery can use the reserved space up to the hard store cap. Unknown
        out-of-band modifications of recovery SQL are not authorized consumers.
        """
        files = self.store.query("SELECT * FROM files WHERE task_id=?", (task_id,))
        attributes = self.store.query(
            "SELECT last_record FROM file_attributes WHERE task_id=?", (task_id,)
        )
        if future_body_bytes is None:
            future_body_bytes = sum(
                ((json.loads(row["last_version"])[3] + 4095) // 4096) * 4096
                for row in files
                if row["kind"] in {"modified", "created"} and row["last_version"] is not None
            )
        # Covers UTF-8 previews/results, multiple SQLite UPDATE page versions,
        # object receipts, and one bounded per-file rollback record. Attribute
        # records have a separate measured addition; they are never guessed free.
        headroom = (
            256 * 1024
            + (len(files) + additional_files) * 16 * 1024
            + sum(2 * len(row["last_record"].encode()) for row in attributes)
        )
        if source is not None and shutil.disk_usage(source.root).free < (
            self.store.min_free_bytes + source_temp_bytes
        ):
            raise WriteError("WRITE_DISK_SPACE: source filesystem needs staging headroom")
        return self.store.reserve(
            object_bytes=object_bytes + future_body_bytes,
            source_temp_bytes=source_temp_bytes,
            metadata_bytes=metadata_bytes + headroom,
        )

    def begin_write_task(self, project_id, request_id, *, title="", paths=None):
        validate_request(request_id)
        if not isinstance(title, str) or len(title) > 256:
            raise WriteError("INVALID_TASK_TITLE: use a title of at most 256 characters")
        scope = self._scope(paths)
        digest = request_digest({"project_id": project_id, "title": title, "paths": scope})
        with self.lock:
            source = self._authorized(project_id)
            previous = self.store.query("SELECT * FROM tasks WHERE request_id=?", (request_id,))
            if previous:
                task = self._task(project_id, previous[0]["task_id"], source)
                if json.loads(task["metadata"])["begin_digest"] != digest:
                    raise WriteError("REQUEST_ID_CONFLICT: same identifier has different content")
                if (
                    task["completed"] is not None
                    and self.clock() - task["completed"] > RETENTION_SECONDS
                ):
                    raise WriteError("WRITE_TASK_EXPIRED: request cannot execute again")
                return self._task_result(task)
            ticket = self.store.query("SELECT value FROM settings WHERE key='next_task_request'")[
                0
            ]["value"]
            if request_id != ticket:
                raise WriteError("WRITE_REQUEST_EXPIRED: use the currently issued task request")
            self._retire_expired()
            if self.store.query(
                "SELECT task_id FROM tasks WHERE state NOT IN ('completed','rolled_back') LIMIT 1"
            ):
                raise WriteError("WRITE_TASK_ACTIVE: continue or finish the existing task")
            rows, directories = self._baseline(source, scope)
            self._authorized(project_id)
            self.reserve_growth(
                metadata_bytes=sum(
                    len(path.encode()) + len(version) + 256 for path, _sha, version in rows
                )
                + sum(len(path.encode()) + len(version) + 256 for path, version in directories)
                + 16384
            )
            task_id = "wt_" + uuid.uuid4().hex
            task = {
                "task_id": task_id,
                "request_id": request_id,
                "project_id": project_id,
                "source_id": source.source_id,
                "state": "active",
                "created": self.clock(),
                "completed": None,
                "metadata": encode_metadata(
                    {
                        "title": title,
                        "scope": scope,
                        "begin_digest": digest,
                        "dependency_coverage": "not_captured; query current Python/Java read tools",
                    }
                ),
            }
            with self.store.transaction() as db:
                db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)", tuple(task.values()))
                db.executemany(
                    "INSERT INTO manifest VALUES(?,?,?,?)", ((task_id, *row) for row in rows)
                )
                db.executemany(
                    "INSERT INTO baseline_directories VALUES(?,?,?)",
                    ((task_id, *row) for row in directories),
                )
                db.execute(
                    "UPDATE settings SET value=? WHERE key='next_task_request'",
                    ("req_" + uuid.uuid4().hex,),
                )
            return self._task_result(task)

    def _task_result(self, task):
        return {
            "project_id": task["project_id"],
            "task_id": task["task_id"],
            "state": task["state"],
            "source_mode": "live",
            "task_origin_available": True,
            "baseline_files": self.store.query(
                "SELECT count(*) AS n FROM manifest WHERE task_id=?", (task["task_id"],)
            )[0]["n"],
            "source_is_untrusted": True,
        }

    def check_first_touch(self, project_id, task_id, path, document):
        """No write: enforce task-origin identity and declared allowed paths."""
        source = self._authorized(project_id)
        with source.parent_fd(path):
            pass
        task = self._task(project_id, task_id, source)
        if task["state"] != "active":
            raise WriteError("WRITE_TASK_NOT_ACTIVE: no new edits accepted")
        scope = json.loads(task["metadata"])["scope"]
        if scope is not None and path not in scope:
            raise WriteError("WRITE_TASK_PATH_SCOPE: path was not declared in this task")
        baseline = self.store.query(
            "SELECT * FROM manifest WHERE task_id=? AND path=?", (task_id, path)
        )
        origin_directory = self.store.query(
            "SELECT path FROM baseline_directories WHERE task_id=? AND path=?", (task_id, path)
        )
        if origin_directory:
            raise WriteError("WRITE_ORIGIN_CONFLICT: an origin directory cannot be adopted")
        if document is None:
            if baseline:
                raise WriteError("WRITE_ORIGIN_CONFLICT: an origin file disappeared")
            return
        if (
            not baseline
            or baseline[0]["sha256"] != document.sha256
            or tuple(json.loads(baseline[0]["version"])) != tuple(document.version)
        ):
            raise WriteError("WRITE_ORIGIN_CONFLICT: late-added file differs from the task origin")

    def apply_edit(self, project_id, task_id, request_id, path, expected_sha256, edit):
        return self.operations.apply_edit(
            project_id, task_id, request_id, path, expected_sha256, edit
        )

    def create_file(self, project_id, task_id, request_id, path, content):
        return self.operations.create_file(project_id, task_id, request_id, path, content)

    def create_directory(self, project_id, task_id, request_id, path):
        return self.operations.create_directory(project_id, task_id, request_id, path)

    def delete_file(self, project_id, task_id, request_id, path, expected_sha256):
        return self.deletion.delete_file(project_id, task_id, request_id, path, expected_sha256)

    def finish_write_task(self, project_id, task_id, request_id):
        return self.operations.finish_write_task(project_id, task_id, request_id)

    def recover(self, project_id):
        """Authenticated local control only. Never expose this method as an MCP tool."""
        return self.recovery.recover(project_id)

    def get_diff(self, project_id, **parameters):
        return self.diff.get_diff(project_id, **parameters)

    def guard_read(self, project_id):
        """Do not publish intermediate or pending source states as normal results."""
        # Query guards can run while a producer holds SourceAccess.lock. Never
        # acquire the coordinator lock here (writers take coordinator -> source).
        # The durable prepared intent precedes every source mutation; repeat this
        # guard before publishing. Unrelated projects are not suspended.
        if (
            self.inflight_project == project_id
            or self.store.query(
                "SELECT task_id FROM tasks WHERE project_id=? "
                "AND state IN ('recovery_required','rolling_back') LIMIT 1",
                (project_id,),
            )
            or self.store.query(
                "SELECT operations.task_id FROM operations JOIN tasks USING(task_id) "
                "WHERE tasks.project_id=? AND operations.state "
                "IN ('prepared','committing','rollback_prepared') LIMIT 1",
                (project_id,),
            )
        ):
            raise WriteError("WRITE_RECOVERY_REQUIRED: project publication is suspended")

    def _retire(self, task):
        if task["state"] not in TERMINAL:
            raise WriteError("WRITE_TASK_PROTECTED: active or pending materials cannot be retired")
        prefix = task["task_id"] + ":"
        with self.store.transaction() as db:
            db.execute("DELETE FROM object_refs WHERE substr(owner,1,?)=?", (len(prefix), prefix))
            db.execute("DELETE FROM tasks WHERE task_id=?", (task["task_id"],))
        self.store.collect_unreferenced()

    def _retire_expired(self):
        for task in self.store.query(
            "SELECT * FROM tasks WHERE state IN ('completed','rolled_back') AND completed<?",
            (self.clock() - RETENTION_SECONDS,),
        ):
            self._retire(task)
        retained = self.store.query(
            "SELECT * FROM tasks WHERE state IN ('completed','rolled_back') "
            "ORDER BY completed DESC,created DESC"
        )
        for task in retained[1:]:
            self._retire(task)

    def close(self):
        self.disable()
        self.closed = True

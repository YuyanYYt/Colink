"""Journaled whole-task rollback candidate, not a Runtime/MCP authorization surface.

The coordinator owns grants and local recovery dispatch. This module owns only
rollback_items and the rollback operation's durable state. Bodies remain in the
bounded RecoveryStore; the journal contains metadata, never source text. A source
mutation starts only after every latest body and origin has been verified.

Content, necessary attributes, identities and current source policy are checked.
Attributes are restored on private preparations, never by mutating an installed
original. SQLite and native calls are not a multi-file filesystem transaction
with arbitrary external editors. Modification times deliberately remain current.
"""

import json
import os
import re
import stat
import uuid
from contextlib import ExitStack
from dataclasses import asdict

from code_context.directory_mutation import (
    PreparedDirectory,
    _inspect,
    discard_directory_temp,
    remove_created_directory,
)
from code_context.file_mutation import (
    PreparedFile,
    _read_named,
    commit_file,
    discard_prepared,
    prepare_file,
)
from code_context.file_removal import (
    RemovedFile,
    discard_removed_file_temp,
    remove_created_file,
)
from code_context.policy import MAX_FILE_BYTES, validate_path
from code_context.recovery_store import encode_metadata
from code_context.scanner import _version
from code_context.source_access import SourceDocument, SourceError
from code_context.write_attributes import (
    current_file_attributes,
    directory_attributes,
    recorded_attributes,
    task_attributes,
)
from code_context.write_coordinator import (
    RETENTION_SECONDS,
    TERMINAL,
    WriteError,
    request_digest,
    validate_request,
)

MAX_ROLLBACK_ITEMS = 1024
MAX_ITEM_METADATA_BYTES = 16 * 1024
_TEMP = re.compile(r"\.colink-write-[a-f0-9]{32}\.tmp")
_HASH = re.compile(r"[a-f0-9]{64}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS rollback_items (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY(task_id, path)
)
"""
ATTRIBUTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rollback_attributes (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    latest_record TEXT NOT NULL, origin_record TEXT,
    PRIMARY KEY(task_id, path)
)
"""


class RollbackConflict(WriteError):
    """Known task-relative targets only; the error string never echoes paths.

    Structured local/UI callers may use conflict_paths (at most 1024 normalized
    paths of at most 1024 characters). Foreign entry names and bodies are omitted.
    """

    def __init__(self, count, conflict_paths=()):
        paths = tuple(sorted(set(conflict_paths)))
        if not 0 <= count <= MAX_ROLLBACK_ITEMS or len(paths) > MAX_ROLLBACK_ITEMS:
            raise WriteError("WRITE_ROLLBACK_METADATA: invalid conflict set")
        for path in paths:
            validate_path(path)
        super().__init__("WRITE_ROLLBACK_CONFLICT: target set changed; no source changes made")
        self.conflict_count = count
        self.conflict_paths = paths


def _metadata(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise WriteError("WRITE_ROLLBACK_METADATA: preserve invalid recovery metadata") from None


def _receipt(cls, value):
    try:
        value = dict(value)
        for key in ("parent_identity", "temp_identity", "file_identity", "directory_identity"):
            if key in value:
                value[key] = tuple(value[key])
        return cls(**value)
    except (TypeError, ValueError, KeyError):
        raise WriteError("WRITE_ROLLBACK_METADATA: preserve invalid native receipt") from None


def _allocated(size):
    return ((size + 4095) // 4096) * 4096


def _identity(info):
    # Native helpers bind dev/inode, not scanner's dev/inode/mode triple.
    return info.st_dev, info.st_ino


def _encode_item(data):
    value = encode_metadata(data)
    if len(value.encode()) > MAX_ITEM_METADATA_BYTES:
        raise WriteError("WRITE_ROLLBACK_METADATA_LIMIT: preserve bounded journal material")
    return value


def _integers(value, length):
    return (
        isinstance(value, (tuple, list))
        and len(value) == length
        and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in value)
    )


class WriteRollback:
    def __init__(self, c):
        self.c, self.store = c, c.store
        with self.store.transaction() as db:
            db.execute(SCHEMA)
            db.execute(ATTRIBUTE_SCHEMA)
            columns = db.execute("PRAGMA table_info(rollback_items)").fetchall()
            if [(row["name"], row["pk"]) for row in columns] != [
                ("task_id", 1),
                ("path", 2),
                ("kind", 0),
                ("state", 0),
                ("metadata", 0),
            ]:
                raise WriteError("WRITE_ROLLBACK_SCHEMA: preserve unknown journal layout")

    def _source(self, source, project, *, local):
        if local:
            if not self.c._alive():
                raise WriteError(
                    "LOCAL_CONTROL_UNAVAILABLE: resume only on an active local connection"
                )
            current = self.c.source_provider(project)
        else:
            current = self.c._authorized(project, _allow_pending=True)
        if current is not source or current.source_id != source.source_id:
            raise WriteError("WRITE_ROLLBACK_SCOPE: source binding changed")
        source.ensure_available()

    def _policy(self, source):
        with source.root_fd() as root:
            return encode_metadata(
                {
                    "ignore_versions": source._ignore_versions(root),
                    "excluded": sorted(source.scanner._excluded),
                }
            )

    def _source_policy(self, source, task, operation):
        self._source(source, task["project_id"], local=self._local)
        if self._policy(source) != _metadata(operation["metadata"])["policy"]:
            raise WriteError("WRITE_ROLLBACK_SCOPE: source policy changed during recovery")

    def _eligible(self, task):
        rows = self.store.query("SELECT rowid AS ordinal,* FROM tasks ORDER BY rowid")
        current = next(row for row in rows if row["task_id"] == task["task_id"])
        if task["state"] == "active":
            if any(
                row["task_id"] != task["task_id"] and row["state"] not in TERMINAL for row in rows
            ):
                raise WriteError("WRITE_ROLLBACK_TASK_ORDER: another task is protected")
            return
        if task["state"] != "completed":
            raise WriteError("WRITE_ROLLBACK_TASK_STATE: choose an active or latest completed task")
        if task["completed"] is None or self.c.clock() - task["completed"] > RETENTION_SECONDS:
            raise WriteError("WRITE_TASK_EXPIRED: completed recovery point has expired")
        terminal = [row for row in rows if row["state"] in TERMINAL]
        if max(terminal, key=lambda row: row["ordinal"])["task_id"] != task["task_id"] or any(
            row["ordinal"] > current["ordinal"] for row in rows
        ):
            raise WriteError("WRITE_ROLLBACK_TASK_ORDER: a later task takes precedence")

    def _rows(self, task):
        rows = self.store.query(
            "SELECT * FROM files WHERE task_id=? ORDER BY path", (task["task_id"],)
        )
        if len(rows) > MAX_ROLLBACK_ITEMS:
            raise WriteError("WRITE_ROLLBACK_LIMIT: recovery set exceeds its bounded item limit")
        try:
            scope = _metadata(task["metadata"])["scope"]
            for row in rows:
                validate_path(row["path"])
                if row["kind"] not in {"modified", "created", "directory"}:
                    raise ValueError
                if scope is not None and row["path"] not in scope:
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            raise WriteError("WRITE_ROLLBACK_SCOPE: invalid task recovery set") from None
        return rows

    def _file(self, source, path):
        with source.parent_fd(path) as (parent, name):
            before = source.scanner._stat(parent, name, path)
            if (
                before is None
                or not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) > 0o777
            ):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: owned single-link file required")
            document = source.read(path)
            after = source.scanner._stat(parent, name, path)
            if (
                after is None
                or _version(before) != document.version
                or _version(after) != document.version
            ):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: file changed during validation")
            return document, _identity(os.fstat(parent))

    def _directory(self, source, row, children):
        binding = _metadata(row["directory_identity"])
        with source.parent_fd(row["path"], directory=True) as (parent, name):
            before = source.scanner._stat(parent, name, row["path"])
            if (
                before is None
                or not stat.S_ISDIR(before.st_mode)
                or _identity(before) != tuple(binding["identity"])
                or stat.S_IMODE(before.st_mode) != binding["mode"]
                or before.st_uid != os.geteuid()
                or _identity(os.fstat(parent)) != tuple(binding["parent_identity"])
            ):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: created directory binding changed")
            with source.scanner._directory(parent, name, row["path"], before) as fd:
                # Do not filter: ignored, hidden and foreign entries still conflict.
                with os.scandir(fd) as entries:
                    actual = set()
                    for entry in entries:
                        actual.add(entry.name)
                        if len(actual) > MAX_ROLLBACK_ITEMS:
                            raise WriteError(
                                "WRITE_ROLLBACK_CONFLICT: directory contains foreign entries"
                            )
                if actual != children:
                    raise WriteError(
                        "WRITE_ROLLBACK_CONFLICT: created directory contains foreign entries"
                    )
                if _version(os.fstat(fd)) != _version(before):
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: directory changed during validation")
            after = source.scanner._stat(parent, name, row["path"])
            if after is None or _version(after) != _version(before):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: directory changed after validation")
            return binding

    def _precheck(self, source, task):
        rows, plan, conflicts = self._rows(task), [], set()
        children = {row["path"]: set() for row in rows if row["kind"] == "directory"}
        for row in rows:
            parent, _, name = row["path"].rpartition("/")
            if parent in children:
                if row["kind"] == "created" and row["last_hash"] is None:
                    continue  # A task-created file already deleted is not a directory entry.
                if row["kind"] not in {"created", "directory"}:
                    conflicts.add(row["path"])
                children[parent].add(name)
        for row in rows:
            try:
                item = {"task_id": task["task_id"], "path": row["path"], "kind": row["kind"]}
                item["latest_attributes"] = task_attributes(
                    self.c, task["task_id"], row["path"]
                ).sha256
                if row["kind"] == "directory":
                    item["directory_binding"] = self._directory(source, row, children[row["path"]])
                else:
                    deleted = row["last_hash"] is None
                    if deleted:
                        from code_context.write_deletion import verify_deleted

                        parent = verify_deleted(source, row)
                        document = None
                    else:
                        document, parent = self._file(source, row["path"])
                        if document.sha256 != row["last_hash"] or document.version != tuple(
                            json.loads(row["last_version"])
                        ):
                            raise WriteError(
                                "WRITE_ROLLBACK_CONFLICT: task file differs from its last state"
                            )
                    item.update(
                        latest_deleted=deleted,
                        latest_hash=document.sha256 if document else None,
                        latest_version=document.version if document else None,
                        latest_mode=document.mode if document else None,
                        latest_size=document.size if document else 0,
                        parent_identity=parent,
                        origin_hash=row["origin_hash"],
                        origin_mode=row["origin_mode"],
                    )
                    if row["kind"] == "modified":
                        item["origin_attributes"] = task_attributes(
                            self.c, task["task_id"], row["path"], origin=True
                        ).sha256
                        raw = self.store.read_blob(row["origin_hash"])
                        if (
                            not isinstance(row["origin_mode"], int)
                            or isinstance(row["origin_mode"], bool)
                            or not 0 <= row["origin_mode"] <= 0o777
                        ):
                            raise WriteError("WRITE_ROLLBACK_METADATA: invalid origin permissions")
                        raw.decode("utf-8")
                        item["origin_size"] = len(raw)
                    elif row["origin_hash"] is not None:
                        raise WriteError(
                            "WRITE_ROLLBACK_METADATA: created file has an unexpected origin"
                        )
                self._check_attributes(source, item, "before")
                plan.append(item)
            except (SourceError, ValueError, TypeError, KeyError, UnicodeError):
                conflicts.add(row["path"])
        if conflicts:
            raise RollbackConflict(len(conflicts), conflicts)
        source.ensure_available()
        return plan

    def _check_attributes(self, source, item, phase):
        data = item.get("data", item)
        if phase == "before" and data.get("latest_deleted"):
            # _deleted_restore_progress proves origin attrs on both links of a
            # no-overwrite restore. The ordinary single-link check cannot be used
            # at the crash boundary between installation and temporary unlink.
            return
        if phase == "after" and data["kind"] != "modified":
            return  # _done has already proven absence, including removed ancestors.
        if data["kind"] == "directory":
            with source.parent_fd(data["path"], directory=True) as (parent, name):
                info = source.scanner._stat(parent, name, data["path"])
            if info is None:
                return  # Removal/absence is independently proven by the journal.
            binding = data["directory_binding"]
            actual = directory_attributes(
                source, data["path"], binding["identity"], binding["mode"]
            )
            expected = self._attributes(item)
        else:
            with source.parent_fd(data["path"]) as (parent, name):
                info = source.scanner._stat(parent, name, data["path"])
            if info is None:
                return  # An isolated created file is validated in _captured.
            prepared = data.get("prepared")
            restored = data["kind"] == "modified" and (
                phase == "after"
                or (prepared and _identity(info) == tuple(prepared["temp_identity"]))
            )
            expected = self._attributes(item, origin=restored)
            actual = current_file_attributes(source, data["path"], _version(info))
        if actual != expected:
            raise WriteError("WRITE_ROLLBACK_ATTRIBUTE_CONFLICT: necessary attributes changed")

    def _restore_attributes(self, source, item):
        """Verify only: necessary attributes were set before atomic installation."""
        self._check_attributes(source, item, "after")

    def _attributes(self, item, *, origin=False):
        data = item.get("data", item)
        rows = self.store.query(
            "SELECT latest_record,origin_record FROM rollback_attributes "
            "WHERE task_id=? AND path=?",
            (data["task_id"], data["path"]),
        )
        if rows:
            record = rows[0]["origin_record" if origin else "latest_record"]
            if record is None:
                raise WriteError("WRITE_ROLLBACK_METADATA: required attributes unavailable")
            attrs = recorded_attributes(record)
        else:
            attrs = task_attributes(self.c, data["task_id"], data["path"], origin=origin)
        if attrs.sha256 != data["origin_attributes" if origin else "latest_attributes"]:
            raise WriteError("WRITE_ROLLBACK_METADATA: attribute journal binding changed")
        return attrs

    def _reserve(self, source, task, plan):
        missing = {}
        peak = 0
        for item in plan:
            if item["kind"] == "directory":
                peak = max(peak, 4096)
            else:
                if not item.get("latest_deleted") and not self.store.query(
                    "SELECT sha256 FROM objects WHERE sha256=?", (item["latest_hash"],)
                ):
                    missing[item["latest_hash"]] = _allocated(item["latest_size"])
                peak = max(peak, _allocated(max(item["latest_size"], item.get("origin_size", 0))))
        # Latest copies are charged here, so do not also charge future_body_bytes.
        # The coordinator separately protects per-item/admin/attribute headroom.
        self.c.reserve_growth(
            task["task_id"],
            object_bytes=sum(missing.values()),
            future_body_bytes=0,
            source_temp_bytes=peak,
            metadata_bytes=65536
            + sum(4 * len(item["path"].encode()) for item in plan)
            + sum(
                len(encode_metadata(self._attributes(item, origin=True).to_record()).encode())
                for item in plan
                if item["kind"] == "modified"
            ),
            source=source,
        )

    def _record(self, source, task, request, digest, plan):
        summary = {
            "kind": "rollback",
            "phase": "backing_up",
            "source_id": source.source_id,
            "item_count": len(plan),
            "policy": self._policy(source),
            "attribute_support": "verified_necessary_attributes",
        }
        with self.store.transaction() as db:
            sequence = db.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM operations WHERE task_id=?",
                (task["task_id"],),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,NULL,?)",
                (
                    task["task_id"],
                    request,
                    digest,
                    "rollback_prepared",
                    encode_metadata(summary),
                    sequence,
                ),
            )
            for item in plan:
                attrs = self.store.query(
                    "SELECT last_record,origin_record FROM file_attributes "
                    "WHERE task_id=? AND path=?",
                    (task["task_id"], item["path"]),
                )[0]
                db.execute(
                    "INSERT INTO rollback_attributes VALUES(?,?,?,?)",
                    (task["task_id"], item["path"], attrs["last_record"], attrs["origin_record"]),
                )
                data = {
                    **item,
                    "request_id": request,
                    "source_id": source.source_id,
                    "temp_name": ".colink-write-" + uuid.uuid4().hex + ".tmp",
                    "backup_verified": False,
                    "origin_verified": False,
                }
                if item["kind"] != "directory":
                    data["backup_owner"] = f"{task['task_id']}:rollback:{request}:{item['path']}"
                db.execute(
                    "INSERT INTO rollback_items VALUES(?,?,?,?,?)",
                    (task["task_id"], item["path"], item["kind"], "queued", _encode_item(data)),
                )
            db.execute("UPDATE tasks SET state='rolling_back' WHERE task_id=?", (task["task_id"],))

    def _items(self, task, operation):
        rows = self.store.query("SELECT * FROM rollback_items WHERE task_id=?", (task["task_id"],))
        summary = _metadata(operation["metadata"])
        if len(rows) != summary["item_count"] or len(rows) > MAX_ROLLBACK_ITEMS:
            raise WriteError("WRITE_ROLLBACK_METADATA: incomplete recovery item set")
        expected = {row["path"]: row for row in self._rows(task)}
        if {row["path"]: row["kind"] for row in rows} != {
            p: row["kind"] for p, row in expected.items()
        }:
            raise WriteError("WRITE_ROLLBACK_METADATA: recovery set changed")
        for row in rows:
            row["data"] = _metadata(row["metadata"])
            data = row["data"]
            _encode_item(data)
            if (
                data["path"] != row["path"]
                or data["task_id"] != task["task_id"]
                or data["kind"] != row["kind"]
                or data["request_id"] != operation["request_id"]
                or data["source_id"] != task["source_id"]
            ):
                raise WriteError("WRITE_ROLLBACK_SCOPE: recovery item binding changed")
            states = {"queued", "done"} | (
                {"preparing", "installing", "installed"}
                if row["kind"] == "modified"
                else {"removing", "isolated"}
            )
            if (
                row["state"] not in states
                or not isinstance(data["temp_name"], str)
                or _TEMP.fullmatch(data["temp_name"]) is None
                or type(data["backup_verified"]) is not bool
                or type(data["origin_verified"]) is not bool
            ):
                raise WriteError("WRITE_ROLLBACK_METADATA: invalid recovery state")
            if row["kind"] != "directory" and data["backup_owner"] != (
                f"{task['task_id']}:rollback:{operation['request_id']}:{row['path']}"
            ):
                raise WriteError("WRITE_ROLLBACK_SCOPE: recovery backup binding changed")
            recorded = expected[row["path"]]
            self._attributes(row)
            if row["kind"] == "modified":
                self._attributes(row, origin=True)
            if row["kind"] == "directory":
                if data["directory_binding"] != _metadata(recorded["directory_identity"]):
                    raise WriteError("WRITE_ROLLBACK_SCOPE: directory journal binding changed")
            else:
                if (
                    type(data.get("latest_deleted", False)) is not bool
                    or not _integers(data["parent_identity"], 2)
                    or data["origin_hash"] != recorded["origin_hash"]
                    or data["origin_mode"] != recorded["origin_mode"]
                ):
                    raise WriteError("WRITE_ROLLBACK_METADATA: invalid file journal binding")
                if data.get("latest_deleted"):
                    if (
                        data["latest_hash"] is not None
                        or data["latest_version"] is not None
                        or data["latest_mode"] is not None
                        or data["latest_size"] != 0
                    ):
                        raise WriteError("WRITE_ROLLBACK_METADATA: invalid absence journal")
                    binding = _metadata(recorded["directory_identity"])
                    if binding.get("deleted_parent_identity") != data["parent_identity"]:
                        raise WriteError("WRITE_ROLLBACK_SCOPE: deleted parent binding changed")
                elif (
                    not _integers(data["latest_version"], 6)
                    or not isinstance(data["latest_hash"], str)
                    or _HASH.fullmatch(data["latest_hash"]) is None
                    or not 0 <= data["latest_size"] <= MAX_FILE_BYTES
                    or data["latest_size"] != data["latest_version"][3]
                    or data["latest_mode"] != stat.S_IMODE(data["latest_version"][2])
                ):
                    raise WriteError("WRITE_ROLLBACK_METADATA: invalid file journal binding")
                if row["state"] == "done":
                    last_hash = data["origin_hash"] if row["kind"] == "modified" else None
                    last_version = (
                        data.get("restored_version") if row["kind"] == "modified" else None
                    )
                else:
                    last_hash, last_version = data["latest_hash"], data["latest_version"]
                if (
                    recorded["last_hash"] != last_hash
                    or (json.loads(recorded["last_version"]) if recorded["last_version"] else None)
                    != last_version
                ):
                    raise WriteError(
                        "WRITE_ROLLBACK_METADATA: task file state differs from journal"
                    )
        return sorted(
            rows, key=lambda row: (row["kind"] == "directory", -row["path"].count("/"), row["path"])
        )

    def _save_item(self, task, item, state):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE rollback_items SET state=?,metadata=? WHERE task_id=? AND path=?",
                (state, _encode_item(item["data"]), task["task_id"], item["path"]),
            )
            if state == "done" and item["kind"] != "directory":
                data = item["data"]
                if item["kind"] == "modified":
                    record = encode_metadata(self._attributes(item, origin=True).to_record())
                    db.execute(
                        "UPDATE file_attributes SET last_record=? WHERE task_id=? AND path=?",
                        (record, task["task_id"], item["path"]),
                    )
                db.execute(
                    "UPDATE files SET last_hash=?,last_version=? WHERE task_id=? AND path=?",
                    (
                        data["origin_hash"] if item["kind"] == "modified" else None,
                        encode_metadata(data["restored_version"])
                        if item["kind"] == "modified"
                        else None,
                        task["task_id"],
                        item["path"],
                    ),
                )
        item["state"] = state

    def _phase(self, task, operation, phase):
        data = _metadata(operation["metadata"])
        data["phase"] = phase
        with self.store.transaction() as db:
            db.execute(
                "UPDATE operations SET metadata=? WHERE task_id=? AND request_id=?",
                (encode_metadata(data), task["task_id"], operation["request_id"]),
            )
        operation["metadata"] = encode_metadata(data)

    def _blob(self, sha):
        with self.store.lock:
            rows = self.store.query("SELECT * FROM objects WHERE sha256=?", (sha,))
            if rows and rows[0]["state"] == "pending":
                # A complete pending body may have crashed before its fsync.
                # Verify its registered inode/hash, durably flush it and the
                # private object directory, then confirm it before promoting.
                self.store.read_blob(sha, _recovering=True)
                with self.store.objects.root_fd() as parent:
                    fd = os.open(sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    try:
                        before = os.fstat(fd)
                        self.store._private(before)
                        if tuple(json.loads(rows[0]["identity"])) != _version(before)[:3]:
                            raise WriteError("WRITE_ROLLBACK_METADATA: recovery inode changed")
                        os.fsync(fd)
                        after = os.stat(sha, dir_fd=parent, follow_symlinks=False)
                        if _version(before) != _version(os.fstat(fd)) or _version(
                            before
                        ) != _version(after):
                            raise WriteError(
                                "WRITE_ROLLBACK_METADATA: recovery body changed during fsync"
                            )
                    finally:
                        os.close(fd)
                    os.fsync(parent)
                self.store.read_blob(sha, _recovering=True)
                with self.store.transaction() as db:
                    db.execute("UPDATE objects SET state='ready' WHERE sha256=?", (sha,))
        return self.store.read_blob(sha)

    def _backups(self, source, task, operation, items):
        for item in items:
            data = item["data"]
            if item["kind"] != "directory":
                if data.get("latest_deleted"):
                    data["backup_verified"] = True  # There is no current body to copy.
                elif not data["backup_verified"]:
                    document, parent = self._file(source, item["path"])
                    if (
                        document.sha256 != data["latest_hash"]
                        or document.version != tuple(data["latest_version"])
                        or parent != tuple(data["parent_identity"])
                    ):
                        raise WriteError("WRITE_ROLLBACK_CONFLICT: source changed before backup")
                    pending = self.store.query(
                        "SELECT state FROM objects WHERE sha256=?", (data["latest_hash"],)
                    )
                    if pending:
                        self._blob(data["latest_hash"])
                    sha = self.store.put_blob(document.content.encode(), data["backup_owner"])
                    if sha != data["latest_hash"]:
                        raise WriteError("WRITE_ROLLBACK_METADATA: recovery body binding changed")
                    data["backup_verified"] = True
                if not data.get("latest_deleted"):
                    self._blob(data["latest_hash"])
                if item["kind"] == "modified":
                    self._blob(data["origin_hash"])
            data["origin_verified"] = True
            self._save_item(task, item, "queued")
        checked = self._precheck(source, task)
        for item in checked:
            saved = next(row["data"] for row in items if row["path"] == item["path"])
            if any(json.loads(encode_metadata(value)) != saved[key] for key, value in item.items()):
                raise WriteError("WRITE_ROLLBACK_SCOPE: backup plan binding changed")
        self._source_policy(source, task, operation)
        self._phase(task, operation, "ready")

    def _temp(self, source, item):
        data = item["data"]
        with source.parent_fd(item["path"], directory=item["kind"] == "directory") as (
            parent,
            target,
        ):
            expected = data.get(
                "parent_identity", data.get("directory_binding", {}).get("parent_identity")
            )
            if _identity(os.fstat(parent)) != tuple(expected):
                raise WriteError("WRITE_ROLLBACK_SCOPE: recovery parent changed")
            return source.scanner._stat(parent, target, item["path"]), source.scanner._stat(
                parent, data["temp_name"], item["path"]
            )

    def _document(self, item):
        data = item["data"]
        raw = self._blob(data["latest_hash"])
        if len(raw) != data["latest_size"]:
            raise WriteError("WRITE_ROLLBACK_METADATA: latest body size differs from journal")
        return SourceDocument(
            item["path"],
            raw.decode(),
            data["latest_hash"],
            len(raw),
            data["latest_mode"],
            tuple(data["latest_version"]),
        )

    def _prepared(self, source, item):
        data = item["data"]
        if not data.get("prepared"):
            return None
        receipt = _receipt(PreparedFile, data["prepared"])
        if (
            receipt.path != item["path"]
            or receipt.temp_name != data["temp_name"]
            or receipt.source_id != source.source_id
            or receipt.parent_identity != tuple(data["parent_identity"])
            or not _integers(receipt.temp_identity, 2)
            or receipt.sha256 != data["origin_hash"]
            or receipt.size != data["origin_size"]
            or receipt.mode != data["origin_mode"]
            or receipt.attribute_sha256 != data["origin_attributes"]
        ):
            raise WriteError("WRITE_ROLLBACK_SCOPE: prepared origin binding differs from journal")
        return receipt

    def _prepared_temp(self, source, item, receipt, *, sync=False):
        with source.parent_fd(item["path"]) as (parent, _):
            if _identity(os.fstat(parent)) != receipt.parent_identity:
                raise WriteError("WRITE_ROLLBACK_SCOPE: prepared parent changed")
            actual = _read_named(parent, receipt.temp_name, attributes=True)
            if (
                actual.sha256 != receipt.sha256
                or _identity(actual.info) != receipt.temp_identity
                or len(actual.raw) != receipt.size
                or stat.S_IMODE(actual.info.st_mode) != receipt.mode
                or actual.attributes != self._attributes(item, origin=True)
            ):
                raise WriteError(
                    "WRITE_ROLLBACK_CONFLICT: preserve incomplete or changed preparation"
                )
            if sync:
                fd = os.open(
                    receipt.temp_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                )
                try:
                    if _version(os.fstat(fd)) != _version(actual.info):
                        raise WriteError(
                            "WRITE_ROLLBACK_CONFLICT: prepared inode changed before fsync"
                        )
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(parent)

    def _removal_receipt(self, source, item):
        data = item["data"]
        if item["kind"] == "directory":
            binding = data["directory_binding"]
            receipt = PreparedDirectory(
                item["path"],
                data["temp_name"],
                tuple(binding["parent_identity"]),
                tuple(binding["identity"]),
                binding["mode"],
                source.source_id,
                data["latest_attributes"],
            )
        else:
            receipt = RemovedFile(
                item["path"],
                data["temp_name"],
                tuple(data["parent_identity"]),
                tuple(data["latest_version"][:2]),
                data["latest_mode"],
                data["latest_hash"],
                data["latest_size"],
                source.source_id,
                data["latest_attributes"],
            )
        if data.get("removed") and _receipt(type(receipt), data["removed"]) != receipt:
            raise WriteError("WRITE_ROLLBACK_SCOPE: isolated receipt binding differs from journal")
        return receipt

    def _captured(self, source, item, *, original=False, directory=False):
        data = item["data"]
        with source.parent_fd(item["path"], directory=directory) as (parent, target):
            identity = data.get(
                "parent_identity", data.get("directory_binding", {}).get("parent_identity")
            )
            if _identity(os.fstat(parent)) != tuple(identity):
                raise WriteError("WRITE_ROLLBACK_SCOPE: isolated parent changed")
            if directory:
                binding = data["directory_binding"]
                if source.scanner._stat(parent, target, item["path"]) is not None:
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: removed directory reappeared")
                _inspect(
                    parent,
                    data["temp_name"],
                    tuple(binding["identity"]),
                    (binding["mode"],),
                    attribute_sha256=data["latest_attributes"],
                )
            else:
                if not original and source.scanner._stat(parent, target, item["path"]) is not None:
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: removed target reappeared")
                actual = _read_named(parent, data["temp_name"], attributes=True)
                if (
                    actual.sha256 != data["latest_hash"]
                    or _identity(actual.info) != tuple(data["latest_version"][:2])
                    or stat.S_IMODE(actual.info.st_mode) != data["latest_mode"]
                    or len(actual.raw) != data["latest_size"]
                    or actual.attributes != self._attributes(item)
                ):
                    raise WriteError(
                        "WRITE_ROLLBACK_CONFLICT: captured latest state cannot be confirmed"
                    )

    def _restore(self, source, task, operation, item):
        if item["data"].get("latest_deleted"):
            return self._restore_deleted(source, task, operation, item)
        data = item["data"]
        current, temp = self._temp(source, item)
        receipt = self._prepared(source, item)
        if current is None:
            raise WriteError("WRITE_ROLLBACK_CONFLICT: modified target disappeared")
        document, _ = self._file(source, item["path"])
        installed = receipt is not None and document.version[:2] == receipt.temp_identity
        if installed:
            if document.sha256 != data["origin_hash"] or document.mode != data["origin_mode"]:
                raise WriteError("WRITE_ROLLBACK_CONFLICT: restored target changed")
            if data.get("native_version") and document.version != tuple(data["native_version"]):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: verified restored target changed")
            if temp is None and not data.get("capture_verified"):
                raise WriteError(
                    "WRITE_ROLLBACK_UNCONFIRMED_SWAP: displaced source cannot be verified"
                )
            if temp is not None:
                self._captured(source, item, original=True)
            data["capture_verified"] = True
            data["native_version"] = document.version
            self._save_item(task, item, "installed")
        else:
            if document.sha256 != data["latest_hash"] or document.version != tuple(
                data["latest_version"]
            ):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: target is not the planned latest state")
            if temp is not None and receipt is None:
                raise WriteError("WRITE_ROLLBACK_UNKNOWN_TEMP: preserve an unregistered temporary")
            if temp is None:
                if receipt is not None:
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: registered preparation disappeared")
                self._save_item(task, item, "preparing")

                def created(prepared):
                    data["prepared"] = asdict(prepared)
                    self._save_item(task, item, "preparing")

                receipt = prepare_file(
                    source,
                    item["path"],
                    self._blob(data["origin_hash"]),
                    data["origin_mode"],
                    data["temp_name"],
                    created,
                    self._attributes(item, origin=True),
                )
            else:
                self._prepared_temp(source, item, receipt, sync=True)
            self._save_item(task, item, "installing")
            self._source(source, task["project_id"], local=self._local)
            after = commit_file(source, receipt, self._document(item), self._attributes(item))
            self._source_policy(source, task, operation)
            self._captured(source, item, original=True)
            data["capture_verified"] = True
            data["native_version"] = after.version
            self._save_item(task, item, "installed")
        if temp is not None or not data.get("temp_cleaned"):
            self._source_policy(source, task, operation)
            discard_prepared(
                source,
                receipt,
                data["latest_hash"],
                tuple(data["latest_version"][:2]),
                self._attributes(item),
            )
        data["temp_cleaned"] = True
        self._restore_attributes(source, item)
        final, _ = self._file(source, item["path"])
        if (
            final.sha256 != data["origin_hash"]
            or final.mode != data["origin_mode"]
            or final.version[:2] != receipt.temp_identity
        ):
            raise WriteError("WRITE_ROLLBACK_CONFLICT: restored file changed before its result")
        data["restored_version"] = final.version
        self._save_item(task, item, "done")

    def _deleted_restore_progress(self, source, item):
        """Only accept an absent target or our fully verified prepared origin."""
        data = item["data"]
        current, temporary = self._temp(source, item)
        receipt = self._prepared(source, item)
        if current is not None:
            if receipt is None or _identity(current) != receipt.temp_identity:
                raise WriteError("WRITE_ROLLBACK_CONFLICT: deleted target reappeared")
            with source.parent_fd(item["path"]) as (parent, name):
                restored = _read_named(parent, name, links=(1, 2), attributes=True)
                if (
                    restored.sha256 != data["origin_hash"]
                    or restored.attributes != self._attributes(item, origin=True)
                    or len(restored.raw) != data["origin_size"]
                    or _identity(restored.info) != receipt.temp_identity
                ):
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: restored deletion target changed")
                if temporary is not None:
                    prepared = _read_named(parent, receipt.temp_name, links=(2,), attributes=True)
                    if (
                        prepared.sha256 != restored.sha256
                        or prepared.attributes != restored.attributes
                        or _identity(prepared.info) != receipt.temp_identity
                        or restored.info.st_nlink != 2
                    ):
                        raise WriteError("WRITE_ROLLBACK_CONFLICT: restored deletion links changed")
                elif restored.info.st_nlink != 1:
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: restored deletion links changed")
            return current, temporary, receipt
        if temporary is not None:
            if receipt is None:
                raise WriteError("WRITE_ROLLBACK_UNKNOWN_TEMP: preserve unregistered origin")
            self._prepared_temp(source, item, receipt)
        elif receipt is not None:
            raise WriteError("WRITE_ROLLBACK_CONFLICT: registered origin preparation disappeared")
        elif item["state"] not in {"queued", "preparing"}:
            raise WriteError("WRITE_ROLLBACK_METADATA: deletion restore has unknown progress")
        return current, temporary, receipt

    def _restore_deleted(self, source, task, operation, item):
        data = item["data"]
        current, temporary, receipt = self._deleted_restore_progress(source, item)
        if current is None:
            if temporary is None:
                self._save_item(task, item, "preparing")

                def created(prepared):
                    data["prepared"] = asdict(prepared)
                    self._save_item(task, item, "preparing")

                receipt = prepare_file(
                    source,
                    item["path"],
                    self._blob(data["origin_hash"]),
                    data["origin_mode"],
                    data["temp_name"],
                    created,
                    self._attributes(item, origin=True),
                )
            else:
                self._prepared_temp(source, item, receipt, sync=True)
            self._save_item(task, item, "installing")
            self._source_policy(source, task, operation)
            commit_file(source, receipt, None)  # Atomic no-overwrite creation, not replacement.
            self._source_policy(source, task, operation)
            self._deleted_restore_progress(source, item)
        self._save_item(task, item, "installed")
        self._source_policy(source, task, operation)
        discard_prepared(
            source,
            receipt,
            receipt.sha256,
            receipt.temp_identity,
            self._attributes(item, origin=True),
        )
        data["temp_cleaned"] = True
        self._restore_attributes(source, item)
        final, _ = self._file(source, item["path"])
        if (
            final.sha256 != data["origin_hash"]
            or final.mode != data["origin_mode"]
            or final.version[:2] != receipt.temp_identity
        ):
            raise WriteError("WRITE_ROLLBACK_CONFLICT: deleted file restore changed")
        data["restored_version"] = final.version
        self._save_item(task, item, "done")

    def _remove(self, source, task, operation, item):
        data, directory = item["data"], item["kind"] == "directory"
        current, temp = self._temp(source, item)
        if data.get("latest_deleted"):
            if current is not None or temp is not None:
                raise WriteError("WRITE_ROLLBACK_CONFLICT: deleted creation reappeared")
            self._save_item(task, item, "done")
            return
        receipt = self._removal_receipt(source, item)

        def moved(actual):
            if actual != receipt:
                raise WriteError("WRITE_ROLLBACK_SCOPE: removal receipt binding changed")
            self._source_policy(source, task, operation)
            self._captured(source, item, directory=directory)
            data["isolation_verified"] = True
            data["removed"] = asdict(actual)
            self._save_item(task, item, "isolated")

        if current is None:
            if temp is not None:
                moved(receipt)
                if directory:
                    discard_directory_temp(source, receipt)
                else:
                    discard_removed_file_temp(source, receipt)
            elif not data.get("isolation_verified"):
                raise WriteError(
                    "WRITE_ROLLBACK_UNCONFIRMED_REMOVE: isolated object cannot be verified"
                )
        else:
            if temp is not None:
                raise WriteError("WRITE_ROLLBACK_CONFLICT: removal has competing source entries")
            data["removed"] = asdict(receipt)
            self._save_item(task, item, "removing")
            self._source(source, task["project_id"], local=self._local)
            if directory:
                remove_created_directory(
                    source,
                    item["path"],
                    receipt.directory_identity,
                    receipt.mode,
                    data["temp_name"],
                    moved,
                    data["latest_attributes"],
                )
            else:
                remove_created_file(
                    source, self._document(item), data["temp_name"], moved, self._attributes(item)
                )
        current, temp = self._temp(source, item)
        if current is not None or temp is not None:
            raise WriteError("WRITE_ROLLBACK_CONFLICT: removal postcondition changed")
        self._save_item(task, item, "done")

    def _absent(self, source, item, items):
        removed_dirs = {
            row["path"] for row in items if row["kind"] == "directory" and row["state"] == "done"
        }
        path = item["path"]
        with source.root_fd() as root, ExitStack() as stack:
            if source.scanner._path_problem(
                path, source._ignore(root), item["kind"] == "directory"
            ):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: source policy changed")
            parent, parts = root, path.split("/")
            for index, name in enumerate(parts[:-1]):
                relative = "/".join(parts[: index + 1])
                info = source.scanner._stat(parent, name, relative)
                if info is None and relative in removed_dirs:
                    return
                if info is not None and relative in removed_dirs:
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: removed ancestor reappeared")
                if info is None or not stat.S_ISDIR(info.st_mode):
                    raise WriteError("WRITE_ROLLBACK_SCOPE: an unconfirmed ancestor disappeared")
                parent = stack.enter_context(
                    source.scanner._directory(parent, name, relative, info)
                )
            if source.scanner._stat(parent, parts[-1], path) is not None:
                raise WriteError("WRITE_ROLLBACK_CONFLICT: removed target reappeared")
            if source.scanner._stat(parent, item["data"]["temp_name"], path) is not None:
                raise WriteError("WRITE_ROLLBACK_CONFLICT: completed isolation reappeared")

    def _done(self, source, item, items):
        data = item["data"]
        if item["kind"] == "modified":
            document, parent = self._file(source, item["path"])
            if (
                document.sha256 != data["origin_hash"]
                or document.version != tuple(data["restored_version"])
                or parent != tuple(data["parent_identity"])
            ):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: a completed restore changed")
            if self._temp(source, item)[1] is not None:
                raise WriteError("WRITE_ROLLBACK_CONFLICT: completed swap temporary reappeared")
        else:
            self._absent(source, item, items)
        self._check_attributes(source, item, "after")

    def _preflight(self, source, items):
        """Reconcile the whole restart set before any further native mutation."""
        for item in items:
            if item["state"] == "done":
                self._done(source, item, items)
                continue
            data = item["data"]
            current, temp = self._temp(source, item)
            self._check_attributes(source, item, "before")
            if data.get("latest_deleted"):
                if item["kind"] == "modified":
                    self._deleted_restore_progress(source, item)
                elif current is not None or temp is not None:
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: deleted creation reappeared")
                continue
            if item["kind"] == "modified":
                receipt = self._prepared(source, item)
                document, _ = self._file(source, item["path"])
                if receipt is not None and document.version[:2] == receipt.temp_identity:
                    if (
                        document.sha256 != data["origin_hash"]
                        or document.mode != data["origin_mode"]
                        or (
                            data.get("native_version")
                            and document.version != tuple(data["native_version"])
                        )
                    ):
                        raise WriteError("WRITE_ROLLBACK_CONFLICT: restored target changed")
                    if temp is None and not data.get("capture_verified"):
                        raise WriteError(
                            "WRITE_ROLLBACK_UNCONFIRMED_SWAP: preserve uncertain progress"
                        )
                    if temp is not None:
                        self._captured(source, item, original=True)
                else:
                    if document.sha256 != data["latest_hash"] or document.version != tuple(
                        data["latest_version"]
                    ):
                        raise WriteError("WRITE_ROLLBACK_CONFLICT: latest target changed")
                    if receipt is not None:
                        self._prepared_temp(source, item, receipt)
                    elif temp is not None or item["state"] != "queued":
                        raise WriteError(
                            "WRITE_ROLLBACK_UNKNOWN_TEMP: preserve unregistered progress"
                        )
                continue
            self._removal_receipt(source, item)
            if current is None:
                if not data.get("removed"):
                    raise WriteError("WRITE_ROLLBACK_UNCONFIRMED_REMOVE: no registered removal")
                if temp is not None:
                    self._captured(source, item, directory=item["kind"] == "directory")
                elif not data.get("isolation_verified"):
                    raise WriteError(
                        "WRITE_ROLLBACK_UNCONFIRMED_REMOVE: preserve uncertain progress"
                    )
                continue
            if temp is not None or data.get("isolation_verified"):
                raise WriteError("WRITE_ROLLBACK_CONFLICT: competing or recreated removal target")
            if item["kind"] == "created":
                document, _ = self._file(source, item["path"])
                if document.sha256 != data["latest_hash"] or document.version != tuple(
                    data["latest_version"]
                ):
                    raise WriteError("WRITE_ROLLBACK_CONFLICT: created target changed")
            else:
                children = set()
                for child in items:
                    parent, _, name = child["path"].rpartition("/")
                    if parent == item["path"] and child["state"] != "done":
                        target, isolated = self._temp(source, child)
                        if target is not None:
                            children.add(name)
                        if isolated is not None and (
                            child["data"].get("prepared") or child["data"].get("removed")
                        ):
                            children.add(child["data"]["temp_name"])
                self._directory(
                    source,
                    {
                        "path": item["path"],
                        "directory_identity": encode_metadata(data["directory_binding"]),
                    },
                    children,
                )

    def _run(self, source, task, operation, *, local):
        self._local = local  # Calls are serialized by the coordinator lock.
        self._source(source, task["project_id"], local=local)
        if operation["task_id"] != task["task_id"] or source.source_id != task["source_id"]:
            raise WriteError("WRITE_ROLLBACK_SCOPE: operation belongs to another task/source")
        summary = _metadata(operation["metadata"])
        if (
            summary["kind"] != "rollback"
            or summary["source_id"] != source.source_id
            or summary["phase"] not in {"backing_up", "ready", "done"}
        ):
            raise WriteError("WRITE_ROLLBACK_SCOPE: invalid rollback operation binding")
        items = self._items(task, operation)
        self._source_policy(source, task, operation)
        if summary["phase"] == "backing_up":
            if any(item["state"] != "queued" for item in items):
                raise WriteError("WRITE_ROLLBACK_METADATA: backup phase has native progress")
            self._backups(source, task, operation, items)
        for item in items:
            if (
                not item["data"]["backup_verified"]
                and item["kind"] != "directory"
                or not item["data"]["origin_verified"]
            ):
                raise WriteError("WRITE_ROLLBACK_METADATA: recovery materials are not verified")
            if item["kind"] != "directory":
                if not item["data"].get("latest_deleted"):
                    self._blob(item["data"]["latest_hash"])
                if item["kind"] == "modified":
                    self._blob(item["data"]["origin_hash"])
                refs = self.store.query(
                    "SELECT sha256 FROM object_refs WHERE owner=?", (item["data"]["backup_owner"],)
                )
                if not item["data"].get("latest_deleted") and (
                    not refs or refs[0]["sha256"] != item["data"]["latest_hash"]
                ):
                    raise WriteError("WRITE_ROLLBACK_METADATA: required latest owner is missing")
        self._preflight(source, items)
        self._source_policy(source, task, operation)
        for item in items:
            self._source_policy(source, task, operation)
            if item["state"] == "done":
                self._done(source, item, items)
                continue
            self._check_attributes(source, item, "before")
            if item["kind"] == "modified":
                self._restore(source, task, operation, item)
            else:
                self._remove(source, task, operation, item)
            self._source_policy(source, task, operation)
        for item in items:
            self._done(source, item, items)
        self._source_policy(source, task, operation)
        result = {
            "project_id": task["project_id"],
            "task_id": task["task_id"],
            "state": "rolled_back",
            "files_restored": sum(row["kind"] == "modified" for row in items),
            "files_removed": sum(
                row["kind"] == "created" and not row["data"].get("latest_deleted") for row in items
            ),
            "directories_removed": sum(row["kind"] == "directory" for row in items),
            "source_mode": "live",
            "readback_verified": True,
            "attribute_support": "verified_necessary_attributes",
            "index_status": "revalidate_on_next_query",
        }
        summary["phase"] = "done"
        with self.store.transaction() as db:
            db.execute(
                "UPDATE operations SET state='done',metadata=?,result=? "
                "WHERE task_id=? AND request_id=?",
                (
                    encode_metadata(summary),
                    encode_metadata(result),
                    task["task_id"],
                    operation["request_id"],
                ),
            )
            db.execute(
                "UPDATE tasks SET state='rolled_back',completed=? WHERE task_id=?",
                (self.c.clock(), task["task_id"]),
            )
        for old in self.store.query(
            "SELECT * FROM tasks WHERE state IN ('completed','rolled_back') AND task_id!=?",
            (task["task_id"],),
        ):
            self.c._retire(old)
        try:
            self.c.on_change(task["project_id"])
        except Exception:
            pass
        return result

    def _pending(self, task, operation):
        self.c.stop_requested.set()
        self.c.grants = {}
        try:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE tasks SET state='rolling_back' WHERE task_id=?", (task["task_id"],)
                )
                db.execute(
                    "UPDATE operations SET state='rollback_prepared' "
                    "WHERE task_id=? AND request_id=?",
                    (task["task_id"], operation["request_id"]),
                )
        except Exception:
            pass  # Original durable rollback intent still protects interrupted work.

    def rollback(self, project_id, task_id, request_id):
        validate_request(request_id)
        digest = request_digest({"kind": "rollback", "project": project_id, "task_id": task_id})
        with self.c.lock:
            source = self.c._authorized(project_id)
            task = self.c._task(project_id, task_id, source)
            previous = self.store.query(
                "SELECT * FROM operations WHERE task_id=? AND request_id=?", (task_id, request_id)
            )
            if previous:
                if previous[0]["digest"] != digest:
                    raise WriteError("REQUEST_ID_CONFLICT: same identifier has different content")
                if previous[0]["state"] == "done":
                    return _metadata(previous[0]["result"])
                raise WriteError(
                    "WRITE_ROLLBACK_RECOVERY_REQUIRED: resume the recorded rollback locally"
                )
            self._eligible(task)
            with source.lock:
                plan = self._precheck(source, task)
                self._reserve(source, task, plan)
                self._source(source, project_id, local=False)
                self._record(source, task, request_id, digest, plan)
                operation = self.store.query(
                    "SELECT * FROM operations WHERE task_id=? AND request_id=?",
                    (task_id, request_id),
                )[0]
                self.c.inflight_project = project_id
                try:
                    return self._run(source, task, operation, local=False)
                except BaseException:
                    self._pending(task, operation)
                    raise WriteError(
                        "WRITE_ROLLBACK_RECOVERY_REQUIRED: preserve progress; resume locally"
                    ) from None
                finally:
                    self.c.inflight_project = None

    def resume(self, source, task, operation):
        """Internal authenticated-local recovery; never grant new general writes."""
        if operation["task_id"] != task["task_id"]:
            raise WriteError("WRITE_ROLLBACK_SCOPE: local operation belongs to another task")
        self.c.disable()
        with self.c.lock, source.lock:
            # Validate the supplied IDs against durable records before protecting
            # anything. A mismatched local caller must not rewrite another task.
            task = self.c._task(task["project_id"], task["task_id"], source)
            rows = self.store.query(
                "SELECT * FROM operations WHERE task_id=? AND request_id=?",
                (task["task_id"], operation["request_id"]),
            )
            if not rows:
                raise WriteError("WRITE_ROLLBACK_METADATA: missing durable rollback operation")
            operation = rows[0]
            expected = request_digest(
                {"kind": "rollback", "project": task["project_id"], "task_id": task["task_id"]}
            )
            if operation["digest"] != expected:
                raise WriteError("WRITE_ROLLBACK_SCOPE: request binding changed")
            if operation["state"] == "done" and task["state"] == "rolled_back":
                self._source(source, task["project_id"], local=True)
                return _metadata(operation["result"])
            if task["state"] != "rolling_back" or operation["state"] != "rollback_prepared":
                raise WriteError("WRITE_ROLLBACK_METADATA: expected a protected rollback intent")
            self.c.inflight_project = task["project_id"]
            try:
                return self._run(source, task, operation, local=True)
            except BaseException:
                self._pending(task, operation)
                raise WriteError(
                    "WRITE_ROLLBACK_RECOVERY_REQUIRED: preserve progress; inspect locally"
                ) from None
            finally:
                self.c.inflight_project = None

    def verify_restored(self, source, task):
        """Diff may say zero only after proving every actual restored member."""
        operations = self.store.query(
            "SELECT * FROM operations WHERE task_id=? AND state='done' ORDER BY sequence DESC",
            (task["task_id"],),
        )
        operation = next(
            (row for row in operations if _metadata(row["metadata"]).get("kind") == "rollback"),
            None,
        )
        if operation is None or task["state"] != "rolled_back":
            raise WriteError("WRITE_ROLLBACK_METADATA: restored proof unavailable")
        items = self._items(task, operation)
        if any(row["state"] != "done" for row in items):
            raise WriteError("WRITE_ROLLBACK_METADATA: incomplete restored proof")
        for item in items:
            self._done(source, item, items)
        source.ensure_available()

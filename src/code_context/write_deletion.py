"""Task-scoped file deletion with a saved origin and provable local recovery.

An absent last_hash is a tombstone, not an empty file. Existing task origins and
attributes stay bounded by the same recovery store. The native helper isolates
the exact verified inode before unlinking; no recursive/system deletion exists.
"""

import json
import os
import uuid
from dataclasses import asdict

from code_context.file_mutation import _read_named, read_file_attributes
from code_context.file_removal import RemovedFile, discard_removed_file_temp, remove_created_file
from code_context.recovery_store import encode_metadata
from code_context.scanner import _version
from code_context.write_attributes import (
    recorded_attributes,
    task_attributes,
    verify_created_parents,
)
from code_context.write_coordinator import WriteError, request_digest, validate_request
from code_context.write_operations import SHA, allocated


def verify_deleted(source, row):
    """Prove absence under the same parent; never adopt a replaced directory."""
    try:
        binding = json.loads(row["directory_identity"])["deleted_parent_identity"]
        if (
            row["last_hash"] is not None
            or row["last_version"] is not None
            or not isinstance(binding, list)
            or len(binding) != 2
            or any(type(value) is not int or value < 0 for value in binding)
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise WriteError("WRITE_DELETE_METADATA: missing absence binding") from None
    with source.parent_fd(row["path"]) as (parent, name):
        if (os.fstat(parent).st_dev, os.fstat(parent).st_ino) != tuple(
            binding
        ) or source.scanner._stat(parent, name, row["path"]) is not None:
            raise WriteError("WRITE_DELETE_CONFLICT: deleted target or parent changed")
    source.ensure_available()
    return tuple(binding)


class WriteDeletion:
    def __init__(self, coordinator):
        self.c, self.store = coordinator, coordinator.store

    def _entries(self, source, metadata):
        receipt = self._receipt(source, metadata)
        with source.parent_fd(metadata["path"]) as (parent, name):
            if (os.fstat(parent).st_dev, os.fstat(parent).st_ino) != receipt.parent_identity:
                raise WriteError("WRITE_DELETE_CONFLICT: source parent changed")
            return (
                source.scanner._stat(parent, name, metadata["path"]),
                source.scanner._stat(parent, receipt.temp_name, metadata["path"]),
            )

    @staticmethod
    def _receipt(source, metadata):
        try:
            record = dict(metadata["removed"])
            for field in ("parent_identity", "file_identity"):
                record[field] = tuple(record[field])
            receipt = RemovedFile(**record)
            if (
                receipt.path != metadata["path"]
                or receipt.temp_name != metadata["temp_name"]
                or receipt.source_id != source.source_id
                or receipt.sha256 != metadata["before_hash"]
                or receipt.file_identity != tuple(metadata["before_version"][:2])
                or receipt.mode != metadata["before_mode"]
                or receipt.size != metadata["before_version"][3]
            ):
                raise ValueError
            return receipt
        except (TypeError, KeyError, ValueError):
            raise WriteError("WRITE_RECOVERY_METADATA: invalid deletion receipt") from None

    def _attributes(self, operation, receipt):
        rows = self.store.query(
            "SELECT before_record,after_record FROM operation_attributes "
            "WHERE task_id=? AND request_id=?",
            (operation["task_id"], operation["request_id"]),
        )
        if not rows or rows[0]["before_record"] != rows[0]["after_record"]:
            raise WriteError("WRITE_RECOVERY_METADATA: deletion attributes unavailable")
        attrs = recorded_attributes(rows[0]["before_record"])
        if attrs.sha256 != receipt.attribute_sha256:
            raise WriteError("WRITE_RECOVERY_METADATA: deletion attributes changed")
        return attrs

    def _isolated(self, source, receipt, attrs):
        with source.parent_fd(receipt.path) as (parent, name):
            actual = _read_named(parent, receipt.temp_name, attributes=True)
            if (
                source.scanner._stat(parent, name, receipt.path) is not None
                or (actual.info.st_dev, actual.info.st_ino) != receipt.file_identity
                or actual.sha256 != receipt.sha256
                or actual.attributes != attrs
                or len(actual.raw) != receipt.size
            ):
                raise WriteError("WRITE_DELETE_CONFLICT: isolated file changed")

    def _complete(self, source, task, operation, metadata, attrs):
        if self._entries(source, metadata) != (None, None) or not metadata.get(
            "isolation_verified"
        ):
            raise WriteError("WRITE_DELETE_CONFLICT: deletion outcome is unconfirmed")
        result = {
            "project_id": task["project_id"],
            "task_id": task["task_id"],
            "path": metadata["path"],
            "state": "deleted",
            "deleted_sha256": metadata["before_hash"],
            "source_mode": "live",
            "readback_verified": True,
            "recovery_point_retained": True,
            "index_status": "revalidate_on_next_query",
        }
        record = encode_metadata(attrs.to_record())
        metadata["phase"] = "done"
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT * FROM files WHERE task_id=? AND path=?",
                (task["task_id"], metadata["path"]),
            ).fetchone()
            if previous is None:
                if not metadata["first_touch"]:
                    raise WriteError("WRITE_RECOVERY_METADATA: deletion participant disappeared")
                db.execute(
                    "INSERT INTO files VALUES(?,?, 'modified',?,?,NULL,NULL,?)",
                    (
                        task["task_id"],
                        metadata["path"],
                        metadata["before_hash"],
                        metadata["before_mode"],
                        encode_metadata(
                            {"deleted_parent_identity": metadata["removed"]["parent_identity"]}
                        ),
                    ),
                )
            else:
                if previous["kind"] not in {"modified", "created"} or (
                    previous["last_hash"] is not None
                    and (
                        previous["last_hash"] != metadata["before_hash"]
                        or json.loads(previous["last_version"]) != metadata["before_version"]
                    )
                ):
                    raise WriteError("WRITE_RECOVERY_METADATA: deletion participant changed")
                db.execute(
                    "UPDATE files SET last_hash=NULL,last_version=NULL,directory_identity=? "
                    "WHERE task_id=? AND path=?",
                    (
                        encode_metadata(
                            {"deleted_parent_identity": metadata["removed"]["parent_identity"]}
                        ),
                        task["task_id"],
                        metadata["path"],
                    ),
                )
            db.execute(
                "INSERT INTO file_attributes VALUES(?,?,?,?) ON CONFLICT(task_id,path) "
                "DO UPDATE SET last_record=excluded.last_record",
                (
                    task["task_id"],
                    metadata["path"],
                    record if metadata["first_touch"] else None,
                    record,
                ),
            )
            db.execute(
                "UPDATE operations SET state='done',metadata=?,result=? "
                "WHERE task_id=? AND request_id=?",
                (
                    encode_metadata(metadata),
                    encode_metadata(result),
                    task["task_id"],
                    operation["request_id"],
                ),
            )
        self.c.recovery._release(task["task_id"], operation["request_id"], metadata)
        try:
            self.c.on_change(task["project_id"])
        except Exception:
            pass
        return result

    def delete_file(self, project, task_id, request_id, path, expected_sha256):
        validate_request(request_id)
        operations = self.c.operations
        operations._path(path)
        if not isinstance(expected_sha256, str) or SHA.fullmatch(expected_sha256) is None:
            raise WriteError("INVALID_EXPECTED_HASH: use the full hash returned by a source read")
        digest = request_digest(
            {"kind": "delete_file", "project": project, "path": path, "sha": expected_sha256}
        )
        with self.c.lock:
            source, task, replay = operations._start(project, task_id, request_id, digest)
            if replay is not None:
                return replay
            with source.lock:
                before = source.read(path)
                if before.sha256 != expected_sha256:
                    raise WriteError("WRITE_HASH_CONFLICT: read the current file before deleting")
                previous = operations._previous(project, task_id, path, before)
                verify_created_parents(self.c, task_id, source, path)
                attrs = read_file_attributes(source, before)
                if previous is not None and attrs != task_attributes(self.c, task_id, path):
                    raise WriteError("WRITE_ATTRIBUTE_CONFLICT: source attributes changed")
                record = encode_metadata(attrs.to_record())
                missing = (
                    0
                    if self.store.query(
                        "SELECT sha256 FROM objects WHERE sha256=?", (before.sha256,)
                    )
                    else allocated(before.size)
                )
                self.c.reserve_growth(
                    task_id,
                    object_bytes=missing,
                    source_temp_bytes=allocated(before.size),
                    metadata_bytes=65536 + 4 * len(record.encode()),
                    additional_files=int(previous is None),
                    source=source,
                )
                temporary = ".colink-write-" + uuid.uuid4().hex + ".tmp"
                with source.parent_fd(path) as (parent, name):
                    info = source.scanner._stat(parent, name, path)
                    if info is None or _version(info) != before.version:
                        raise WriteError("WRITE_DELETE_CONFLICT: source changed before intent")
                    receipt = RemovedFile(
                        path,
                        temporary,
                        (os.fstat(parent).st_dev, os.fstat(parent).st_ino),
                        before.version[:2],
                        before.mode,
                        before.sha256,
                        before.size,
                        source.source_id,
                        attrs.sha256,
                    )
                metadata = {
                    "kind": "delete_file",
                    "path": path,
                    "phase": "backing_up",
                    "temp_name": temporary,
                    "before_hash": before.sha256,
                    "before_version": list(before.version),
                    "before_mode": before.mode,
                    "first_touch": previous is None,
                    "removed": asdict(receipt),
                    "isolation_verified": False,
                }
                operations._record(task_id, request_id, digest, metadata)
                operation = {"task_id": task_id, "request_id": request_id}
                self.c.inflight_project = project
                try:
                    with self.store.transaction() as db:
                        db.execute(
                            "INSERT INTO operation_attributes VALUES(?,?,?,?)",
                            (task_id, request_id, record, record),
                        )
                    self.store.put_blob(
                        before.content.encode(), f"{task_id}:op:{request_id}:before"
                    )
                    if previous is None:
                        self.store.put_blob(before.content.encode(), f"{task_id}:origin:{path}")
                    self.c._authorized(project, _allow_pending=True)
                    metadata["phase"] = "removing"
                    operations._metadata(task_id, request_id, metadata, state="committing")

                    def moved(actual):
                        if actual != receipt:
                            raise WriteError("WRITE_DELETE_CONFLICT: native receipt changed")
                        self.c._authorized(project, _allow_pending=True)
                        self._isolated(source, receipt, attrs)
                        metadata.update(phase="isolated", isolation_verified=True)
                        operations._metadata(task_id, request_id, metadata)

                    remove_created_file(source, before, temporary, moved, attrs)
                    return self._complete(source, task, operation, metadata, attrs)
                except BaseException:
                    operations._protect(task_id)
                    raise WriteError(
                        "WRITE_RECOVERY_REQUIRED: preserve deletion; recover locally"
                    ) from None
                finally:
                    self.c.inflight_project = None

    def recover(self, source, task, operation, metadata):
        """Only the existing authenticated local recovery dispatcher calls this."""
        receipt = self._receipt(source, metadata)
        current, temporary = self._entries(source, metadata)
        if current is not None:
            if temporary is not None or metadata.get("isolation_verified"):
                raise WriteError("WRITE_DELETE_CONFLICT: deleted path reappeared")
            document = source.read(metadata["path"])
            if document.sha256 != receipt.sha256 or document.version != tuple(
                metadata["before_version"]
            ):
                raise WriteError("WRITE_DELETE_CONFLICT: original source changed")
            # A crash before attributes/backups are recorded can safely abort;
            # the unchanged full source version must still match the intent.
            rows = self.store.query(
                "SELECT * FROM operation_attributes WHERE task_id=? AND request_id=?",
                (task["task_id"], operation["request_id"]),
            )
            if rows and read_file_attributes(source, document) != self._attributes(
                operation, receipt
            ):
                raise WriteError("WRITE_ATTRIBUTE_CONFLICT: deletion origin attributes changed")
            return self.c.recovery._abort(operation, metadata)
        attrs = self._attributes(operation, receipt)
        refs = self.store.query(
            "SELECT sha256 FROM object_refs WHERE owner=?",
            (f"{task['task_id']}:op:{operation['request_id']}:before",),
        )
        if (
            not refs
            or refs[0]["sha256"] != receipt.sha256
            or len(self.store.read_blob(receipt.sha256)) != receipt.size
        ):
            raise WriteError("WRITE_RECOVERY_METADATA: deletion backup unavailable")
        if metadata["first_touch"]:
            refs = self.store.query(
                "SELECT sha256 FROM object_refs WHERE owner=?",
                (f"{task['task_id']}:origin:{metadata['path']}",),
            )
            if not refs or refs[0]["sha256"] != receipt.sha256:
                raise WriteError("WRITE_RECOVERY_METADATA: deletion origin ownership unavailable")
        if temporary is not None:
            self._isolated(source, receipt, attrs)
            metadata.update(phase="isolated", isolation_verified=True)
            self.c.operations._metadata(task["task_id"], operation["request_id"], metadata)
            if not self.c._alive():
                raise WriteError("LOCAL_CONTROL_UNAVAILABLE: deletion recovery stopped")
            discard_removed_file_temp(source, receipt)
        elif not metadata.get("isolation_verified"):
            raise WriteError("WRITE_DELETE_CONFLICT: missing target has no verified removal")
        return self._complete(source, task, operation, metadata, attrs)

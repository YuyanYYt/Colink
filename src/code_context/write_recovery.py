"""Local-only reconciliation of provable interrupted file/directory operations.

Recovery never grants writes, guesses an unknown inode, overwrites an external
edit or adopts incomplete objects. An unregistered/partial/tampered object stays
pending for local inspection. Safe known pre-install attempts may be aborted;
verified installed content may be confirmed without repeating the mutation.
Whole-task rollback recovery is attached separately.
"""

import json
import os
import stat

from code_context.directory_mutation import PreparedDirectory, discard_directory_temp
from code_context.file_mutation import PreparedFile, _read_named, discard_prepared
from code_context.recovery_store import encode_metadata
from code_context.source_access import SourceDocument
from code_context.write_coordinator import WriteError


def prepared_file(data):
    try:
        value = dict(data)
        for key in ("parent_identity", "temp_identity"):
            value[key] = tuple(value[key])
        return PreparedFile(**value)
    except (TypeError, KeyError, ValueError):
        raise WriteError("WRITE_RECOVERY_METADATA: preserve invalid file receipt") from None


def prepared_directory(data):
    try:
        value = dict(data)
        for key in ("parent_identity", "directory_identity"):
            value[key] = tuple(value[key])
        return PreparedDirectory(**value)
    except (TypeError, KeyError, ValueError):
        raise WriteError("WRITE_RECOVERY_METADATA: preserve invalid directory receipt") from None


class WriteRecovery:
    def __init__(self, coordinator):
        self.c, self.store = coordinator, coordinator.store

    def _current(self, source, path, *, directory=False):
        with source.parent_fd(path, directory=directory) as (parent, name):
            info = source.scanner._stat(parent, name, path)
            if info is None:
                return None
            if directory:
                return info
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink not in {1, 2}
                or stat.S_IMODE(info.st_mode) > 0o777
            ):
                raise WriteError("WRITE_RECOVERY_CONFLICT: source ownership or type changed")
            return source.read(path)

    def _temp(self, source, metadata, *, directory=False):
        with source.parent_fd(metadata["path"], directory=directory) as (parent, _):
            return source.scanner._stat(parent, metadata["temp_name"], metadata["path"])

    def _binding(self, source, metadata, receipt, *, directory=False):
        if (
            receipt.path != metadata["path"]
            or receipt.temp_name != metadata["temp_name"]
            or receipt.source_id != source.source_id
        ):
            raise WriteError("WRITE_RECOVERY_METADATA: receipt and operation bindings disagree")
        with source.parent_fd(metadata["path"], directory=directory) as (parent, _):
            info = os.fstat(parent)
            if (info.st_dev, info.st_ino) != receipt.parent_identity:
                raise WriteError("WRITE_RECOVERY_SCOPE: the registered parent was replaced")

    def _verify_captured(self, source, receipt, before):
        """Verify the real exchanged object before labeling a swap confirmed."""
        with source.parent_fd(receipt.path) as (parent, target):
            actual = _read_named(parent, receipt.temp_name, links=(1,) if before else (1, 2))
            expected_sha = before.sha256 if before else receipt.sha256
            expected_identity = before.version[:2] if before else receipt.temp_identity
            expected_mode = before.mode if before else receipt.mode
            if (
                actual.sha256 != expected_sha
                or (actual.info.st_dev, actual.info.st_ino) != expected_identity
                or stat.S_IMODE(actual.info.st_mode) != expected_mode
            ):
                raise WriteError("WRITE_RECOVERY_CONFLICT: captured content was not confirmed")
            if actual.info.st_nlink == 2:
                linked = _read_named(parent, target, links=(2,))
                if linked.sha256 != actual.sha256 or linked.info.st_ino != actual.info.st_ino:
                    raise WriteError("WRITE_RECOVERY_CONFLICT: installation links no longer agree")

    def _confirm_objects(self, task_id, request_id):
        prefix = f"{task_id}:op:{request_id}:"
        rows = self.store.query(
            "SELECT DISTINCT objects.* FROM object_refs JOIN objects USING(sha256) "
            "WHERE substr(owner,1,?)=?",
            (len(prefix), prefix),
        )
        for row in rows:
            if row["state"] == "ready":
                self.store.read_blob(row["sha256"])
                continue
            if row["state"] != "pending":
                raise WriteError("WRITE_RECOVERY_OBJECT: preserve unexpected protected object")
            with self.store.objects.root_fd() as parent:
                try:
                    os.stat(row["sha256"], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Registered intent, never created; abort may release it.
            self.store.read_blob(row["sha256"], _recovering=True)
            with self.store.transaction() as db:
                db.execute("UPDATE objects SET state='ready' WHERE sha256=?", (row["sha256"],))

    def _release(self, task_id, request_id, metadata, *, aborted=False):
        prefix = f"{task_id}:op:{request_id}:"
        owners = [prefix + "before", prefix + "after"]
        if (
            aborted
            and metadata.get("first_touch")
            and not self.store.query(
                "SELECT path FROM files WHERE task_id=? AND path=?", (task_id, metadata["path"])
            )
        ):
            owners.append(f"{task_id}:origin:{metadata['path']}")
        with self.store.transaction() as db:
            db.executemany("DELETE FROM object_refs WHERE owner=?", ((owner,) for owner in owners))
        self.store.collect_unreferenced()

    def _abort(self, operation, metadata):
        result = {"state": "aborted", "source_mutation_repeated": False}
        metadata["phase"] = "aborted"
        with self.store.transaction() as db:
            db.execute(
                "UPDATE operations SET state='aborted',metadata=?,result=? "
                "WHERE task_id=? AND request_id=?",
                (
                    encode_metadata(metadata),
                    encode_metadata(result),
                    operation["task_id"],
                    operation["request_id"],
                ),
            )
        self._release(operation["task_id"], operation["request_id"], metadata, aborted=True)
        return result

    def _recover_file(self, source, task, operation, metadata):
        current = self._current(source, metadata["path"])
        temp = self._temp(source, metadata)
        receipt = prepared_file(metadata["prepared"]) if metadata.get("prepared") else None
        if receipt is not None:
            self._binding(source, metadata, receipt)
        before_matches = (current is None and metadata["before_hash"] is None) or (
            current is not None
            and current.sha256 == metadata["before_hash"]
            and current.version == tuple(metadata["before_version"])
            and current.mode == metadata["before_mode"]
        )
        if before_matches:
            if temp is not None:
                if receipt is None:
                    raise WriteError("WRITE_RECOVERY_UNKNOWN_TEMP: preserve an unregistered inode")
                if stat.S_IMODE(temp.st_mode) not in {receipt.mode, 0o600}:
                    raise WriteError("WRITE_RECOVERY_CONFLICT: temporary permissions changed")
                discard_prepared(source, receipt, receipt.sha256, receipt.temp_identity)
            return self._abort(operation, metadata)
        if (
            receipt is None
            or current is None
            or current.version[:2] != receipt.temp_identity
            or current.sha256 != metadata["after_hash"]
            or current.size != metadata["after_size"]
            or current.mode != metadata["mode"]
        ):
            raise WriteError(
                "WRITE_RECOVERY_CONFLICT: current source is not a provable operation state"
            )
        if metadata["before_hash"] is not None:
            if temp is None and metadata["phase"] not in {"installed", "done"}:
                raise WriteError(
                    "WRITE_RECOVERY_UNCONFIRMED_SWAP: displaced source cannot be verified"
                )
            raw = self.store.read_blob(metadata["before_hash"])
            before = SourceDocument(
                metadata["path"],
                raw.decode("utf-8"),
                metadata["before_hash"],
                len(raw),
                metadata["before_mode"],
                tuple(metadata["before_version"]),
            )
        else:
            before = None
        # A temp's mode change is a conflict, not authority to silently delete it.
        if temp is not None and stat.S_IMODE(temp.st_mode) != (
            before.mode if before else receipt.mode
        ):
            raise WriteError("WRITE_RECOVERY_CONFLICT: registered temporary permissions changed")
        if temp is not None:
            self._verify_captured(source, receipt, before)
        metadata["phase"] = "installed"
        self.c.operations._metadata(operation["task_id"], operation["request_id"], metadata)
        previous = self.c.operations._file(operation["task_id"], metadata["path"])
        if previous is None and not metadata["first_touch"]:
            raise WriteError("WRITE_RECOVERY_METADATA: missing earlier task ownership")
        self.c.operations._touch(operation["task_id"], metadata["path"], before, previous, current)
        if temp is not None:
            discard_prepared(
                source,
                receipt,
                before.sha256 if before else receipt.sha256,
                before.version[:2] if before else receipt.temp_identity,
            )
        final = source.read(metadata["path"])
        if (
            final.sha256 != current.sha256
            or final.version[:2] != current.version[:2]
            or final.mode != current.mode
        ):
            raise WriteError("WRITE_RECOVERY_CONFLICT: source changed during confirmation")
        self.c.operations._touch(
            operation["task_id"],
            metadata["path"],
            before,
            self.c.operations._file(operation["task_id"], metadata["path"]),
            final,
        )
        result = {
            "project_id": task["project_id"],
            "task_id": task["task_id"],
            "path": metadata["path"],
            "state": "saved",
            "sha256": final.sha256,
            "size": final.size,
            "source_mode": "live",
            "source_is_untrusted": True,
            "preview": metadata["preview"],
            "readback_verified": True,
            "index_status": "revalidate_on_next_query",
        }
        metadata["phase"] = "done"
        with self.store.transaction() as db:
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
        self._release(operation["task_id"], operation["request_id"], metadata)
        return {"state": "saved", "source_mutation_repeated": False}

    def _recover_directory(self, source, task, operation, metadata):
        current = self._current(source, metadata["path"], directory=True)
        temp = self._temp(source, metadata, directory=True)
        receipt = prepared_directory(metadata["prepared"]) if metadata.get("prepared") else None
        if receipt is not None:
            self._binding(source, metadata, receipt, directory=True)
        if current is None:
            if temp is not None:
                if receipt is None:
                    raise WriteError(
                        "WRITE_RECOVERY_UNKNOWN_TEMP: preserve an unregistered directory"
                    )
                discard_directory_temp(source, receipt)
            return self._abort(operation, metadata)
        if (
            receipt is None
            or temp is not None
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != receipt.directory_identity
            or stat.S_IMODE(current.st_mode) != receipt.mode
            or current.st_uid != os.geteuid()
        ):
            raise WriteError("WRITE_RECOVERY_CONFLICT: directory installation cannot be confirmed")
        result = {
            "project_id": task["project_id"],
            "task_id": task["task_id"],
            "path": metadata["path"],
            "state": "created",
            "kind": "directory",
            "source_mode": "live",
            "ownership_recorded": True,
        }
        metadata["phase"] = "done"
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO files VALUES(?,?,?,NULL,NULL,NULL,NULL,?)",
                (
                    task["task_id"],
                    metadata["path"],
                    "directory",
                    encode_metadata(
                        {
                            "identity": receipt.directory_identity,
                            "mode": receipt.mode,
                            "parent_identity": receipt.parent_identity,
                        }
                    ),
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
        return {"state": "created", "source_mutation_repeated": False}

    def recover(self, project):
        # Local entry invalidates any former grant even if it subsequently fails.
        self.c.disable()
        with self.c.lock:
            if not self.c._alive():
                raise WriteError(
                    "LOCAL_CONTROL_UNAVAILABLE: recover only on an active local connection"
                )
            source = self.c.source_provider(project)
            source.ensure_available()
            tasks = self.store.query(
                "SELECT * FROM tasks WHERE project_id=? "
                "AND state IN ('active','recovery_required','rolling_back')",
                (project,),
            )
            task = tasks[0] if tasks else None
            if task is None:
                return {"state": "no_pending_operation", "write_enabled": False}
            if task["source_id"] != source.source_id:
                raise WriteError(
                    "WRITE_RECOVERY_SCOPE: reauthorize the original source, not its replacement"
                )
            if task["state"] == "rolling_back":
                raise WriteError(
                    "WRITE_ROLLBACK_RECOVERY_NOT_READY: whole-task recovery is not attached"
                )
            self.c.inflight_project = project
            try:
                outcomes = []
                with source.lock:
                    for operation in self.store.query(
                        "SELECT * FROM operations WHERE task_id=? "
                        "AND state IN ('prepared','committing') ORDER BY sequence",
                        (task["task_id"],),
                    ):
                        if not self.c._alive():
                            raise WriteError(
                                "LOCAL_CONTROL_UNAVAILABLE: recovery authorization was lost"
                            )
                        metadata = json.loads(operation["metadata"])
                        scope = json.loads(task["metadata"])["scope"]
                        if scope is not None and metadata["path"] not in scope:
                            raise WriteError(
                                "WRITE_RECOVERY_SCOPE: operation exceeds its task scope"
                            )
                        self._confirm_objects(task["task_id"], operation["request_id"])
                        if metadata["kind"] in {"apply_edit", "create_file"}:
                            outcome = self._recover_file(source, task, operation, metadata)
                        elif metadata["kind"] == "create_directory":
                            outcome = self._recover_directory(source, task, operation, metadata)
                        else:
                            raise WriteError(
                                "WRITE_RECOVERY_METADATA: unsupported pending operation"
                            )
                        outcomes.append(outcome)
                    self.store.collect_unreferenced()
                    with self.store.transaction() as db:
                        db.execute(
                            "UPDATE tasks SET state='active' WHERE task_id=?", (task["task_id"],)
                        )
                    try:
                        self.c.on_change(project)
                    except Exception:
                        pass
                    return {
                        "state": "recovered",
                        "task_id": task["task_id"],
                        "outcomes": outcomes,
                        "write_enabled": False,
                    }
            except BaseException:
                self.c.operations._protect(task["task_id"])
                raise WriteError(
                    "WRITE_RECOVERY_CONFLICT: preserve materials; local inspection is required"
                ) from None
            finally:
                self.c.inflight_project = None

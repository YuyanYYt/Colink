"""Bounded task-scoped batches, with durable identities and sequential recovery.

The full input is hash-bound, never copied into a second source store. Every
mutation uses the original single-operation journal, receipts and object refs.
Read-only preflight reduces avoidable partial results; external editors and
permission changes still prevent this from being a multi-file transaction.
"""

import hashlib
import json
import re
import stat
import unicodedata
from contextlib import ExitStack
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from code_context.file_attributes import MAX_RECORD_BYTES
from code_context.file_mutation import read_file_attributes
from code_context.policy import MAX_FILE_BYTES, content_problem, validate_path
from code_context.recovery_store import encode_metadata
from code_context.source_access import SourceError
from code_context.text_edits import apply_text_edit
from code_context.write_attributes import (
    directory_attributes,
    task_attributes,
    verify_created_parents,
)
from code_context.write_coordinator import (
    MAX_OPERATIONS,
    WriteError,
    request_digest,
    validate_request,
)
from code_context.write_operations import allocated

MAX_BATCHES = 128
MAX_BATCH_ITEMS = 20
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")


class BatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class InsertLines(BatchModel):
    kind: Literal["insert_lines"]
    line: int = Field(ge=1)
    position: Literal["before", "after"]
    text: str
    expected_context: str


class ReplaceLines(BatchModel):
    kind: Literal["replace_lines"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    old_text: str
    new_text: str


class ReplaceFragment(BatchModel):
    kind: Literal["replace_fragment"]
    old_text: str
    new_text: str


BatchTextEdit = Annotated[InsertLines | ReplaceLines | ReplaceFragment, Field(discriminator="kind")]


class BatchPath(BatchModel):
    path: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def relative_path(self):
        validate_path(self.path)
        return self


class BatchEdit(BatchPath):
    kind: Literal["apply_edit"]
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    edit: BatchTextEdit


class BatchCreateFile(BatchPath):
    kind: Literal["create_file"]
    content: str


class BatchCreateDirectory(BatchPath):
    kind: Literal["create_directory"]


BatchOperation = Annotated[
    BatchEdit | BatchCreateFile | BatchCreateDirectory, Field(discriminator="kind")
]
BatchOperations = Annotated[list[BatchOperation], Field(min_length=1, max_length=MAX_BATCH_ITEMS)]
_OPERATIONS = TypeAdapter(BatchOperations)


def batch_operations(operations):
    try:
        items = _OPERATIONS.validate_python(operations, strict=True)
        paths = [unicodedata.normalize("NFC", item.path).casefold() for item in items]
        if len(set(paths)) != len(paths):
            raise ValueError
        return [item.model_dump() for item in items]
    except (ValidationError, ValueError, TypeError):
        raise WriteError(
            "INVALID_WRITE_BATCH: use 1-20 strict edit/file/directory operations with unique paths"
        ) from None


def operation_digest(project, item):
    if item["kind"] == "apply_edit":
        parameters = {
            "kind": "apply_edit",
            "project": project,
            "path": item["path"],
            "sha": item["expected_sha256"],
            "edit": item["edit"],
        }
    else:
        parameters = {"kind": item["kind"], "project": project, "path": item["path"]}
        if item["kind"] == "create_file":
            parameters["content"] = item["content"]
    return request_digest(parameters)


def batch_error(error):
    code = str(error).split(":", 1)[0] if isinstance(error, SourceError) else ""
    return code if _ERROR_CODE.fullmatch(code) else "WRITE_BATCH_OPERATION_FAILED"


class WriteBatch:
    def __init__(self, coordinator):
        self.c, self.store = coordinator, coordinator.store

    def _row(self, task_id, request_id):
        rows = self.store.query(
            "SELECT * FROM write_batches WHERE task_id=? AND request_id=?",
            (task_id, request_id),
        )
        return rows[0] if rows else None

    def _metadata(self, row):
        try:
            metadata = json.loads(row["metadata"])
            descriptors = metadata["items"]
            if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= MAX_BATCH_ITEMS:
                raise ValueError
            for item in descriptors:
                if item["kind"] not in {"apply_edit", "create_file", "create_directory"}:
                    raise ValueError
                validate_path(item["path"])
                validate_request(item["request_id"])
                if not re.fullmatch(r"[a-f0-9]{64}", item["digest"]):
                    raise ValueError
            return metadata
        except (ValueError, TypeError, KeyError, RecursionError):
            raise WriteError("WRITE_BATCH_STATE: preserve invalid batch receipts") from None

    def _entries(self, row):
        entries = []
        for descriptor in self._metadata(row)["items"]:
            records = self.store.query(
                "SELECT * FROM operations WHERE task_id=? AND request_id=?",
                (row["task_id"], descriptor["request_id"]),
            )
            record = records[0] if records else None
            if record is not None and record["digest"] != descriptor["digest"]:
                raise WriteError("REQUEST_ID_CONFLICT: batch child has different content")
            entries.append((descriptor, record))
        return entries

    def _summary(self, row, *, error=None, failed_index=None, verified=False, detail=True):
        entries = self._entries(row)
        completed = sum(record is not None and record["state"] == "done" for _, record in entries)
        pending = any(
            record is not None
            and record["state"] in {"prepared", "committing", "rollback_prepared"}
            for _, record in entries
        )
        aborted = any(record is not None and record["state"] == "aborted" for _, record in entries)
        metadata = self._metadata(row)
        error = error or metadata.get("error")
        failed_index = failed_index if failed_index is not None else metadata.get("failed_index")
        if completed == len(entries) and not error:
            state = "completed"
        else:
            state = "partial" if completed else "blocked"
        result = {
            "task_id": row["task_id"],
            "request_id": row["request_id"],
            "state": state,
            "atomic": False,
            "total": len(entries),
            "completed_count": completed,
            "requires_local_recovery": pending,
            "retryable": not pending and not aborted,
            "current_state_verified": verified,
        }
        if error:
            result["error"] = error
        if failed_index is not None:
            result["failed_index"] = failed_index
        if detail:
            result["items"] = []
            for index, (descriptor, record) in enumerate(entries):
                state = record["state"] if record is not None else "not_started"
                item = {
                    "index": index,
                    "kind": descriptor["kind"],
                    "path": descriptor["path"],
                    "state": state,
                }
                if record is not None and state == "done":
                    saved = json.loads(record["result"])
                    item["result"] = {
                        key: saved[key]
                        for key in (
                            "state",
                            "sha256",
                            "size",
                            "kind",
                            "readback_verified",
                            "ownership_recorded",
                        )
                        if key in saved
                    }
                result["items"].append(item)
        return result

    def status(self, project_id=None):
        condition, parameters = (
            ("WHERE tasks.project_id=?", (project_id,)) if project_id else ("", ())
        )
        rows = self.store.query(
            "SELECT write_batches.*,tasks.project_id FROM write_batches JOIN tasks USING(task_id) "
            f"{condition} ORDER BY created DESC,request_id DESC LIMIT 11",
            parameters,
        )
        return {
            "batches": [
                {"project_id": row["project_id"], **self._summary(row, detail=False)}
                for row in rows[:10]
            ],
            "batches_has_more": len(rows) > 10,
        }

    def _record(self, project, task_id, request_id, digest, items):
        count = self.store.query(
            "SELECT count(*) AS n FROM write_batches WHERE task_id=?", (task_id,)
        )[0]["n"]
        if count >= MAX_BATCHES:
            raise WriteError("WRITE_BATCH_LIMIT: finish this task before starting more batches")
        token = hashlib.sha256((task_id + ":" + request_id).encode()).hexdigest()[:40]
        descriptors = [
            {
                "kind": item["kind"],
                "path": item["path"],
                "request_id": f"wb_{token}_{index:02d}",
                "digest": operation_digest(project, item),
            }
            for index, item in enumerate(items)
        ]
        for identifier in [request_id, *(item["request_id"] for item in descriptors)]:
            if self.store.query(
                "SELECT request_id FROM operations WHERE task_id=? AND request_id=?",
                (task_id, identifier),
            ) or self._row(task_id, identifier):
                raise WriteError(
                    "REQUEST_ID_CONFLICT: identifier already belongs to another operation"
                )
        metadata = encode_metadata({"items": descriptors})
        self.c.reserve_growth(task_id, metadata_bytes=len(metadata.encode()) * 2 + 16 * 1024)
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO write_batches VALUES(?,?,?,?,?,?)",
                (task_id, request_id, digest, self.c.clock(), "ready", metadata),
            )
        return self._row(task_id, request_id)

    def _update(self, row, state, *, error=None, failed_index=None):
        metadata = self._metadata(row)
        metadata.pop("error", None)
        metadata.pop("failed_index", None)
        if error is not None:
            metadata["error"] = error
        if failed_index is not None:
            metadata["failed_index"] = failed_index
        with self.store.transaction() as db:
            db.execute(
                "UPDATE write_batches SET state=?,metadata=? WHERE task_id=? AND request_id=?",
                (state, encode_metadata(metadata), row["task_id"], row["request_id"]),
            )
        return self._row(row["task_id"], row["request_id"])

    def _parents(self, source, path, directories):
        parts = path.split("/")
        with source.root_fd() as root, ExitStack() as stack:
            spec = source._ignore(root)
            if source.scanner._path_problem(path, spec):
                raise WriteError("PATH_EXCLUDED: path is outside the allowed source policy")
            parent = root
            for index, name in enumerate(parts[:-1]):
                prefix = "/".join(parts[: index + 1])
                info = source.scanner._stat(parent, name, prefix)
                if info is None:
                    if all(
                        "/".join(parts[:size]) in directories
                        for size in range(index + 1, len(parts))
                    ):
                        return True
                    raise WriteError(
                        "WRITE_BATCH_PARENT: create every missing ancestor earlier in the batch"
                    )
                if not stat.S_ISDIR(info.st_mode):
                    raise WriteError("INVALID_PARENT: parents must be existing real directories")
                parent = stack.enter_context(source.scanner._directory(parent, name, prefix, info))
        return False

    def _origin_absent(self, task_id, path):
        if self.store.query(
            "SELECT path FROM manifest WHERE task_id=? AND path=?", (task_id, path)
        ) or self.store.query(
            "SELECT path FROM baseline_directories WHERE task_id=? AND path=?", (task_id, path)
        ):
            raise WriteError("WRITE_ORIGIN_CONFLICT: an origin path cannot be adopted")

    def _verify_done(self, project, task, source, descriptor, record):
        metadata, saved = json.loads(record["metadata"]), json.loads(record["result"])
        path = descriptor["path"]
        if metadata.get("kind") != descriptor["kind"] or metadata.get("path") != path:
            raise WriteError("WRITE_BATCH_STATE: child receipt does not match the batch")
        owned = self.c.operations._file(task["task_id"], path)
        if owned is None:
            raise WriteError("WRITE_BATCH_CONFLICT: completed child ownership is unavailable")
        verify_created_parents(self.c, task["task_id"], source, path)
        if descriptor["kind"] == "create_directory":
            binding = json.loads(owned["directory_identity"])
            receipt = metadata.get("installed", metadata.get("prepared", {}))
            if tuple(binding["identity"]) != tuple(receipt.get("directory_identity", ())):
                raise WriteError("WRITE_BATCH_CONFLICT: completed directory identity changed")
            if directory_attributes(
                source, path, binding["identity"], binding["mode"]
            ) != task_attributes(self.c, task["task_id"], path):
                raise WriteError("WRITE_BATCH_CONFLICT: completed directory attributes changed")
        else:
            document = source.read(path)
            identity = metadata.get(
                "installed_version", metadata.get("prepared", {}).get("temp_identity", ())
            )
            if document.sha256 != saved.get("sha256") or document.version[:2] != tuple(
                identity[:2]
            ):
                raise WriteError("WRITE_BATCH_CONFLICT: a completed child changed after writing")
            self.c.operations._previous(project, task["task_id"], path, document)
            if read_file_attributes(source, document) != task_attributes(
                self.c, task["task_id"], path
            ):
                raise WriteError("WRITE_BATCH_CONFLICT: completed file attributes changed")

    def _preflight(self, project, task, source, items, entries):
        scope = json.loads(task["metadata"])["scope"]
        directories, bodies, future_sizes = set(), {}, {}
        unfinished = 0
        for item, (descriptor, record) in zip(items, entries, strict=True):
            self.c._authorized(project)
            path = item["path"]
            if scope is not None and path not in scope:
                raise WriteError("WRITE_TASK_PATH_SCOPE: path was not declared in this task")
            if record is not None:
                if record["state"] != "done":
                    if record["state"] == "aborted":
                        raise WriteError(
                            "WRITE_OPERATION_ABORTED: this batch child cannot execute again"
                        )
                    raise WriteError("WRITE_RECOVERY_REQUIRED: recover the batch child locally")
                self._verify_done(project, task, source, descriptor, record)
                continue
            unfinished += 1
            missing_parent = self._parents(source, path, directories)
            if not missing_parent:
                verify_created_parents(self.c, task["task_id"], source, path)
            if item["kind"] == "apply_edit":
                if missing_parent:
                    raise WriteError("FILE_UNAVAILABLE: edit target must already exist")
                document = source.read(path)
                if document.sha256 != item["expected_sha256"]:
                    raise WriteError("WRITE_HASH_CONFLICT: read the current file before editing")
                previous = self.c.operations._previous(project, task["task_id"], path, document)
                attributes = read_file_attributes(source, document)
                if previous is not None and attributes != task_attributes(
                    self.c, task["task_id"], path
                ):
                    raise WriteError(
                        "WRITE_ATTRIBUTE_CONFLICT: source attributes differ from task state"
                    )
                changed = apply_text_edit(document.content, item["edit"]).content
                if content_problem(changed):
                    raise WriteError(
                        "INVALID_WRITE_CONTENT: edited content is outside source policy"
                    )
                bodies[document.sha256] = allocated(document.size)
                raw = changed.encode()
            else:
                previous = self.c.operations._file(task["task_id"], path)
                if missing_parent:
                    if previous is not None:
                        raise WriteError(
                            "WRITE_FILE_CONFLICT: a previously touched parent disappeared"
                        )
                    self._origin_absent(task["task_id"], path)
                else:
                    with source.parent_fd(path, directory=item["kind"] == "create_directory") as (
                        parent,
                        name,
                    ):
                        if source.scanner._stat(parent, name, path) is not None:
                            raise WriteError(
                                "WRITE_TARGET_EXISTS: batch creates never overwrite an object"
                            )
                    if previous is None:
                        self.c.check_first_touch(project, task["task_id"], path, None)
                    elif (
                        item["kind"] == "create_file"
                        and previous["last_hash"] is None
                        and previous["kind"] in {"modified", "created"}
                    ):
                        from code_context.write_deletion import verify_deleted

                        verify_deleted(source, previous)
                    else:
                        raise WriteError(
                            "WRITE_FILE_CONFLICT: a previously touched path disappeared"
                        )
                if item["kind"] == "create_directory":
                    directories.add(path)
                    continue
                raw = item["content"].encode()
                if len(raw) > MAX_FILE_BYTES or content_problem(item["content"]):
                    raise WriteError("INVALID_WRITE_CONTENT: use bounded allowed UTF-8 source text")
            sha = hashlib.sha256(raw).hexdigest()
            bodies[sha] = allocated(len(raw))
            future_sizes[path] = allocated(len(raw))
        count = self.store.query(
            "SELECT count(*) AS n FROM operations WHERE task_id=?", (task["task_id"],)
        )[0]["n"]
        if count + unfinished > MAX_OPERATIONS:
            raise WriteError("WRITE_OPERATION_LIMIT: finish this task before adding the batch")
        missing = sum(
            size
            for sha, size in bodies.items()
            if not self.store.query("SELECT sha256 FROM objects WHERE sha256=?", (sha,))
        )
        current_sizes = sum(
            allocated(json.loads(row["last_version"])[3])
            for row in self.store.query("SELECT * FROM files WHERE task_id=?", (task["task_id"],))
            if row["path"] not in future_sizes
            and row["kind"] in {"modified", "created"}
            and row["last_version"] is not None
        )
        if unfinished:
            self.c.reserve_growth(
                task["task_id"],
                object_bytes=missing,
                future_body_bytes=current_sizes + sum(future_sizes.values()),
                source_temp_bytes=sum(future_sizes.values()) + len(directories) * 4096,
                metadata_bytes=16 * 1024
                + unfinished * 64 * 1024
                + (4 + 2 * unfinished) * MAX_RECORD_BYTES,
                additional_files=unfinished,
                source=source,
            )

    def _execute(self, project, task_id, descriptor, item):
        request_id = descriptor["request_id"]
        if item["kind"] == "apply_edit":
            return self.c.apply_edit(
                project, task_id, request_id, item["path"], item["expected_sha256"], item["edit"]
            )
        if item["kind"] == "create_file":
            return self.c.create_file(project, task_id, request_id, item["path"], item["content"])
        return self.c.create_directory(project, task_id, request_id, item["path"])

    def write(self, project, task_id, request_id, operations):
        validate_request(request_id)
        items = batch_operations(operations)
        digest = request_digest(
            {
                "kind": "write_batch",
                "project_id": project,
                "task_id": task_id,
                "request_id": request_id,
                "operations": items,
            }
        )
        with self.c.lock:
            source = self.c.source_provider(project)
            source.ensure_available()
            task = self.c._task(project, task_id, source)
            row = self._row(task_id, request_id)
            if row is not None and row["digest"] != digest:
                raise WriteError("REQUEST_ID_CONFLICT: same batch identifier has different content")
            try:
                self.c._authorized(project)
            except SourceError as error:
                if row is None:
                    raise
                return {
                    "project_id": project,
                    "source_mode": "live",
                    **self._summary(row, error=batch_error(error)),
                }
            if row is None:
                if task["state"] != "active":
                    raise WriteError("WRITE_TASK_NOT_ACTIVE: no new batch writes accepted")
                row = self._record(project, task_id, request_id, digest, items)
            entries = self._entries(row)
            if len(entries) != len(items) or any(
                descriptor["digest"] != operation_digest(project, item)
                for item, (descriptor, _) in zip(items, entries, strict=True)
            ):
                raise WriteError("WRITE_BATCH_STATE: preserve inconsistent batch receipts")
            failed_index = None
            with source.lock:
                try:
                    if task["state"] != "active" and any(
                        record is None or record["state"] != "done" for _, record in entries
                    ):
                        raise WriteError(
                            "WRITE_TASK_NOT_ACTIVE: unfinished batch cannot extend a finished task"
                        )
                    self._preflight(project, task, source, items, entries)
                    row = self._update(row, "running")
                    for index, (item, (descriptor, record)) in enumerate(
                        zip(items, entries, strict=True)
                    ):
                        failed_index = index
                        self.c._authorized(project)
                        if record is None:
                            self._execute(project, task_id, descriptor, item)
                    failed_index = None
                    for descriptor, record in self._entries(row):
                        self._verify_done(project, task, source, descriptor, record)
                    self.c._authorized(project)
                    row = self._update(row, "completed")
                    return {
                        "project_id": project,
                        "source_mode": "live",
                        **self._summary(row, verified=True),
                    }
                except Exception as error:
                    code = batch_error(error)
                    try:
                        row = self._update(row, "blocked", error=code, failed_index=failed_index)
                    except Exception:
                        pass  # The existing per-operation journal remains authoritative.
                    return {
                        "project_id": project,
                        "source_mode": "live",
                        **self._summary(row, error=code, failed_index=failed_index),
                    }

"""Serial durable file operations. Not exposed by Runtime/MCP until W gates pass.

Origin text is retained once per task/file; successful intermediate full bodies
are reclaimed only after a durable result and verified temporary cleanup. Every
post-intent failure is protected pending data, never an invitation to retry the
source mutation blindly. Quotas reserve space for the eventual whole-task undo.
"""

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import asdict

from code_context.directory_mutation import commit_directory, prepare_directory
from code_context.file_mutation import commit_file, discard_prepared, prepare_file
from code_context.policy import MAX_FILE_BYTES, content_problem, validate_path
from code_context.recovery_store import encode_metadata
from code_context.scanner import _version
from code_context.text_edits import apply_text_edit
from code_context.write_coordinator import (
    MAX_OPERATIONS,
    WriteError,
    request_digest,
    validate_request,
)

SHA = re.compile(r"[a-f0-9]{64}")


def allocated(size):
    return ((size + 4095) // 4096) * 4096


class WriteOperations:
    def __init__(self, coordinator):
        self.c = coordinator
        self.store = coordinator.store

    def _path(self, path):
        try:
            if not isinstance(path, str):
                raise ValueError
            validate_path(path)
        except ValueError:
            raise WriteError("INVALID_WRITE_PATH: use a normalized project-relative path") from None

    def _replay(self, task_id, request_id, digest):
        rows = self.store.query(
            "SELECT * FROM operations WHERE task_id=? AND request_id=?", (task_id, request_id)
        )
        if not rows:
            return None
        if rows[0]["digest"] != digest:
            raise WriteError("REQUEST_ID_CONFLICT: same identifier has different content")
        if rows[0]["state"] == "done":
            return json.loads(rows[0]["result"])
        if rows[0]["state"] == "aborted":
            raise WriteError("WRITE_OPERATION_ABORTED: this request cannot execute again")
        raise WriteError("WRITE_RECOVERY_REQUIRED: recover the recorded operation locally")

    def _start(self, project, task_id, request_id, digest, *, administrative=False):
        source = self.c._authorized(project)
        task = self.c._task(project, task_id, source)
        replay = self._replay(task_id, request_id, digest)
        if replay is not None:
            return source, task, replay
        if task["state"] != "active":
            raise WriteError("WRITE_TASK_NOT_ACTIVE: no new edits accepted")
        count = self.store.query(
            "SELECT count(*) AS n FROM operations WHERE task_id=?", (task_id,)
        )[0]["n"]
        if not administrative and count >= MAX_OPERATIONS:
            raise WriteError("WRITE_OPERATION_LIMIT: finish or roll back this task")
        return source, task, None

    def _file(self, task_id, path):
        rows = self.store.query("SELECT * FROM files WHERE task_id=? AND path=?", (task_id, path))
        return rows[0] if rows else None

    def _previous(self, project, task_id, path, document):
        source = self.c.source_provider(project)
        with source.parent_fd(path) as (parent, name):
            info = source.scanner._stat(parent, name, path)
            if (
                info is None
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) > 0o777
            ):
                raise WriteError("WRITE_UNSAFE_FILE: use an owned ordinary single-link file")
            if _version(info) != document.version:
                raise WriteError("WRITE_FILE_CONFLICT: source changed before task validation")
        previous = self._file(task_id, path)
        if previous is None:
            self.c.check_first_touch(project, task_id, path, document)
        elif (
            previous["kind"] not in {"modified", "created"}
            or previous["last_hash"] != document.sha256
            or tuple(json.loads(previous["last_version"])) != document.version
        ):
            raise WriteError("WRITE_FILE_CONFLICT: file differs from the task's last saved state")
        return previous

    def _reserve(self, task_id, path, before, raw, previous, *, source):
        bodies = {hashlib.sha256(raw).hexdigest(): raw}
        if before is not None:
            bodies[before.sha256] = before.content.encode("utf-8")
        missing = sum(
            allocated(len(body))
            for sha, body in bodies.items()
            if not self.store.query("SELECT sha256 FROM objects WHERE sha256=?", (sha,))
        )
        # Protect room for every touched file's latest state before whole-task
        # rollback. Deliberately conservative: no optimistic dedup of future data.
        touched = self.store.query("SELECT * FROM files WHERE task_id=?", (task_id,))
        rollback_bytes = allocated(len(raw)) + sum(
            allocated(json.loads(row["last_version"])[3])
            for row in touched
            if row["path"] != path and row["kind"] in {"modified", "created"}
        )
        metadata = 64 * 1024 + (len(touched) + (previous is None)) * 1024
        self.c.reserve_growth(
            task_id,
            object_bytes=missing,
            future_body_bytes=rollback_bytes,
            additional_files=int(previous is None),
            source_temp_bytes=allocated(max(len(raw), before.size if before else 0)),
            metadata_bytes=metadata,
            source=source,
        )

    def _record(self, task_id, request_id, digest, metadata):
        with self.store.transaction() as db:
            sequence = db.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM operations WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,NULL,?)",
                (task_id, request_id, digest, "prepared", encode_metadata(metadata), sequence),
            )

    def _metadata(self, task_id, request_id, metadata, *, state=None):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE operations SET metadata=?,state=coalesce(?,state) "
                "WHERE task_id=? AND request_id=?",
                (encode_metadata(metadata), state, task_id, request_id),
            )

    def _protect(self, task_id):
        self.c.stop_requested.set()
        self.c.grants = {}
        try:
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET state='recovery_required' WHERE task_id=?", (task_id,))
        except Exception:
            pass  # Existing pending operation still gates restart and publication.

    def _touch(self, task_id, path, before, previous, after):
        with self.store.transaction() as db:
            if previous is None:
                db.execute(
                    "INSERT INTO files VALUES(?,?,?,?,?,?,?,NULL)",
                    (
                        task_id,
                        path,
                        "modified" if before is not None else "created",
                        before.sha256 if before is not None else None,
                        before.mode if before is not None else None,
                        after.sha256,
                        encode_metadata(after.version),
                    ),
                )
            else:
                db.execute(
                    "UPDATE files SET last_hash=?,last_version=? WHERE task_id=? AND path=?",
                    (after.sha256, encode_metadata(after.version), task_id, path),
                )

    def _install(self, project, task_id, request_id, path, digest, before, previous, raw, preview):
        c, source = self.c, self.c._authorized(project)
        self._reserve(task_id, path, before, raw, previous, source=source)
        mode = before.mode if before is not None else 0o644
        prefix = f"{task_id}:op:{request_id}:"
        metadata = {
            "kind": "apply_edit" if before is not None else "create_file",
            "path": path,
            "phase": "backing_up",
            "temp_name": ".colink-write-" + uuid.uuid4().hex + ".tmp",
            "before_hash": before.sha256 if before is not None else None,
            "before_version": before.version if before is not None else None,
            "before_mode": before.mode if before is not None else None,
            "after_hash": hashlib.sha256(raw).hexdigest(),
            "after_size": len(raw),
            "mode": mode,
            "first_touch": previous is None,
            "preview": preview,
        }
        self._record(task_id, request_id, digest, metadata)
        c.inflight_project = project
        try:
            if before is not None:
                body = before.content.encode("utf-8")
                self.store.put_blob(body, prefix + "before")
                if previous is None:
                    self.store.put_blob(body, f"{task_id}:origin:{path}")
            self.store.put_blob(raw, prefix + "after")
            metadata["phase"] = "preparing"
            self._metadata(task_id, request_id, metadata)

            def created(receipt):
                metadata["prepared"] = asdict(receipt)
                self._metadata(task_id, request_id, metadata)

            prepared = prepare_file(source, path, raw, mode, metadata["temp_name"], created)
            c._authorized(project, _allow_pending=True)
            metadata["phase"] = "installing"
            self._metadata(task_id, request_id, metadata, state="committing")
            after = commit_file(source, prepared, before)
            metadata["phase"] = "installed"
            metadata["installed_version"] = after.version
            self._metadata(task_id, request_id, metadata)
            self._touch(task_id, path, before, previous, after)
            # The installed result is durable before any source temp is removed.
            discard_prepared(
                source,
                prepared,
                before.sha256 if before else prepared.sha256,
                before.version[:2] if before else prepared.temp_identity,
            )
            final = source.read(path)
            if final.sha256 != after.sha256 or final.version[:2] != after.version[:2]:
                raise WriteError("WRITE_FILE_CONFLICT: installed file changed before final result")
            self._touch(task_id, path, before, self._file(task_id, path), final)
            result = {
                "project_id": project,
                "task_id": task_id,
                "path": path,
                "state": "saved",
                "sha256": final.sha256,
                "size": final.size,
                "source_mode": "live",
                "source_is_untrusted": True,
                "preview": preview,
                "readback_verified": True,
                "index_status": "revalidate_on_next_query",
            }
            metadata["phase"] = "done"
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE operations SET state='done',metadata=?,result=? "
                    "WHERE task_id=? AND request_id=?",
                    (encode_metadata(metadata), encode_metadata(result), task_id, request_id),
                )
                db.execute(
                    "DELETE FROM object_refs WHERE owner IN (?,?)",
                    (prefix + "before", prefix + "after"),
                )
            self.store.collect_unreferenced()
            try:
                c.on_change(project)
            except Exception:
                pass  # Real source fingerprints still reject stale query contexts.
            return result
        except BaseException:
            self._protect(task_id)
            raise WriteError(
                "WRITE_RECOVERY_REQUIRED: preserve this operation; recover locally before retrying"
            ) from None
        finally:
            c.inflight_project = None

    def apply_edit(self, project, task_id, request_id, path, expected_sha256, edit):
        validate_request(request_id)
        self._path(path)
        if not isinstance(expected_sha256, str) or SHA.fullmatch(expected_sha256) is None:
            raise WriteError("INVALID_EXPECTED_HASH: use the full hash returned by a source read")
        digest = request_digest(
            {
                "kind": "apply_edit",
                "project": project,
                "path": path,
                "sha": expected_sha256,
                "edit": edit,
            }
        )
        with self.c.lock:
            source, task, replay = self._start(project, task_id, request_id, digest)
            if replay is not None:
                return replay
            with source.lock:
                before = source.read(path)
                if before.sha256 != expected_sha256:
                    raise WriteError("WRITE_HASH_CONFLICT: read the current file before editing")
                previous = self._previous(project, task_id, path, before)
                changed = apply_text_edit(before.content, edit)
                preview = asdict(changed)
                del preview["content"]
                return self._install(
                    project,
                    task_id,
                    request_id,
                    path,
                    digest,
                    before,
                    previous,
                    changed.content.encode("utf-8"),
                    preview,
                )

    def create_file(self, project, task_id, request_id, path, content):
        validate_request(request_id)
        self._path(path)
        try:
            if not isinstance(content, str):
                raise ValueError
            raw = content.encode("utf-8")
            if len(raw) > MAX_FILE_BYTES or content_problem(content):
                raise ValueError
        except (ValueError, UnicodeError):
            raise WriteError(
                "INVALID_WRITE_CONTENT: use bounded allowed UTF-8 source text"
            ) from None
        digest = request_digest(
            {"kind": "create_file", "project": project, "path": path, "content": content}
        )
        with self.c.lock:
            source, task, replay = self._start(project, task_id, request_id, digest)
            if replay is not None:
                return replay
            with source.lock, source.parent_fd(path) as (parent, name):
                if source.scanner._stat(parent, name, path) is not None:
                    raise WriteError("WRITE_TARGET_EXISTS: new file must not overwrite any object")
                if self._file(task_id, path) is not None:
                    raise WriteError("WRITE_FILE_CONFLICT: a previously touched path disappeared")
                self.c.check_first_touch(project, task_id, path, None)
                return self._install(
                    project,
                    task_id,
                    request_id,
                    path,
                    digest,
                    None,
                    None,
                    raw,
                    {
                        "start_line": 1,
                        "end_line": max(1, len(content.splitlines())),
                        "before": "",
                        "after": content[:4000],
                    },
                )

    def finish_write_task(self, project, task_id, request_id):
        validate_request(request_id)
        digest = request_digest({"kind": "finish_write_task", "project": project})
        with self.c.lock:
            source, task, replay = self._start(
                project, task_id, request_id, digest, administrative=True
            )
            if replay is not None:
                return replay
            from code_context.write_diff import verify_task_files

            with source.lock:
                verify_task_files(self.c, task, source)
                self.c._authorized(project)
                self.store.reserve(metadata_bytes=16 * 1024)
                result = {
                    "project_id": project,
                    "task_id": task_id,
                    "state": "completed",
                    "source_mode": "live",
                    "recovery_point_retained": True,
                    "rollback_available": False,  # Whole-task recovery layer is not attached yet.
                }
                with self.store.transaction() as db:
                    sequence = db.execute(
                        "SELECT coalesce(max(sequence),0)+1 FROM operations WHERE task_id=?",
                        (task_id,),
                    ).fetchone()[0]
                    db.execute(
                        "INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                        (
                            task_id,
                            request_id,
                            digest,
                            "done",
                            encode_metadata({"kind": "finish_write_task"}),
                            encode_metadata(result),
                            sequence,
                        ),
                    )
                    db.execute(
                        "UPDATE tasks SET state='completed',completed=? WHERE task_id=?",
                        (self.c.clock(), task_id),
                    )
                for old in self.store.query(
                    "SELECT * FROM tasks WHERE state IN ('completed','rolled_back') AND task_id!=?",
                    (task_id,),
                ):
                    self.c._retire(old)
                return result

    def create_directory(self, project, task_id, request_id, path):
        validate_request(request_id)
        self._path(path)
        digest = request_digest({"kind": "create_directory", "project": project, "path": path})
        with self.c.lock:
            source, task, replay = self._start(project, task_id, request_id, digest)
            if replay is not None:
                return replay
            with source.lock, source.parent_fd(path, directory=True) as (parent, name):
                if source.scanner._stat(parent, name, path) is not None:
                    raise WriteError("WRITE_TARGET_EXISTS: new directory must not adopt any object")
                if self._file(task_id, path) is not None:
                    raise WriteError(
                        "WRITE_FILE_CONFLICT: a previously touched directory disappeared"
                    )
                self.c.check_first_touch(project, task_id, path, None)
                self.c.reserve_growth(
                    task_id,
                    source_temp_bytes=4096,
                    metadata_bytes=64 * 1024,
                    additional_files=1,
                    source=source,
                )
                metadata = {
                    "kind": "create_directory",
                    "path": path,
                    "phase": "preparing",
                    "temp_name": ".colink-write-" + uuid.uuid4().hex + ".tmp",
                    "mode": 0o755,
                }
                self._record(task_id, request_id, digest, metadata)
                self.c.inflight_project = project
                try:

                    def created(prepared):
                        metadata["prepared"] = asdict(prepared)
                        self._metadata(task_id, request_id, metadata)

                    prepared = prepare_directory(
                        source, path, 0o755, metadata["temp_name"], created
                    )
                    self.c._authorized(project, _allow_pending=True)
                    metadata["phase"] = "installing"
                    self._metadata(task_id, request_id, metadata, state="committing")
                    receipt = commit_directory(source, prepared)
                    metadata["phase"] = "done"
                    metadata["installed"] = asdict(receipt)
                    result = {
                        "project_id": project,
                        "task_id": task_id,
                        "path": path,
                        "state": "created",
                        "kind": "directory",
                        "source_mode": "live",
                        "ownership_recorded": True,
                    }
                    with self.store.transaction() as db:
                        db.execute(
                            "INSERT INTO files VALUES(?,?,?,NULL,NULL,NULL,NULL,?)",
                            (
                                task_id,
                                path,
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
                                task_id,
                                request_id,
                            ),
                        )
                    try:
                        self.c.on_change(project)
                    except Exception:
                        pass
                    return result
                except BaseException:
                    self._protect(task_id)
                    raise WriteError(
                        "WRITE_RECOVERY_REQUIRED: preserve directory materials and recover locally"
                    ) from None
                finally:
                    self.c.inflight_project = None

"""Bounded task-origin comparisons, independent of a permanent source mirror.

Metadata summaries verify every touched path against the task's last state.
Patches load only requested origin/current text; large line products refuse a
potentially quadratic diff and expose that limit rather than inventing a patch.
Unknown external edits never become 'no changes' or part of this task silently.
"""

import difflib
import json
import stat

from code_context.policy import MAX_TOTAL_BYTES, validate_path
from code_context.scanner import _version
from code_context.source_access import SourceError
from code_context.source_page import physical_lines
from code_context.write_coordinator import RETENTION_SECONDS, TERMINAL, WriteError

MAX_DIFF_LINES = 10_000
MAX_LINE_PRODUCT = 4_000_000


def verify_task_files(c, task, source):
    rows = c.store.query("SELECT * FROM files WHERE task_id=? ORDER BY path", (task["task_id"],))
    binary_paths = {
        row["path"]
        for row in c.store.query(
            "SELECT path FROM move_mappings WHERE task_id=? AND kind='binary'",
            (task["task_id"],),
        )
    }
    for row in rows:
        if row["kind"] != "directory" and row["last_hash"] is None:
            from code_context.write_deletion import verify_deleted

            verify_deleted(source, row)
            continue
        with source.parent_fd(row["path"], directory=row["kind"] == "directory") as (parent, name):
            before = source.scanner._stat(parent, name, row["path"])
            if row["kind"] == "directory":
                binding = json.loads(row["directory_identity"])
                if (
                    before is None
                    or not stat.S_ISDIR(before.st_mode)
                    or (before.st_dev, before.st_ino) != tuple(binding["identity"])
                    or stat.S_IMODE(before.st_mode) != binding["mode"]
                ):
                    raise WriteError("WRITE_DIFF_CONFLICT: a task-created directory changed")
            elif (
                before is None
                or not stat.S_ISREG(before.st_mode)
                or _version(before) != tuple(json.loads(row["last_version"]))
                or (
                    c.movement.snapshot(source, row["path"]).digest
                    if row["path"] in binary_paths
                    else source.fingerprint(row["path"])
                )
                != row["last_hash"]
            ):
                raise WriteError("WRITE_DIFF_CONFLICT: a task file changed outside its last state")
            after = source.scanner._stat(parent, name, row["path"])
            if after is None or _version(before) != _version(after):
                raise WriteError("WRITE_DIFF_CONFLICT: a task path changed during validation")
    source.ensure_available()
    from code_context.write_attributes import verify_task_attributes

    verify_task_attributes(c, task, source, rows)
    c.movement.verify(task, source)
    return rows


def bounded_patch(path, before, after, max_chars, *, from_path=None):
    old, new = physical_lines(before, keepends=True), physical_lines(after, keepends=True)
    if (
        len(old) > MAX_DIFF_LINES
        or len(new) > MAX_DIFF_LINES
        or len(old) * len(new) > MAX_LINE_PRODUCT
    ):
        return "", True, "DIFF_COMPUTATION_LIMIT"
    parts, remaining, truncated = [], max_chars, False
    for piece in difflib.unified_diff(
        old, new, fromfile="a/" + (from_path or path), tofile="b/" + path
    ):
        if len(piece) > remaining:
            parts.append(piece[:remaining])
            truncated = True
            break
        parts.append(piece)
        remaining -= len(piece)
    return "".join(parts), truncated, None


class TaskDiff:
    def __init__(self, coordinator):
        self.c = coordinator
        self.store = coordinator.store

    @staticmethod
    def _parameters(path, baseline, detail, offset, limit, max_chars):
        if path is not None:
            try:
                if not isinstance(path, str):
                    raise ValueError
                validate_path(path)
            except ValueError:
                raise WriteError("INVALID_DIFF_PATH: use a normalized relative path") from None
        if (
            baseline not in {"previous", "empty"}
            or detail not in {"summary", "patch"}
            or any(type(v) is not int for v in (offset, limit, max_chars))
            or offset < 0
            or not 1 <= limit <= 100
            or not 1000 <= max_chars <= 50_000
        ):
            raise WriteError("INVALID_DIFF_PARAMETERS: use bounded summary or patch parameters")

    def get_diff(
        self,
        project,
        *,
        path=None,
        baseline="previous",
        detail="summary",
        offset=0,
        limit=50,
        max_chars=20_000,
        task_id=None,
    ):
        self._parameters(path, baseline, detail, offset, limit, max_chars)
        with self.c.lock:
            self.c.guard_read(project)
            source = self.c.source_provider(project)
            source.ensure_available()
            if baseline == "empty":
                return self._empty(project, source, path, detail, offset, limit, max_chars)
            candidates = self.store.query(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY created DESC", (project,)
            )
            if task_id is not None:
                task = self.c._task(project, task_id, source)
            else:
                task = next(
                    (row for row in candidates if row["source_id"] == source.source_id), None
                )
            if task is None or (
                task["state"] in TERMINAL
                and task["completed"] is not None
                and self.c.clock() - task["completed"] > RETENTION_SECONDS
            ):
                return {
                    "project_id": project,
                    "source_mode": "live",
                    "changes_available": False,
                    "baseline": None,
                    "reason": "NO_TASK_BASELINE",
                    "changes": [],
                    "has_more": False,
                    "next_offset": None,
                    "truncated": False,
                }
            if task["state"] == "rolled_back":
                with source.lock:
                    self.c.rollback.verify_restored(source, task)
                    return self._result(project, task, [], [], detail, offset, False)
            with source.lock:
                rows = verify_task_files(self.c, task, source)
                mappings = {
                    row["path"]: row
                    for row in self.store.query(
                        "SELECT * FROM move_mappings WHERE task_id=?", (task["task_id"],)
                    )
                }
                descriptions = []
                for row in rows:
                    mapping = mappings.get(row["path"])
                    origin = mapping["origin_path"] if mapping else row["path"]
                    moved = origin is not None and origin != row["path"]
                    if (
                        (path is not None and path not in {row["path"], origin})
                        or (
                            row["kind"] == "modified"
                            and row["origin_hash"] == row["last_hash"]
                            and not moved
                        )
                        or (row["kind"] == "created" and row["last_hash"] is None)
                    ):
                        continue
                    operation = (
                        "delete"
                        if row["last_hash"] is None and row["kind"] != "directory"
                        else "add"
                        if row["kind"] in {"created", "directory"}
                        else "move"
                        if moved
                        else "modify"
                    )
                    description = {
                        "path": row["path"],
                        "op": operation,
                        "kind": "directory" if row["kind"] == "directory" else "file",
                    }
                    if moved and row["kind"] not in {"created", "directory"}:
                        description.update({"from_path": origin, "to_path": row["path"]})
                    if mapping and mapping["kind"] == "binary":
                        description["content_kind"] = "binary"
                    descriptions.append(description)
                for current_path, mapping in mappings.items():
                    origin = mapping["origin_path"]
                    if (
                        mapping["kind"] == "directory"
                        and origin is not None
                        and origin != current_path
                        and (path is None or path in {current_path, origin})
                    ):
                        descriptions.append(
                            {
                                "path": current_path,
                                "from_path": origin,
                                "to_path": current_path,
                                "op": "move",
                                "kind": "directory",
                            }
                        )
                descriptions.sort(key=lambda item: item["path"])
                selected = descriptions[offset : offset + limit]
                changes, remaining, truncated = [], max_chars, False
                by_path = {row["path"]: row for row in rows}
                for description in selected:
                    entry = dict(description)
                    charge = len(json.dumps(entry, ensure_ascii=False))
                    if charge + 128 > remaining:
                        truncated = True
                        break
                    remaining -= charge + 128
                    if detail == "patch" and entry.get("content_kind") == "binary":
                        entry.update(
                            {
                                "patch": "",
                                "patch_truncated": False,
                                "patch_unavailable_reason": "BINARY_CONTENT",
                            }
                        )
                    elif detail == "patch" and entry["kind"] != "directory":
                        row = by_path[entry["path"]]
                        current_text = ""
                        if row["last_hash"] is not None:
                            current = source.read(row["path"])
                            if current.sha256 != row["last_hash"] or current.version != tuple(
                                json.loads(row["last_version"])
                            ):
                                raise WriteError("WRITE_DIFF_CONFLICT: current patch input changed")
                            current_text = current.content
                        original = (
                            self.store.read_blob(row["origin_hash"]).decode("utf-8")
                            if row["origin_hash"] is not None
                            else ""
                        )
                        patch, cut, reason = bounded_patch(
                            row["path"],
                            original,
                            current_text,
                            remaining,
                            from_path=entry.get("from_path"),
                        )
                        entry.update({"patch": patch, "patch_truncated": cut})
                        if reason:
                            entry["patch_unavailable_reason"] = reason
                        remaining -= len(patch)
                        truncated |= cut
                    changes.append(entry)
                verify_task_files(self.c, task, source)
                return self._result(project, task, descriptions, changes, detail, offset, truncated)

    def _result(self, project, task, descriptions, changes, detail, offset, truncated):
        next_offset = offset + len(changes)
        has_more = next_offset < len(descriptions)
        return {
            "project_id": project,
            "source_mode": "live",
            "task_id": task["task_id"],
            "task_state": task["state"],
            "baseline": "task_origin",
            "detail": detail,
            "changes_available": True,
            "source_is_untrusted": True,
            "summary": {
                "files_changed": sum(item["kind"] == "file" for item in descriptions),
                "directories_added": sum(
                    item["kind"] == "directory" and item["op"] == "add" for item in descriptions
                ),
                "added": sum(item["op"] == "add" for item in descriptions),
                "modified": sum(item["op"] == "modify" for item in descriptions),
                "deleted": sum(item["op"] == "delete" for item in descriptions),
                "moved": sum(item["op"] == "move" for item in descriptions),
                "line_counts_available": False,
            },
            "changes": changes,
            "offset": offset,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "truncated": truncated,
        }

    @staticmethod
    def _empty(project, source, path, detail, offset, limit, max_chars):
        metadata = source.manifest()
        candidates = [item for item in metadata["files"] if path is None or item["path"] == path]
        changes, consumed, remaining, truncated, inspected = [], 0, max_chars, False, 0
        selected = candidates[offset : offset + limit]
        for item in selected:
            if consumed + item["size"] > MAX_TOTAL_BYTES:
                truncated = True
                break
            try:
                document = source.read(item["path"])
            except SourceError as exc:
                if str(exc).startswith("FILE_EXCLUDED:"):
                    inspected += 1
                    continue
                raise
            consumed += document.size
            entry = {"path": item["path"], "op": "add", "kind": "file", "sha256": document.sha256}
            charge = len(json.dumps(entry, ensure_ascii=False)) + 128
            if charge > remaining:
                truncated = True
                break
            remaining -= charge
            if detail == "patch":
                patch, cut, reason = bounded_patch(item["path"], "", document.content, remaining)
                entry.update({"patch": patch, "patch_truncated": cut})
                if reason:
                    entry["patch_unavailable_reason"] = reason
                remaining -= len(patch)
                truncated |= cut
            changes.append(entry)
            inspected += 1
        next_offset = offset + inspected
        has_more = next_offset < len(candidates)
        return {
            "project_id": project,
            "source_mode": "live",
            "baseline": "empty",
            "changes_available": True,
            "source_is_untrusted": True,
            "detail": detail,
            "changes": changes,
            "candidate_file_count": len(candidates),
            "source_partial": metadata["partial"],
            "offset": offset,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "truncated": truncated,
        }

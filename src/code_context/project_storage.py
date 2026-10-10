"""Offline, local-only mapping of saved project metadata to derived index folders.

This never initializes a project, opens a database, reads credentials or requires
the original source directory to exist. It does not delete any persisted data.
"""

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from code_context.models import validate_project
from code_context.source_access import SourceAccess, SourceError

_KEY = re.compile(r"^[0-9a-f]{64}$")
_MAX_BYTES = 1024 * 1024


def _text(value, maximum=1024):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("invalid saved metadata")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("invalid saved metadata")
    return value


def _registry(boundary, relative):
    """Read a pinned, owned metadata file without following directory/file links."""
    with boundary.root_fd() as root_fd:
        parent = os.dup(root_fd)
        try:
            parts = PurePosixPath(relative).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size > _MAX_BYTES
                ):
                    raise ValueError("invalid saved metadata")
                raw = stream.read(_MAX_BYTES + 1)
                if len(raw) > _MAX_BYTES:
                    raise ValueError("invalid saved metadata")
                return json.loads(raw)
        finally:
            os.close(parent)


def _index_size(boundary, relative):
    """Count fixed cache files only; never recurse into source/runtime folders."""
    with boundary.root_fd() as root_fd:
        parent = os.dup(root_fd)
        try:
            for part in PurePosixPath(relative).parts:
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                    )
                except FileNotFoundError:
                    return 0, False
                os.close(parent)
                parent = child
            size = 0
            exists = False
            for name in (
                "live-index.sqlite3",
                "live-index.sqlite3-journal",
                "live-index.sqlite3-wal",
                "live-index.sqlite3-shm",
            ):
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("invalid derived file")
                size += info.st_size
                exists = True
            return size, exists
        finally:
            os.close(parent)


def project_storage(workspace: Path, *, offset=0, limit=10):
    """Return a bounded page, including registrations whose source was deleted."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid storage pagination")
    boundary = SourceAccess(workspace.expanduser())
    saved = boundary.root / ".code-context/desktop/workspaces"
    try:
        keys = sorted(path.name for path in saved.iterdir() if _KEY.fullmatch(path.name))
    except FileNotFoundError:
        keys = []
    if len(keys) > 128:
        raise SourceError("STORAGE_METADATA_LIMIT: too many saved workspaces")
    rows, unavailable = [], 0
    for key in keys:
        relative_runtime = f".code-context/desktop/workspaces/{key}/live-v1"
        try:
            value = _registry(boundary, relative_runtime + "/registry/projects.json")
            if value.get("schema") not in {1, 2, 3}:
                raise ValueError("unknown registry")
            source_root = Path(_text(value["workspace"]["path"]))
            if not source_root.is_absolute() or ".." in source_root.parts:
                raise ValueError("invalid saved root")
            projects = value["projects"]
            if not isinstance(projects, list) or len(projects) > 64:
                raise ValueError("invalid saved projects")
            for project in projects:
                identifier = validate_project(_text(project["project_id"], 100))
                name = _text(project["display_name"], 120)
                relative = project["relative_root"]
                if not isinstance(relative, str) or len(relative) > 1024:
                    raise ValueError("invalid saved source")
                path = PurePosixPath(relative)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or any(ord(character) < 32 or ord(character) == 127 for character in relative)
                ):
                    raise ValueError("invalid saved source")
                index_relative = (
                    relative_runtime
                    + "/index/projects/"
                    + hashlib.sha256(identifier.encode()).hexdigest()
                )
                try:
                    size, cached = _index_size(boundary, index_relative)
                except FileNotFoundError:
                    size, cached = 0, False
                source = source_root / relative
                rows.append(
                    {
                        "id": key + ":" + identifier,
                        "project_id": identifier,
                        "display_name": name,
                        "source_path": str(source),
                        "source_exists": source.is_dir(),
                        "index_directory": str(boundary.root / index_relative),
                        "index_bytes": size,
                        "index_exists": cached,
                        "runtime_directory": str(boundary.root / relative_runtime),
                    }
                )
        except (OSError, SourceError, ValueError, TypeError, KeyError):
            unavailable += 1
    rows.sort(key=lambda item: (item["display_name"], item["source_path"], item["id"]))
    page = rows[offset : offset + limit]
    result = {
        "projects": page,
        "total": len(rows),
        "offset": offset,
        "has_more": offset + len(page) < len(rows),
        "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
        "unavailable_metadata": unavailable,
        "storage_directory": str(boundary.root),
        "local_only": True,
    }
    # The native pipe has a 64 KiB byte limit. Long Unicode paths may require
    # fewer rows than requested, without losing the next page's position.
    while len(json.dumps(result, ensure_ascii=False, indent=2).encode()) > 60_000:
        if len(page) <= 1:
            raise SourceError("STORAGE_METADATA_LIMIT: saved paths exceed the display budget")
        page.pop()
        result["has_more"] = True
        result["next_offset"] = offset + len(page)
    return result

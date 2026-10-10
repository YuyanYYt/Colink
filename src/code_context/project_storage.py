"""Offline, local-only mapping of saved project metadata to derived index folders.

This never initializes a project, opens a database, reads credentials or requires
the original source directory to exist. It does not delete any persisted data.
"""

import fcntl
import hashlib
import json
import os
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
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


def _tree_storage(boundary, relative, *, request_deadline=None):
    """Count owned local data, including sparse allocation; never follow links."""
    deadline = time.monotonic() + 3
    if request_deadline is not None:
        deadline = min(deadline, request_deadline)
    remaining = 50_000

    def count(directory, device):
        nonlocal remaining
        size, allocated = 0, 0
        for name in os.listdir(directory):
            remaining -= 1
            if remaining < 0 or time.monotonic() > deadline:
                raise SourceError("STORAGE_METADATA_LIMIT: workspace inventory exceeds its budget")
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if info.st_uid != os.getuid() or info.st_dev != device:
                raise ValueError("foreign workspace entry")
            if stat.S_ISDIR(info.st_mode):
                child = _directory_at(directory, name)
                try:
                    child_size, child_allocated = count(child, device)
                    size += child_size
                    allocated += child_allocated
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                size += info.st_size
                allocated += info.st_blocks * 512
            else:
                raise ValueError("linked or unsupported workspace entry")
        return size, allocated

    with boundary.root_fd() as root:
        parent = os.dup(root)
        try:
            for part in PurePosixPath(relative).parts:
                child = _directory_at(parent, part)
                os.close(parent)
                parent = child
            return count(parent, os.fstat(parent).st_dev)
        finally:
            os.close(parent)


def project_storage(workspace: Path, *, offset=0, limit=10, view="projects"):
    """Return a bounded page, including registrations whose source was deleted."""
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 20
        or view not in {"projects", "workspaces"}
    ):
        raise ValueError("invalid storage pagination")
    boundary = SourceAccess(workspace.expanduser())
    saved = boundary.root / ".code-context/desktop/workspaces"
    try:
        keys = sorted(path.name for path in saved.iterdir() if _KEY.fullmatch(path.name))
    except FileNotFoundError:
        keys = []
    if len(keys) > 128:
        raise SourceError("STORAGE_METADATA_LIMIT: too many saved workspaces")
    rows, workspaces, unavailable = [], [], 0
    listing_deadline = time.monotonic() + 8
    for key in keys:
        relative_runtime = f".code-context/desktop/workspaces/{key}/live-v1"
        try:
            value = _registry(boundary, relative_runtime + "/registry/projects.json")
            if not isinstance(value, dict) or value.get("schema") not in {1, 2, 3}:
                raise ValueError("unknown registry")
            source_root = Path(_text(value["workspace"]["path"]))
            if not source_root.is_absolute() or ".." in source_root.parts:
                raise ValueError("invalid saved root")
            if hashlib.sha256(str(source_root).encode()).hexdigest() != key:
                raise ValueError("saved workspace path mismatch")
            projects = value["projects"]
            if not isinstance(projects, list) or len(projects) > 64:
                raise ValueError("invalid saved projects")
            workspace_rows = []
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
                workspace_rows.append(
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
            workspace_relative = f".code-context/desktop/workspaces/{key}"
            if view == "workspaces":
                total_bytes, allocated = _tree_storage(
                    boundary, workspace_relative, request_deadline=listing_deadline
                )
            else:
                total_bytes, allocated = 0, 0
            legacy_bytes, _ = _index_size(boundary, relative_runtime + "/index")
            workspaces.append(
                {
                    "id": key,
                    "display_name": source_root.name,
                    "source_path": str(source_root),
                    "source_exists": source_root.is_dir(),
                    "workspace_directory": str(boundary.root / workspace_relative),
                    "runtime_directory": str(boundary.root / relative_runtime),
                    "workspace_bytes": total_bytes,
                    "workspace_allocated_bytes": allocated,
                    "project_count": len(workspace_rows),
                    "project_names": [
                        item["display_name"]
                        + (
                            " · " + str(Path(item["source_path"]).relative_to(source_root))
                            if item["source_path"] != str(source_root)
                            else ""
                        )
                        for item in workspace_rows
                    ],
                    "index_bytes": legacy_bytes + sum(p["index_bytes"] for p in workspace_rows),
                    "legacy_index_bytes": legacy_bytes,
                }
            )
            rows.extend(workspace_rows)
        except (OSError, SourceError, ValueError, TypeError, KeyError):
            unavailable += 1
    rows.sort(key=lambda item: (item["display_name"], item["source_path"], item["id"]))
    workspaces.sort(key=lambda item: (item["display_name"], item["source_path"], item["id"]))
    selected = workspaces if view == "workspaces" else rows
    page = selected[offset : offset + limit]
    result = {
        "projects": page if view == "projects" else [],
        "workspaces": page if view == "workspaces" else [],
        "total": len(selected),
        "total_projects": len(rows),
        "total_workspaces": len(workspaces),
        "view": view,
        "offset": offset,
        "has_more": offset + len(page) < len(selected),
        "next_offset": offset + len(page) if offset + len(page) < len(selected) else None,
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


def _directory_at(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise ValueError("unsafe persisted directory")
    return fd


def _locked_at(parent, name):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise ValueError("unsafe persisted lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SourceError(
                "INDEX_IN_USE: stop this workspace connection before clearing"
            ) from None
        return fd
    except BaseException:
        os.close(fd)
        raise


def _trash_directory():
    path = Path.home() / ".Trash"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def trash_project_index(workspace: Path, entry_id: str, *, stage_for_native_trash=False):
    """Move one registered, inactive derived folder to Trash; preserve all other data."""
    if not isinstance(entry_id, str) or len(entry_id) > 180 or entry_id.count(":") != 1:
        raise SourceError("INVALID_STORAGE_PROJECT: choose a saved project")
    key, project_id = entry_id.split(":")
    if _KEY.fullmatch(key) is None:
        raise SourceError("INVALID_STORAGE_PROJECT: choose a saved project")
    try:
        validate_project(project_id)
        boundary = SourceAccess(workspace.expanduser())
        relative = f".code-context/desktop/workspaces/{key}/live-v1"
        registry = _registry(boundary, relative + "/registry/projects.json")
        if registry.get("schema") not in {1, 2, 3} or not any(
            item.get("project_id") == project_id for item in registry.get("projects", [])
        ):
            raise ValueError("project registration unavailable")
        project_key = hashlib.sha256(project_id.encode()).hexdigest()
        handles = []
        with boundary.root_fd() as root_fd:
            parent = os.dup(root_fd)
            handles.append(parent)
            try:
                for part in PurePosixPath(relative).parts:
                    parent = _directory_at(parent, part)
                    handles.append(parent)
                handles.append(_locked_at(parent, "runtime.lock"))
                try:
                    index = _directory_at(parent, "index")
                except FileNotFoundError:
                    return {"cleared": False, "already_empty": True, "project_id": project_id}
                handles.append(index)
                handles.append(_locked_at(index, "project-index.lock"))
                try:
                    projects = _directory_at(index, "projects")
                    handles.append(projects)
                    selected = _directory_at(projects, project_key)
                    handles.append(selected)
                except FileNotFoundError:
                    return {"cleared": False, "already_empty": True, "project_id": project_id}
                handles.append(_locked_at(selected, "live-index.lock"))
                expected = {
                    "live-index.lock",
                    "live-index.sqlite3",
                    "live-index.sqlite3-journal",
                    "live-index.sqlite3-wal",
                    "live-index.sqlite3-shm",
                }
                size = 0
                for name in os.listdir(selected):
                    info = os.stat(name, dir_fd=selected, follow_symlinks=False)
                    if (
                        name not in expected
                        or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_nlink != 1
                    ):
                        raise ValueError("unknown file in derived directory")
                    size += info.st_size
                if stage_for_native_trash:
                    context = _directory_at(root_fd, ".code-context")
                    handles.append(context)
                    try:
                        os.mkdir("retired-indexes", mode=0o700, dir_fd=context)
                    except FileExistsError:
                        pass
                    trash = _directory_at(context, "retired-indexes")
                    trash_path = boundary.root / ".code-context/retired-indexes"
                else:
                    trash_path = _trash_directory()
                    trash = os.open(trash_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                handles.append(trash)
                trash_info = os.fstat(trash)
                if trash_info.st_uid != os.getuid() or trash_info.st_mode & 0o022:
                    raise ValueError("unsafe trash directory")
                if trash_info.st_dev != os.fstat(selected).st_dev:
                    raise SourceError(
                        "INDEX_TRASH_DIFFERENT_VOLUME: use Finder for this storage volume"
                    )
                current = os.stat(project_key, dir_fd=projects, follow_symlinks=False)
                opened = os.fstat(selected)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("derived directory changed")
                destination = "CoLink-index-" + project_id[:12] + "-" + uuid.uuid4().hex[:12]
                os.rename(project_key, destination, src_dir_fd=projects, dst_dir_fd=trash)
                return {
                    "cleared": True,
                    "project_id": project_id,
                    "index_bytes": size,
                    "trash_path": str(trash_path / destination),
                    "native_trash_pending": stage_for_native_trash,
                    "recoverable": True,
                    "source_configuration_recovery_unchanged": True,
                }
            finally:
                for fd in reversed(handles):
                    os.close(fd)
    except SourceError:
        raise
    except BlockingIOError:
        raise SourceError("INDEX_IN_USE: stop this workspace connection before clearing") from None
    except (OSError, ValueError, TypeError, KeyError):
        raise SourceError(
            "INDEX_CLEAR_REFUSED: saved project or index ownership could not be verified"
        ) from None


def _existing_lock(parent, name, handles):
    """Reserve an existing lifecycle lease without creating a misleading file."""
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    handles.append(_locked_at(parent, name))


def _workspace_owners(directory, handles):
    """Hold every known runtime/cache ledger lease until the folder is retired."""
    names = {
        "project-index.lock",
        "live-index.lock",
        "execution.lock",
        "ports.lock",
        "git.lock",
        "recovery.lock",
    }
    for name in os.listdir(directory):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _directory_at(directory, name)
            handles.append(child)
            _workspace_owners(child, handles)
        elif name in names:
            _existing_lock(directory, name, handles)


def _workspace_layout(selected):
    """Only retire recognized app metadata, never a user file added to an index."""
    database_files = {
        "live-index.lock",
        "live-index.sqlite3",
        "live-index.sqlite3-journal",
        "live-index.sqlite3-wal",
        "live-index.sqlite3-shm",
        ".DS_Store",
    }

    def check(parent, name, allowed):
        try:
            directory = _directory_at(parent, name)
        except FileNotFoundError:
            return None
        if not set(os.listdir(directory)) <= allowed:
            os.close(directory)
            raise SourceError("WORKSPACE_CLEAR_REFUSED: unknown saved files must be preserved")
        return directory

    handles = []
    try:
        tunnel = check(selected, "tunnel", {"profile.yaml", ".DS_Store"})
        if tunnel is not None:
            handles.append(tunnel)
        live = _directory_at(selected, "live-v1")
        handles.append(live)
        registry = check(
            live, "registry", {"projects.json", "projects.pre-volume-v1.json", ".DS_Store"}
        )
        if registry is not None:
            handles.append(registry)
        index = check(live, "index", database_files | {"project-index.lock", "projects"})
        if index is not None:
            handles.append(index)
            try:
                projects = _directory_at(index, "projects")
            except FileNotFoundError:
                projects = None
            if projects is not None:
                handles.append(projects)
                for name in os.listdir(projects):
                    if _KEY.fullmatch(name) is None:
                        raise SourceError("WORKSPACE_CLEAR_REFUSED: unknown index folder")
                    project = check(projects, name, database_files)
                    if project is not None:
                        handles.append(project)
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _pending_writes(boundary, identifiers, handles):
    """Global write recovery remains intact; unfinished work prevents forgetting it."""
    path = boundary.root / ".code-context/write-recovery-v1"
    if not path.exists():
        return
    with boundary.root_fd() as root:
        context = _directory_at(root, ".code-context")
        handles.append(context)
        recovery = _directory_at(context, "write-recovery-v1")
        handles.append(recovery)
        _existing_lock(recovery, "recovery.lock", handles)
        try:
            info = os.stat("recovery.sqlite3", dir_fd=recovery, follow_symlinks=False)
        except FileNotFoundError:
            if os.listdir(recovery) not in ([], ["recovery.lock"]):
                raise ValueError("unrecognized recovery data") from None
            return
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("unsafe recovery data")
        db = sqlite3.connect((path / "recovery.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            for identifier in identifiers:
                if db.execute(
                    "SELECT 1 FROM tasks WHERE project_id=? "
                    "AND state NOT IN ('completed','rolled_back') LIMIT 1",
                    (identifier,),
                ).fetchone():
                    raise SourceError(
                        "WORKSPACE_RECOVERY_REQUIRED: finish or recover the saved write task first"
                    )
        finally:
            db.close()


def _inactive_scopes(boundary, relative_runtime):
    """Reject any live/loaded supervisor; never stop processes as part of deletion."""
    from code_context import execution_process
    from code_context.execution_scope import (
        MAX_SCOPE_JSON_BYTES,
        MAX_SCOPE_PLIST_BYTES,
        MacKernel,
        _identity_record_valid,
        _kernel_boot_identity,
        _manifest_valid,
        _read_scope_file,
        _scope_payload,
        _supervisor_plist,
    )

    kernel, boot = None, None
    deadline = time.monotonic() + 5
    for kind in ("terminal-v1", "execution-v1"):
        root = boundary.root / relative_runtime / kind / "scope"
        if not root.is_dir():
            continue
        names = sorted(root.iterdir())
        if len(names) > 4096:
            raise ValueError("too many process scopes")
        for directory in names:
            if time.monotonic() >= deadline:
                raise SourceError("WORKSPACE_TASK_UNVERIFIED: scope check exceeded its budget")
            if re.fullmatch(r"[0-9a-f]{32}", directory.name) is None:
                raise ValueError("unknown process scope")
            source = SourceAccess(directory)
            with source.root_fd() as scope:
                control_raw, _ = _read_scope_file(scope, "control.json", MAX_SCOPE_JSON_BYTES)
                identity_raw, _ = _read_scope_file(scope, "identity.json", MAX_SCOPE_JSON_BYTES)
                plist_raw, _ = _read_scope_file(scope, "job.plist", MAX_SCOPE_PLIST_BYTES)
            control, identity = _scope_payload(control_raw), _scope_payload(identity_raw)
            _manifest_valid(control, name=directory.name)
            process = _identity_record_valid(identity, control, control_raw, plist_raw)
            label = control["label"]
            if plistlib.loads(plist_raw) != _supervisor_plist(
                directory, Path(execution_process.__file__).resolve()
            ):
                raise ValueError("unrecognized process supervisor")
            if sys.platform != "darwin":
                raise SourceError("WORKSPACE_TASK_UNVERIFIED: process ownership needs macOS")
            if boot is None:
                boot = _kernel_boot_identity()
            if control["kernel_boot"] == boot:
                if kernel is None:
                    kernel = MacKernel()
                if kernel.identity(process.pid) is not None or kernel.members(
                    identity["resource_id"]
                ):
                    raise SourceError("WORKSPACE_TASK_IN_USE: stop the workspace tasks first")
            result = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                timeout=min(2, max(0.01, deadline - time.monotonic())),
                check=False,
            )
            if result.returncode != 113:
                raise SourceError("WORKSPACE_TASK_IN_USE: a supervisor still references the folder")


def _inactive_disks(boundary, relative_runtime):
    """Task mounts may be in /private/tmp; check journals and image attachments."""
    runtime = boundary.root / relative_runtime
    journals = sorted((runtime / "execution-v1/jobs").glob("*.disk.json"))
    if len(journals) > 4096:
        raise ValueError("too many task disk records")
    for path in journals:
        record = _registry(boundary, str(path.relative_to(boundary.root)))
        if (
            not isinstance(record, dict)
            or record.get("state") not in {"retired", "detached"}
            or record.get("cleanup_verified") is not True
        ):
            raise SourceError("WORKSPACE_DISK_IN_USE: finish task disk recovery first")
    images = list(runtime.glob("execution-v1/*/*.sparsebundle"))
    if not images:
        return
    if sys.platform != "darwin":
        raise SourceError("WORKSPACE_DISK_UNVERIFIED: image attachment needs macOS")
    result = subprocess.run(
        ["/usr/bin/hdiutil", "info", "-plist"],
        capture_output=True,
        timeout=2,
        check=False,
    )
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        raise SourceError("WORKSPACE_DISK_UNVERIFIED: image attachment could not be checked")
    inventory = plistlib.loads(result.stdout)
    mounted = inventory.get("images") if isinstance(inventory, dict) else None
    if not isinstance(mounted, list) or len(mounted) > 4096:
        raise ValueError("invalid image attachment metadata")
    for image in mounted:
        path = image.get("image-path") if isinstance(image, dict) else None
        if not isinstance(path, str):
            raise ValueError("unknown image attachment")
        if Path(path).is_relative_to(runtime):
            raise SourceError("WORKSPACE_DISK_IN_USE: detach workspace images first")


def trash_project_workspace(workspace: Path, workspace_id: str, *, stage_for_native_trash=False):
    """Recoverably forget one entire selected-directory runtime, including its registry."""
    if not isinstance(workspace_id, str) or _KEY.fullmatch(workspace_id) is None:
        raise SourceError("INVALID_STORAGE_WORKSPACE: choose a saved workspace")
    handles = []
    try:
        boundary = SourceAccess(workspace.expanduser())
        relative = f".code-context/desktop/workspaces/{workspace_id}"
        runtime = relative + "/live-v1"
        with boundary.root_fd() as root:
            parent = os.dup(root)
            handles.append(parent)
            for part in PurePosixPath(".code-context/desktop/workspaces").parts:
                try:
                    parent = _directory_at(parent, part)
                except FileNotFoundError:
                    return {"cleared": False, "already_empty": True, "workspace_id": workspace_id}
                handles.append(parent)
            try:
                selected = _directory_at(parent, workspace_id)
            except FileNotFoundError:
                return {"cleared": False, "already_empty": True, "workspace_id": workspace_id}
            handles.append(selected)
            if not set(os.listdir(selected)) <= {"live-v1", "tunnel", ".DS_Store"}:
                raise ValueError("unknown workspace content")
            live = _directory_at(selected, "live-v1")
            handles.append(live)
            if not set(os.listdir(live)) <= {
                "registry",
                "index",
                "terminal-v1",
                "execution-v1",
                "git-v1",
                "database-profiles",
                "runtime.lock",
                "control.json",
                ".DS_Store",
            }:
                raise ValueError("unknown runtime content")
            handles.append(_locked_at(live, "runtime.lock"))
            registry = _registry(boundary, runtime + "/registry/projects.json")
            source = Path(_text(registry["workspace"]["path"]))
            if (
                registry.get("schema") not in {1, 2, 3}
                or not source.is_absolute()
                or ".." in source.parts
                or hashlib.sha256(str(source).encode()).hexdigest() != workspace_id
                or source.is_relative_to(boundary.root / relative)
                or (boundary.root / relative).is_relative_to(source)
            ):
                raise ValueError("workspace registration mismatch")
            projects = registry["projects"]
            if not isinstance(projects, list) or not 1 <= len(projects) <= 64:
                raise ValueError("invalid project registry")
            identifiers = [validate_project(_text(p["project_id"], 100)) for p in projects]
            names = [_text(p["display_name"], 120) for p in projects]
            _workspace_layout(selected)
            size, allocated = _tree_storage(boundary, relative)
            _workspace_owners(live, handles)
            _pending_writes(boundary, identifiers, handles)
            _inactive_disks(boundary, runtime)
            _inactive_scopes(boundary, runtime)
            registry_version = os.stat(
                boundary.root / runtime / "registry/projects.json", follow_symlinks=False
            )
            # The registry/source must remain the one selected before staging.
            if _registry(boundary, runtime + "/registry/projects.json") != registry:
                raise ValueError("workspace registry changed")
            if stage_for_native_trash:
                context = _directory_at(root, ".code-context")
                handles.append(context)
                try:
                    os.mkdir("retired-workspaces", 0o700, dir_fd=context)
                except FileExistsError:
                    pass
                trash = _directory_at(context, "retired-workspaces")
                trash_path = boundary.root / ".code-context/retired-workspaces"
            else:
                trash_path = _trash_directory()
                trash = os.open(trash_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(trash)
            if os.fstat(trash).st_dev != os.fstat(selected).st_dev:
                raise SourceError("WORKSPACE_TRASH_DIFFERENT_VOLUME: use Finder on this volume")
            current = os.stat(workspace_id, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(selected)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("workspace directory changed")
            after = os.stat(
                boundary.root / runtime / "registry/projects.json", follow_symlinks=False
            )
            if (after.st_ino, after.st_mtime_ns, after.st_size) != (
                registry_version.st_ino,
                registry_version.st_mtime_ns,
                registry_version.st_size,
            ):
                raise ValueError("workspace registry changed")
            destination = "CoLink-workspace-" + workspace_id[:12] + "-" + uuid.uuid4().hex[:12]
            os.rename(workspace_id, destination, src_dir_fd=parent, dst_dir_fd=trash)
            os.fsync(parent)
            os.fsync(trash)
            return {
                "cleared": True,
                "workspace_id": workspace_id,
                "project_count": len(projects),
                "project_names": names,
                "workspace_bytes": size,
                "workspace_allocated_bytes": allocated,
                "trash_path": str(trash_path / destination),
                "native_trash_pending": stage_for_native_trash,
                "recoverable": True,
                "source_global_configuration_recovery_unchanged": True,
                "rebuild": "select the original directory and start its connection again",
            }
    except SourceError as exc:
        if str(exc).startswith("INDEX_IN_USE:"):
            raise SourceError(
                "WORKSPACE_IN_USE: close workspace connections before clearing"
            ) from None
        if str(exc).startswith(("WORKSPACE_", "STORAGE_METADATA_LIMIT:")):
            raise
        raise SourceError(
            "WORKSPACE_CLEAR_REFUSED: workspace ownership or task state is unverified"
        ) from None
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
        raise SourceError(
            "WORKSPACE_CLEAR_REFUSED: workspace ownership or task state is unverified"
        ) from None
    finally:
        for fd in reversed(handles):
            os.close(fd)

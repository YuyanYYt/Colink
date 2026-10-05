"""One local workspace runtime owns registration, invalidation and lazy indexing."""

import fcntl
import os
import stat
from pathlib import Path

from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.live_watch import WatchCoordinator
from code_context.local_control import LocalControl, control_request, private_directory
from code_context.project_registry import ProjectRegistry
from code_context.source_access import SourceError


def initialize_workspace(root: Path, data_dir: Path, name: str | None = None):
    """Local selected-directory authorization, never an MCP-accessible operation."""
    registry = ProjectRegistry(root, data_dir=data_dir / "registry")
    discovery = registry.discover()
    project = registry.register("", display_name=name, enabled=True)
    return {"initialized": True, "project_id": project, "discovery": discovery}


class WorkspaceRuntime:
    def __init__(self, root: Path, data_dir: Path, socket_dir: Path | None = None):
        self.state = private_directory(data_dir)
        self.data_dir = self.state.root
        self.lock_fd = None
        self.control = None
        self.backend = None
        self.watcher = None
        self.closed = False
        with self.state.root_fd() as directory:
            fd = os.open(
                "runtime.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                os.close(fd)
                raise SourceError("UNSAFE_RUNTIME_LOCK: private regular lock required")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise SourceError(
                    "WORKSPACE_ALREADY_RUNNING: stop the current runtime first"
                ) from None
            self.lock_fd = fd
        try:
            self.registry = ProjectRegistry(root, data_dir=self.data_dir / "registry")
            if not self.registry.list_projects(enabled_only=False)["projects"]:
                raise SourceError(
                    "WORKSPACE_NOT_CONFIGURED: select and enable a project locally first"
                )
            self.backend = LiveQueries(registry=self.registry)
            index_directory = private_directory(self.data_dir / "index")
            self.backend.index_service = LiveIndexService(
                index_directory.root,
                max_bytes=64 * 1024 * 1024,
                max_peak_bytes=128 * 1024 * 1024,
            )
            self.watcher = WatchCoordinator(
                self.backend.sources, self.backend.index_service.invalidate
            )
            self.backend.watcher = self.watcher
            # Keep AF_UNIX path lengths bounded; never use a system temp directory.
            state_boundary = next(
                (
                    parent
                    for parent in self.data_dir.parents
                    if parent.name in {".code-context", ".artifacts"}
                ),
                self.registry.workspace / ".code-context",
            )
            controls = socket_dir or (state_boundary / "controls")
            self.control = LocalControl(self.data_dir, controls, self.handle_control)
        except BaseException:
            self.close()
            raise

    def start(self):
        self.control.start()
        self.watcher.start()

    def status(self):
        return {
            "state": "stopped" if self.closed else "live_read",
            "source_mode": "live",
            "write_enabled": False,
            "write_available": False,
            "active_task": None,
            "recovery_required": False,
            "projects": self.registry.list_projects(enabled_only=False)["projects"],
            "watcher": self.watcher.status(),
            "history_available": False,
        }

    def handle_control(self, action, parameters):
        if self.closed:
            raise SourceError("WORKSPACE_STOPPED: restart locally")
        if action == "status" and not parameters:
            return self.status()
        if action == "discover" and not parameters:
            result = self.registry.discover()
            self.backend.refresh_sources()
            return result
        if action == "set_enabled" and set(parameters) == {"project_id", "enabled"}:
            self.registry.set_enabled(parameters["project_id"], parameters["enabled"])
            self.backend.refresh_sources()
            return self.status()
        if action == "register" and set(parameters) <= {
            "relative_root",
            "display_name",
            "aliases",
            "enabled",
        }:
            values = dict(parameters)
            if "aliases" in values:
                values["aliases"] = tuple(values["aliases"])
            project = self.registry.register(**values)
            self.backend.refresh_sources()
            return {"project_id": project, **self.status()}
        if action in {"enable_write", "disable_write", "recover"}:
            raise SourceError("WRITE_NOT_IMPLEMENTED: controlled-write gate is not ready")
        raise SourceError("UNKNOWN_CONTROL_ACTION: use a supported local action")

    def close(self):
        self.closed = True
        if self.control is not None:
            self.control.close()
        if self.watcher is not None:
            self.watcher.close()
        if self.backend is not None:
            self.backend.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()


def read_workspace_status(data_dir: Path):
    """Read-only lifecycle inspection; do not initialize an idle selected folder."""
    fallback = {
        "state": "stopped",
        "status": "not_running",
        "running": False,
        "initialized": (data_dir / "registry" / "projects.json").is_file(),
        "source_mode": "live",
        "write_enabled": False,
        "write_available": False,
        "projects": [],
        "active_task": None,
        "recovery_required": False,
    }
    if not (data_dir / "control.json").exists():
        return fallback
    try:
        return {
            **control_request(data_dir, "status"),
            "status": "ready",
            "running": True,
            "initialized": True,
        }
    except SourceError:
        return fallback

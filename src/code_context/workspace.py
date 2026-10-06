"""One local workspace runtime owns registration, invalidation and lazy indexing."""

import fcntl
import os
import stat
import threading
from pathlib import Path

from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.live_watch import WatchCoordinator
from code_context.local_control import LocalControl, control_request, private_directory
from code_context.project_registry import ProjectRegistry
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceError
from code_context.write_coordinator import WriteCoordinator


def initialize_workspace(root: Path, data_dir: Path, name: str | None = None):
    """Local selected-directory authorization, never an MCP-accessible operation."""
    registry = ProjectRegistry(root, data_dir=data_dir / "registry")
    discovery = registry.discover()
    project = registry.register("", display_name=name, enabled=True)
    return {"initialized": True, "project_id": project, "discovery": discovery}


class WorkspaceRuntime:
    def __init__(
        self,
        root: Path,
        data_dir: Path,
        socket_dir: Path | None = None,
        *,
        recovery_dir: Path | None = None,
    ):
        self.state = private_directory(data_dir)
        self.data_dir = self.state.root
        self.lock_fd = None
        self.control = None
        self.backend = None
        self.watcher = None
        self.recovery_store = None
        self.write_coordinator = None
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
                (parent for parent in self.data_dir.parents if parent.name == ".code-context"),
                next(
                    (parent for parent in self.data_dir.parents if parent.name == ".artifacts"),
                    self.data_dir.parent,
                ),
            )
            controls = socket_dir or (state_boundary / "controls")
            self.control = LocalControl(
                self.data_dir, controls, self.handle_control, on_disconnect=self._control_lost
            )
            # One global recovery budget/lease, never a per-source mirror DB.
            # Callers/tests can pass a new isolated private recovery directory.
            recovery_dir = (
                recovery_dir if recovery_dir is not None else state_boundary / "write-recovery-v1"
            )
            self.recovery_store = RecoveryStore(recovery_dir)
            self.write_coordinator = WriteCoordinator(
                self.recovery_store,
                self.backend.source,
                control_alive=self._control_alive,
                on_change=self._on_change,
            )
            self.backend.write_coordinator = self.write_coordinator
        except BaseException:
            self.close()
            raise

    def start(self):
        if self.closed:
            raise SourceError("WORKSPACE_STOPPED: restart locally")
        try:
            self.control.start()
            self.watcher.start()
        except BaseException:
            self.close()
            raise

    def _control_lost(self):
        # May be called under a Source lock: do not acquire coordinator.lock.
        if self.write_coordinator is not None:
            self.write_coordinator.stop_requested.set()
            self.write_coordinator.grants = {}

    def _control_alive(self):
        return not self.closed and self.control is not None and self.control.is_alive()

    def _on_change(self, project_id):
        self.backend.contexts.invalidate_project(project_id)
        self.backend.index_service.invalidate(project_id)

    def _local_only(self):
        if not self._control_alive() or threading.current_thread() is not self.control.thread:
            raise SourceError("LOCAL_CONTROL_REQUIRED: use the authenticated local socket")

    def _revoke(self):
        # disable() sets stop_requested before waiting for an in-flight writer.
        self.write_coordinator.disable()

    def begin_task(self, project_id, request_id, *, title="", paths=None):
        return self.write_coordinator.begin_write_task(
            project_id, request_id, title=title, paths=paths
        )

    def apply_edit(self, project_id, task_id, request_id, path, expected_sha256, edit):
        return self.write_coordinator.apply_edit(
            project_id, task_id, request_id, path, expected_sha256, edit
        )

    def create_file(self, project_id, task_id, request_id, path, content):
        return self.write_coordinator.create_file(project_id, task_id, request_id, path, content)

    def create_directory(self, project_id, task_id, request_id, path):
        return self.write_coordinator.create_directory(project_id, task_id, request_id, path)

    def delete_file(self, project_id, task_id, request_id, path, expected_sha256):
        return self.write_coordinator.delete_file(
            project_id, task_id, request_id, path, expected_sha256
        )

    def finish_task(self, project_id, task_id, request_id):
        return self.write_coordinator.finish_write_task(project_id, task_id, request_id)

    def recover_local(self, project_id):
        self._local_only()
        return self.write_coordinator.recover(project_id)

    def get_task_diff(self, project_id, **parameters):
        return self.write_coordinator.get_diff(project_id, **parameters)

    def guard_read(self, project_id):
        return self.write_coordinator.guard_read(project_id)

    def status(self):
        if not self._control_alive():
            self._control_lost()
        writes = self.write_coordinator.status()
        available = self._control_alive()
        return {
            "state": "stopped" if self.closed else "live_read",
            "source_mode": "live",
            **writes,
            "write_available": available,
            "write_enabled": available and writes["write_enabled"],
            "write_projects": writes["write_projects"] if available else [],
            "local_actions": [
                "enable_write",
                "disable_write",
                "recover_write",
            ]
            if available
            else [],
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
            self._local_only()
            self._revoke()
            result = self.registry.discover()
            self.backend.refresh_sources()
            return result
        if action == "set_enabled" and set(parameters) == {"project_id", "enabled"}:
            self._local_only()
            self._revoke()
            self.registry.set_enabled(parameters["project_id"], parameters["enabled"])
            self.backend.refresh_sources()
            return self.status()
        if action == "register" and set(parameters) <= {
            "relative_root",
            "display_name",
            "aliases",
            "enabled",
        }:
            self._local_only()
            self._revoke()
            values = dict(parameters)
            if "aliases" in values:
                values["aliases"] = tuple(values["aliases"])
            project = self.registry.register(**values)
            self.backend.refresh_sources()
            return {"project_id": project, **self.status()}
        if action == "enable_write" and set(parameters) == {"project_ids"}:
            self._local_only()
            self.write_coordinator.enable(parameters["project_ids"])
            return self.status()
        if action == "disable_write" and not parameters:
            self._local_only()
            self._revoke()
            return self.status()
        if action == "recover_write" and set(parameters) == {"project_id"}:
            return self.recover_local(parameters["project_id"])
        raise SourceError("UNKNOWN_CONTROL_ACTION: use a supported local action")

    def close(self):
        self.closed = True
        try:
            if self.write_coordinator is not None:
                self.write_coordinator.close()
        finally:
            try:
                if self.control is not None:
                    self.control.close()
            finally:
                try:
                    if self.watcher is not None:
                        self.watcher.close()
                finally:
                    try:
                        if self.backend is not None:
                            self.backend.close()
                    finally:
                        try:
                            if self.recovery_store is not None:
                                self.recovery_store.close()
                        finally:
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

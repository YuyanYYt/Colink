"""Local lifecycle support for Colink. No model calls or remote write tools.

The native app owns a pipe to this supervisor. A stop request, pipe EOF, signal,
or unexpected client exit shuts down the entire owned process group. Merely
inspecting a selected folder never scans it or starts a remote connection.
"""

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from code_context.client import SyncError
from code_context.scanner import Scanner
from code_context.tunnel import _read_private, load_profile, prepare_profile, tunnel_status


@dataclass(frozen=True)
class DesktopBinding:
    root: Path
    project: str
    data: Path
    profile: Path
    tunnel_id: str


def desktop_binding(workspace: Path, root: Path) -> DesktopBinding:
    workspace = workspace.expanduser().resolve()
    baseline = workspace / ".code-context/tunnel/profile.yaml"
    config, original_root, project, data = load_profile(baseline, verify_source=False)
    selected = Scanner(root.expanduser()).root
    # Prevent an accidental whole-home/system selection in this small desktop UI.
    if selected in {Path("/"), Path.home().resolve(), workspace.parent} or selected.parts[:2] in {
        ("/", "System"),
        ("/", "Library"),
        ("/", "private"),
    }:
        raise SyncError("select a specific code project, not a home or system directory")
    tunnel_id = config["control_plane"]["tunnel_id"]
    if selected == original_root:
        return DesktopBinding(selected, project, data, baseline, tunnel_id)
    identity = hashlib.sha256(str(selected).encode()).hexdigest()
    directory = workspace / ".code-context/desktop/sources" / identity
    return DesktopBinding(
        selected,
        "folder-" + identity[:16],
        directory / "data",
        directory / "tunnel/profile.yaml",
        tunnel_id,
    )


def _lock_is_held(path: Path) -> bool:
    try:
        with path.open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def _profiles(workspace: Path) -> list[Path]:
    profiles = [workspace / ".code-context/tunnel/profile.yaml"]
    directory = workspace / ".code-context/desktop/sources"
    if directory.exists():
        profiles.extend(directory.glob("*/tunnel/profile.yaml"))
    if len(profiles) > 128:
        raise SyncError("too many saved desktop sources; review them before creating more")
    return profiles


def _running_profile(workspace: Path) -> Path | None:
    from code_context.local import read_local_mirror_status

    for path in _profiles(workspace):
        _, _, _, data = load_profile(path, verify_source=False)
        if read_local_mirror_status(data)["running"]:
            return path
    return None


def desktop_status(workspace: Path, root: Path) -> dict:
    workspace = workspace.expanduser().resolve()
    binding = desktop_binding(workspace, root)
    directory = workspace / ".code-context/desktop"
    supervised = _lock_is_held(directory / "connection.lock")
    runtime = {}
    runtime_file = directory / "runtime.json"
    if runtime_file.exists():
        try:
            runtime = json.loads(_read_private(runtime_file))
        except (ValueError, SyncError):
            runtime = {}
    running_profile = _running_profile(workspace)
    selected_status = tunnel_status(binding.profile) if binding.profile.exists() else {}
    selected_active = bool(selected_status.get("local_mirror", {}).get("running"))
    return {
        "selected_root": str(binding.root),
        "project_id": binding.project,
        "profile": str(binding.profile),
        "data_dir": str(binding.data),
        "supervised": supervised,
        "app_pid": runtime.get("app_pid") if supervised else None,
        "external_active": running_profile is not None and not supervised,
        "active_elsewhere": running_profile is not None and running_profile != binding.profile,
        "running": selected_active,
        "ready": bool(
            selected_status.get("ready")
            and selected_active
            and selected_status.get("local_mirror", {}).get("status") == "ready"
        ),
        "revision": selected_status.get("local_mirror", {}).get("revision", 0),
        "tracked_files": selected_status.get("local_mirror", {}).get("tracked_files", 0),
        "mirror_status": selected_status.get("local_mirror", {}).get("status", "not_running"),
        "phase": runtime.get("phase", "stopped") if supervised else "stopped",
        "auto_start": False,
        "chatgpt_web_verified": False,
    }


def _record_runtime(directory: Path, **values) -> None:
    # Generated local state, never a source file. Preserve the last state after exit.
    import tempfile

    fd, temporary = tempfile.mkstemp(prefix="runtime-", suffix=".json", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(values, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / "runtime.json")


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


def _stop_owned_group(child: subprocess.Popen) -> None:
    # The group was created by this supervisor with start_new_session=True.
    # Never signal a PID found in a status file or an unrelated external client.
    group = child.pid
    for requested_signal, seconds in ((signal.SIGINT, 4), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        if not _group_alive(group):
            break
        try:
            os.killpg(group, requested_signal)
        except ProcessLookupError:
            # The last process may exit between the liveness probe and signal.
            # Still reap the owned leader and verify no descendants remain.
            break
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            child.poll()  # reap the leader while also checking its descendants
            if not _group_alive(group):
                break
            time.sleep(0.05)
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired as exc:
        raise SyncError("owned connection could not be stopped; do not start another") from exc
    if _group_alive(group):
        raise SyncError("owned connection still has live descendants; do not start another")


def run_desktop(workspace: Path, root: Path, client: str, app_pid: int = 0) -> int:
    workspace = workspace.expanduser().resolve()
    binding = desktop_binding(workspace, root)
    directory = workspace / ".code-context/desktop"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (directory / "connection.lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("Colink already owns a connection") from exc
        if _running_profile(workspace) is not None:
            raise SyncError("an external connection is running; stop it in its owning application")
        if not binding.profile.exists():
            prepare_profile(
                binding.root, binding.project, binding.data, binding.tunnel_id, binding.profile
            )
        else:
            _, saved_root, saved_project, saved_data = load_profile(binding.profile)
            if (saved_root, saved_project, saved_data) != (
                binding.root,
                binding.project,
                binding.data,
            ):
                raise SyncError("saved connection does not match the selected folder")
        stopped = threading.Event()

        def on_signal(*_):
            stopped.set()

        previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}

        def control_pipe():
            # Closing the native app also closes this pipe. No reconnect loop exists.
            while True:
                line = sys.stdin.readline()
                if not line or line.strip() == "stop":
                    stopped.set()
                    return

        threading.Thread(target=control_pipe, name="desktop-control", daemon=True).start()
        record = {"app_pid": app_pid, "root": str(binding.root), "profile": str(binding.profile)}
        child = None
        exit_code = 0
        try:
            _record_runtime(directory, **record, phase="starting")
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "code_context",
                    "tunnel-run",
                    "--profile",
                    str(binding.profile),
                    "--env-file",
                    str(workspace / ".env.local"),
                    "--client",
                    client,
                ],
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _record_runtime(directory, **record, phase="running")
            while not stopped.wait(0.1):
                if child.poll() is not None:
                    exit_code = 1
                    break
        finally:
            _record_runtime(directory, **record, phase="stopping")
            try:
                if child is not None:
                    _stop_owned_group(child)
            except SyncError:
                _record_runtime(directory, **record, phase="stop_failed", exit_code=1)
                raise
            else:
                _record_runtime(directory, **record, phase="stopped", exit_code=exit_code)
            finally:
                for requested_signal, handler in previous.items():
                    signal.signal(requested_signal, handler)
        return exit_code
    finally:
        lock.close()

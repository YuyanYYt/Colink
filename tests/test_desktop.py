import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from code_context.client import SyncError
from code_context.desktop import _stop_owned_group, desktop_binding, desktop_status
from code_context.local import LocalMirror, read_local_mirror_status
from code_context.scanner import ScanError
from code_context.storage import MirrorStore
from code_context.tunnel import load_profile, prepare_profile


@pytest.fixture
def desktop_workspace(tmp_path):
    workspace = tmp_path.resolve() / "workspace"
    root = workspace / "examples/sample_project"
    root.mkdir(parents=True)
    (root / "main.py").write_text("value = 1\n")
    prepare_profile(
        root,
        "sample",
        workspace / ".code-context/local-sample",
        "tunnel_" + "0" * 32,
        workspace / ".code-context/tunnel/profile.yaml",
    )
    key = workspace / ".env.local"
    key.write_text("OPENAI_API_KEY=sk-" + "k" * 32 + "\n")
    key.chmod(0o600)
    return workspace, root


def test_desktop_inspection_is_local_read_only(desktop_workspace, monkeypatch):
    workspace, root = desktop_workspace
    monkeypatch.setattr(Path, "home", lambda: workspace.parent / "home")
    status = desktop_status(workspace, root)
    assert status["project_id"] == "sample"
    assert not status["running"] and not status["auto_start"]
    assert not status["chatgpt_web_verified"]
    assert not (workspace / ".code-context/desktop").exists()
    assert not (workspace / ".code-context/local-sample").exists()


def test_folder_selection_uses_separate_identity_without_scanning(desktop_workspace, monkeypatch):
    workspace, root = desktop_workspace
    selected = workspace / "another project 中文"
    selected.mkdir()
    (selected / "other.py").write_text("another = True\n")
    monkeypatch.setattr("code_context.desktop.Scanner.scan", lambda *_: pytest.fail("no scan"))
    baseline = desktop_binding(workspace, root)
    chosen = desktop_binding(workspace, selected)
    assert chosen.root == selected and chosen.tunnel_id == baseline.tunnel_id
    assert chosen.data != baseline.data and chosen.project != baseline.project
    assert chosen == desktop_binding(workspace, selected)
    assert not chosen.data.exists() and not chosen.profile.exists()
    status = desktop_status(workspace, selected)
    assert status["revision"] == 0 and not status["running"]
    assert not chosen.data.parent.exists()


def test_desktop_rejects_home_system_parent_and_symlink(desktop_workspace, monkeypatch):
    workspace, root = desktop_workspace
    monkeypatch.setattr(Path, "home", lambda: root)
    with pytest.raises(SyncError, match="specific code project"):
        desktop_binding(workspace, root)
    with pytest.raises(SyncError, match="specific code project"):
        desktop_binding(workspace, workspace.parent)
    with pytest.raises(SyncError):
        desktop_binding(workspace, Path("/"))
    link = workspace / "alias"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ScanError, match="without symlinks"):
        desktop_binding(workspace, link)


def test_external_writer_is_reported_but_never_stopped(desktop_workspace):
    workspace, root = desktop_workspace
    binding = desktop_binding(workspace, root)
    with LocalMirror(root, "sample", binding.data) as mirror:
        mirror.sync_once()
        status = desktop_status(workspace, root)
        assert status["external_active"] and status["running"]
        assert not status["supervised"] and status["app_pid"] is None
        assert read_local_mirror_status(binding.data)["running"]


def test_new_folder_can_be_selected_after_old_source_is_moved(desktop_workspace):
    workspace, root = desktop_workspace
    moved = root.parent / "moved-source"
    root.rename(moved)
    chosen = desktop_binding(workspace, moved)
    assert chosen.root == moved
    status = desktop_status(workspace, moved)
    assert not status["running"] and not status["auto_start"]
    assert not chosen.data.exists()


def test_removed_source_cannot_hide_an_existing_live_writer(desktop_workspace):
    workspace, root = desktop_workspace
    binding = desktop_binding(workspace, root)
    with LocalMirror(root, "sample", binding.data) as mirror:
        mirror.sync_once()
        moved = root.parent / "moved-source"
        root.rename(moved)
        status = desktop_status(workspace, moved)
        assert status["external_active"] and status["active_elsewhere"]
        assert read_local_mirror_status(binding.data)["running"]


def test_stop_reaps_owned_leader_when_group_exits_between_probe_and_signal(monkeypatch):
    child = SimpleNamespace(pid=999999, wait=Mock(return_value=0))
    alive = iter([True, False])
    monkeypatch.setattr("code_context.desktop._group_alive", lambda _: next(alive))

    def exited_group(group, requested_signal):
        assert group == child.pid and requested_signal == signal.SIGINT
        raise ProcessLookupError

    monkeypatch.setattr("code_context.desktop.os.killpg", exited_group)
    _stop_owned_group(child)
    child.wait.assert_called_once_with(timeout=2)


@pytest.fixture
def fake_official_client(desktop_workspace):
    workspace, _ = desktop_workspace
    script = workspace / "fake-official-client"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, shlex, signal, subprocess, sys, threading\n"
        "profile = pathlib.Path(sys.argv[sys.argv.index('--profile-file') + 1])\n"
        "configuration = json.loads(profile.read_text())\n"
        "child = subprocess.Popen(shlex.split(configuration['mcp']['commands'][0]['command']), "
        "stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)\n"
        "pathlib.Path('fake-mcp.pid').write_text(str(child.pid))\n"
        "stopped = threading.Event()\n"
        "signal.signal(signal.SIGINT, lambda *_: stopped.set())\n"
        "signal.signal(signal.SIGTERM, lambda *_: stopped.set())\n"
        "try:\n"
        "    while not stopped.wait(.1):\n"
        "        if child.poll() is not None: break\n"
        "finally:\n"
        # The whole group has already received SIGINT. Do not race the child's
        # SQLite shutdown with an immediate second, fatal SIGTERM from this mock.
        "    child.stdin.close()\n"
        "    try: child.wait(timeout=3)\n"
        "    except subprocess.TimeoutExpired: child.terminate()\n"
        "    child.wait(timeout=5)\n"
    )
    script.chmod(0o700)
    return script


def launch(workspace, root, client):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "code_context",
            "desktop-run",
            "--workspace",
            str(workspace),
            "--root",
            str(root),
            "--client",
            str(client),
            "--app-pid",
            str(os.getpid()),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def await_running(workspace, root, supervisor):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        assert supervisor.poll() is None, "supervisor exited before the mirror was ready"
        try:
            status = desktop_status(workspace, root)
            # A writer lock can be held before startup reconciliation finishes.
            # This mock does not implement the tunnel health server, so wait for
            # the real mirror's ready state, not just a previously saved revision.
            if status["running"] and status["mirror_status"] == "ready" and status["revision"] > 0:
                return status
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("desktop mirror did not start")


@pytest.mark.parametrize("stop_mode", ["request", "pipe_eof", "signal"])
def test_supervisor_shutdown_stops_entire_tree_and_stays_closed(
    desktop_workspace, fake_official_client, stop_mode
):
    workspace, root = desktop_workspace
    supervisor = launch(workspace, root, fake_official_client)
    try:
        status = await_running(workspace, root, supervisor)
        assert status["supervised"] and status["app_pid"] == os.getpid()
        server_pid = int((workspace / "fake-mcp.pid").read_text())
        if stop_mode == "request":
            supervisor.stdin.write("stop\n")
            supervisor.stdin.flush()
        elif stop_mode == "pipe_eof":
            supervisor.stdin.close()
        else:
            supervisor.send_signal(signal.SIGTERM)
        assert supervisor.wait(timeout=12) == 0, supervisor.stderr.read()
        with pytest.raises(ProcessLookupError):
            os.kill(server_pid, 0)
        stopped = desktop_status(workspace, root)
        assert not stopped["running"] and not stopped["supervised"] and not stopped["auto_start"]
        runtime = json.loads((workspace / ".code-context/desktop/runtime.json").read_text())
        assert runtime["phase"] == "stopped"
        assert desktop_status(workspace, root)["phase"] == "stopped"
        assert not read_local_mirror_status(Path(status["data_dir"]))["running"]
    finally:
        if supervisor.poll() is None:
            supervisor.send_signal(signal.SIGTERM)
            supervisor.wait(timeout=12)


def test_desktop_single_connection_and_reopen_recovery(desktop_workspace, fake_official_client):
    workspace, root = desktop_workspace
    first = launch(workspace, root, fake_official_client)
    try:
        await_running(workspace, root, first)
        duplicate = launch(workspace, root, fake_official_client)
        assert duplicate.wait(timeout=5) == 1
        assert desktop_status(workspace, root)["running"]
        first.stdin.write("stop\n")
        first.stdin.flush()
        assert first.wait(timeout=12) == 0
    finally:
        if first.poll() is None:
            first.send_signal(signal.SIGTERM)
            first.wait(timeout=12)
    (root / "main.py").write_text("value = 2\n")
    # Inspecting/reopening alone cannot pick up or share the offline edit.
    assert desktop_status(workspace, root)["revision"] == 1
    second = launch(workspace, root, fake_official_client)
    try:
        status = await_running(workspace, root, second)
        assert status["revision"] == 2
        mirror = MirrorStore(Path(status["data_dir"]) / "server/mirror.sqlite3")
        assert mirror.read_file("sample", "main.py", 1)["content"] == "value = 1\n"
        assert mirror.read_file("sample", "main.py", 2)["content"] == "value = 2\n"
        assert load_profile(Path(status["profile"]))[1] == root
        second.stdin.write("stop\n")
        second.stdin.flush()
        assert second.wait(timeout=12) == 0
    finally:
        if second.poll() is None:
            second.send_signal(signal.SIGTERM)
            second.wait(timeout=12)

"""Local socket/write lifecycle proof on fresh synthetic sources only.

No remote tunnel, UI, MCP transport, original mirror or user source is exercised.
Every recovery root and short AF_UNIX path is inside this project's .artifacts.
"""

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

import code_context.write_operations as operations
from code_context.local_control import _receive, _send, control_request, read_state, write_state
from code_context.read_context import ContextError
from code_context.source_access import SourceError
from code_context.workspace import WorkspaceRuntime, initialize_workspace
from code_context.write_coordinator import WriteError


def socket_directory():
    # Keep macOS's AF_UNIX byte limit independent of pytest's long test names.
    return Path(tempfile.mkdtemp(prefix="wgate-", dir=Path.cwd() / ".artifacts"))


def wait_until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("bounded local lifecycle wait did not complete")
        time.sleep(0.01)


@pytest.fixture
def configured(tmp_path):
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "README.md").write_text("synthetic workspace\n")
    for name in ("a", "b"):
        project = root / name
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname="synthetic"\n')
        (project / "module.py").write_text("def value():\n    return 1\n")
    state = tmp_path / "state"
    initialize_workspace(root, state)
    return root, state, tmp_path / "isolated-recovery"


@pytest.fixture
def runtime(configured):
    root, state, recovery = configured
    r = WorkspaceRuntime(root, state, socket_directory(), recovery_dir=recovery)
    r.start()
    try:
        for row in r.registry.list_projects(enabled_only=False)["projects"]:
            if row["relative_root"] in {"a", "b"}:
                control_request(
                    state, "set_enabled", {"project_id": row["project_id"], "enabled": True}
                )
        yield r
    finally:
        r.close()


def project(r, relative_root="a"):
    return next(
        row["project_id"]
        for row in r.registry.list_projects(enabled_only=False)["projects"]
        if row["relative_root"] == relative_root
    )


def enable(r, project_id=None):
    return control_request(r.data_dir, "enable_write", {"project_ids": [project_id or project(r)]})


def begin(r, project_id=None, **kwargs):
    pid = project_id or project(r)
    return r.begin_task(pid, r.status()["next_task_request_id"], **kwargs)["task_id"]


def edit(r, task_id, request_id="edit_0001"):
    pid = project(r)
    return r.apply_edit(
        pid,
        task_id,
        request_id,
        "module.py",
        r.backend.source(pid).fingerprint("module.py"),
        {"kind": "replace_fragment", "old_text": "return 1", "new_text": "return 2"},
    )


def test_default_off_real_socket_authorized_source_and_no_mirror_takeover(runtime):
    r = runtime
    pid = project(r)
    status = control_request(r.data_dir, "status")
    assert status["write_available"] and not status["write_enabled"]
    assert r.backend.write_coordinator is r.write_coordinator
    assert r.write_coordinator.source_provider(pid) is r.backend.source(pid)
    assert not (r.data_dir / "server" / "mirror.sqlite3").exists()
    assert r.recovery_store.root == r.data_dir.parent / "isolated-recovery"
    assert r.recovery_store.max_bytes == 64 * 1024 * 1024
    assert r.recovery_store.max_peak_bytes == 128 * 1024 * 1024
    assert "return 1" in r.backend.read_file(pid, "module.py")["content"]
    with pytest.raises(WriteError, match="WRITE_DISABLED"):
        begin(r)
    granted = enable(r)
    assert granted["write_available"] and granted["write_enabled"]
    assert granted["write_projects"] == [pid]
    task_id = begin(r, paths=["module.py"])
    assert task_id.startswith("wt_")
    with pytest.raises(WriteError, match="WRITE_NOT_AUTHORIZED"):
        begin(r, project(r, "b"))
    assert r.backend.source(project(r, "b")).metrics["body_reads"] == 0
    disabled = control_request(r.data_dir, "disable_write")
    assert not disabled["write_enabled"] and disabled["write_projects"] == []
    with pytest.raises(WriteError, match="WRITE_DISABLED"):
        edit(r, task_id)


@pytest.mark.parametrize("kind", ["managed", "explicit", "managed_priority"])
def test_default_global_recovery_comes_from_declared_state_not_cwd(configured, tmp_path, kind):
    root, _, _ = configured
    if kind == "explicit":
        state = tmp_path / "declared" / "profile"
        # This test is inside the declared .artifacts state boundary. All
        # profiles below it share a single bounded recovery root, not cwd.
        expected = next(p for p in state.parents if p.name == ".artifacts") / "write-recovery-v1"
    elif kind == "managed":
        state = tmp_path / ".code-context" / "profiles" / "one"
        expected = tmp_path / ".code-context" / "write-recovery-v1"
    else:
        state = tmp_path / ".code-context" / ".artifacts" / "profiles" / "one"
        expected = tmp_path / ".code-context" / "write-recovery-v1"
    initialize_workspace(root, state)
    mirror = state / "server"
    mirror.mkdir()
    (mirror / "mirror.sqlite3").write_bytes(b"synthetic old mirror sentinel")
    with WorkspaceRuntime(root, state, socket_directory()) as r:
        assert r.recovery_store.root == expected
        assert not expected.is_relative_to(root)
        assert (mirror / "mirror.sqlite3").read_bytes() == b"synthetic old mirror sentinel"


def test_profiles_share_one_recovery_lease_and_release_on_close(configured, tmp_path):
    root, _, _ = configured
    boundary = tmp_path / ".code-context"
    states = [boundary / "profiles" / name for name in ("one", "two")]
    for state in states:
        initialize_workspace(root, state)
    first = WorkspaceRuntime(root, states[0], socket_directory())
    try:
        with pytest.raises(SourceError, match="RECOVERY_ALREADY_OPEN"):
            WorkspaceRuntime(root, states[1], socket_directory())
        assert first.recovery_store.root == boundary / "write-recovery-v1"
    finally:
        first.close()
    # Failed construction released profile two's runtime lock as well.
    with WorkspaceRuntime(root, states[1], socket_directory()) as second:
        assert second.recovery_store.root == boundary / "write-recovery-v1"
        assert not second.status()["write_enabled"]


@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("enable_write", {"project_ids": []}),
        ("disable_write", {}),
        ("recover_write", {"project_id": "synthetic"}),
        ("discover", {}),
        ("register", {}),
        ("set_enabled", {"project_id": "synthetic", "enabled": False}),
        (
            "rollback_write_task",
            {"project_id": "synthetic", "task_id": "wt_fake", "request_id": "undo_0001"},
        ),
    ],
)
def test_privileged_control_actions_require_actual_authenticated_control_thread(
    runtime, action, parameters
):
    with pytest.raises(SourceError, match="LOCAL_CONTROL_REQUIRED"):
        runtime.handle_control(action, parameters)
    assert not runtime.write_coordinator.grants


@pytest.mark.parametrize("scope", [[], "all", ["unknown"], ["duplicate", "duplicate"]])
def test_explicit_invalid_write_scope_revokes_previous_grant(runtime, scope):
    enable(runtime)
    with pytest.raises(SourceError):
        control_request(runtime.data_dir, "enable_write", {"project_ids": scope})
    assert runtime.write_coordinator.stop_requested.is_set()
    assert runtime.write_coordinator.grants == {}


def test_wrong_token_never_dispatches_or_echoes_input(runtime):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(runtime.control.path))
        _send(
            client,
            {
                "token": "synthetic-not-authorized",
                "action": "enable_write",
                "parameters": {"project_ids": [project(runtime)]},
            },
        )
        response = _receive(client)
    assert response == {"ok": False, "error": "CONTROL_NOT_AUTHORIZED"}
    assert runtime.control.is_alive()
    assert not runtime.write_coordinator.grants
    assert enable(runtime)["write_enabled"]


@pytest.mark.parametrize("action", ["discover", "register", "set_enabled"])
def test_registry_change_revokes_before_mutation_even_on_failure(runtime, monkeypatch, action):
    enable(runtime)
    entered = []

    def fail(*args, **kwargs):
        entered.append(True)
        assert runtime.write_coordinator.stop_requested.is_set()
        assert runtime.write_coordinator.grants == {}
        raise SourceError("SYNTHETIC_REGISTRY_FAILURE: unchanged")

    monkeypatch.setattr(runtime.registry, action, fail)
    parameters = (
        {"project_id": project(runtime), "enabled": False} if action == "set_enabled" else {}
    )
    with pytest.raises(SourceError, match="SYNTHETIC_REGISTRY_FAILURE"):
        control_request(runtime.data_dir, action, parameters)
    assert entered == [True]
    assert not runtime.status()["write_enabled"]


def test_disabled_new_child_discovery_revokes_parent_and_invalidates_scope(runtime):
    r = runtime
    pid = project(r)
    source = r.backend.source(pid)
    old = r.backend.read_file(pid, "module.py")["snapshot"]
    enable(r)
    nested = source.root / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    (nested / "module.py").write_text("def independent(): pass\n")
    control_request(r.data_dir, "discover")
    child = project(r, "a/nested")
    assert not r.registry.list_projects(enabled_only=True)["projects"] == []
    assert child not in r.registry.authorized_sources()
    assert not r.status()["write_enabled"]
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        r.backend.read_file(pid, "nested/module.py")
    with pytest.raises(SourceError, match="LIVE_CONTEXT_INVALID"):
        r.backend.resolve_snapshot(pid, old)


def test_real_edit_invalidates_context_index_and_diff_delegates(runtime, monkeypatch):
    r = runtime
    pid = project(r)
    source = r.backend.source(pid)
    old = r.backend.read_file(pid, "module.py")["snapshot"]
    invalidated = []
    actual = r.backend.index_service.invalidate

    def invalidate(project_id):
        invalidated.append(project_id)
        return actual(project_id)

    monkeypatch.setattr(r.backend.index_service, "invalidate", invalidate)
    enable(r)
    task = begin(r, paths=["module.py", "newdir", "newdir/new.py"])
    changed = edit(r, task)
    assert changed["readback_verified"] and "return 2" in source.read("module.py").content
    assert pid in invalidated
    with pytest.raises(ContextError):
        r.backend.contexts.get(pid, source.source_id, old)
    r.create_directory(pid, task, "mkdir_0001", "newdir")
    r.create_file(pid, task, "create_001", "newdir/new.py", "created\n")
    assert r.get_task_diff(pid)["summary"]["files_changed"] == 2
    assert r.finish_task(pid, task, "finish_001")["state"] == "completed"
    assert r.backend.source(project(r, "b")).metrics["body_reads"] == 0


def test_local_rollback_button_uses_coordinator_not_general_edit(runtime, monkeypatch):
    pid = project(runtime)
    calls = []

    def rollback(*args):
        assert threading.current_thread() is runtime.control.thread
        calls.append(args)
        return {"state": "synthetic_delegate_only"}

    monkeypatch.setattr(runtime.write_coordinator, "rollback_write_task", rollback)
    parameters = {"project_id": pid, "task_id": "wt_fake", "request_id": "undo_0001"}
    assert control_request(runtime.data_dir, "rollback_write_task", parameters) == {
        "state": "synthetic_delegate_only"
    }
    assert calls == [(pid, "wt_fake", "undo_0001")]
    for action in ("apply_edit", "create_file", "begin_task"):
        with pytest.raises(SourceError, match="UNKNOWN_CONTROL_ACTION"):
            control_request(runtime.data_dir, action, {})


@pytest.mark.parametrize("kind", ["fd", "stop", "token", "state_identity", "state_mode", "path"])
def test_control_loss_is_sticky_and_fail_closed(runtime, kind):
    r = runtime
    enable(r)
    task = begin(r, paths=["module.py"])
    saved = read_state(r.control.state, "control.json")
    if kind == "fd":
        r.control.socket.close()
    elif kind == "stop":
        r.control.stop.set()
    elif kind == "token":
        write_state(r.control.state, "control.json", {**saved, "token": "synthetic-replaced"})
    elif kind == "state_identity":
        write_state(r.control.state, "control.json", saved)
    elif kind == "state_mode":
        os.chmod(r.data_dir / "control.json", 0o644)
    else:
        r.control.path.rename(r.control.path.with_suffix(".held"))
    assert not r.control.is_alive()
    assert r.control.stop.is_set()
    assert r.write_coordinator.stop_requested.is_set() and r.write_coordinator.grants == {}
    with pytest.raises(WriteError, match="WRITE_DISABLED"):
        edit(r, task)
    assert "return 1" in (r.registry.workspace / "a" / "module.py").read_text()
    if kind == "path":
        r.control.path.with_suffix(".held").rename(r.control.path)
    assert not r.control.is_alive()  # Restoring an endpoint does not revive this connection.
    assert not r.write_coordinator._alive()


def test_socket_path_replacement_is_not_unlinked_by_close(runtime):
    r = runtime
    enable(r)
    r.control.path.rename(r.control.path.with_suffix(".held"))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as foreign:
        foreign.bind(str(r.control.path))
        os.chmod(r.control.path, 0o600)
        assert not r.control.is_alive()
        r.close()
        assert r.control.path.exists()


def test_unexpected_control_thread_exit_proactively_revokes(runtime):
    r = runtime
    enable(r)

    def fail(*args):
        raise RuntimeError("synthetic unexpected failure")

    r.control.handler = fail
    with pytest.raises(SourceError, match="CONTROL_UNAVAILABLE"):
        control_request(r.data_dir, "status")
    wait_until(lambda: r.control.stop.is_set())
    assert r.write_coordinator.stop_requested.is_set() and not r.write_coordinator.grants
    assert not r.control.is_alive()


def test_close_latches_rejection_before_waiting_for_writer_and_closes_resources(runtime):
    r = runtime
    enable(r)
    held, release = threading.Event(), threading.Event()

    def hold_writer():
        with r.write_coordinator.lock:
            held.set()
            assert release.wait(3)

    holder = threading.Thread(target=hold_writer)
    holder.start()
    assert held.wait(2)
    closer = threading.Thread(target=r.close)
    closer.start()
    try:
        assert r.write_coordinator.stop_requested.wait(2)
        assert r.closed and closer.is_alive()
        with pytest.raises(WriteError, match="WRITE_DISABLED"):
            r.write_coordinator._authorized(project(r))
    finally:
        release.set()
        holder.join(3)
        closer.join(3)
    assert not holder.is_alive() and not closer.is_alive()
    assert not r.control.thread.is_alive() and r.watcher.closed.is_set()
    assert r.backend._closed and r.recovery_store.db is None and r.lock_fd is None


def test_close_interrupts_partial_local_socket_receive(runtime):
    r = runtime
    previous = r.control._connection
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(r.control.path))
        client.sendall(b'{"token":')
        wait_until(
            lambda: r.control._connection is not None and r.control._connection is not previous
        )
        r.close()
        assert not r.control.thread.is_alive()
        assert client.recv(1024) == b""


def test_pending_restart_requires_authenticated_local_recover_without_regrant(
    runtime, configured, monkeypatch
):
    r = runtime
    root, state, recovery = configured
    pid = project(r)
    enable(r)
    task = begin(r, paths=["module.py"])
    actual = operations.prepare_file

    def prepare(*args, **kwargs):
        prepared = actual(*args, **kwargs)
        r.write_coordinator.disable()
        return prepared

    monkeypatch.setattr(operations, "prepare_file", prepare)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        edit(r, task)
    assert r.status()["recovery_required"]
    token = r.control.token
    r.close()
    with WorkspaceRuntime(root, state, socket_directory(), recovery_dir=recovery) as fresh:
        assert fresh.control.token != token
        assert fresh.status()["recovery_required"] and not fresh.status()["write_enabled"]
        with pytest.raises(SourceError, match="WRITE_RECOVERY_REQUIRED"):
            fresh.backend.read_file(pid, "module.py")
        # Publication of another authorized project remains available.
        assert "return 1" in fresh.backend.read_file(project(fresh, "b"), "module.py")["content"]
        with pytest.raises(SourceError, match="LOCAL_CONTROL_REQUIRED"):
            fresh.recover_local(pid)
        with pytest.raises(SourceError, match="WRITE_RECOVERY_REQUIRED"):
            enable(fresh, pid)
        recovered = control_request(state, "recover_write", {"project_id": pid})
        assert recovered["outcomes"][0]["state"] == "aborted"
        assert not fresh.status()["recovery_required"] and not fresh.status()["write_enabled"]
        assert "return 1" in fresh.backend.read_file(pid, "module.py")["content"]
        assert enable(fresh, pid)["write_enabled"]
        assert json.loads((state / "control.json").read_text())["token"] == fresh.control.token

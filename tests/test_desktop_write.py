"""Native-local forwarder proof, not ChatGPT web or menu-bar click evidence."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from code_context.client import SyncError
from code_context.desktop import desktop_control
from code_context.source_access import SourceError
from code_context.workspace import WorkspaceRuntime, initialize_workspace


@pytest.fixture
def forwarder(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(
        "code_context.desktop.desktop_binding", lambda *_: SimpleNamespace(data=state)
    )
    request = Mock()
    monkeypatch.setattr("code_context.local_control.control_request", request)
    return tmp_path, state, request


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"project_ids": []},
        {"project_ids": "all"},
        {"project_ids": ["a", "a"]},
        {"project_ids": [None]},
        {"project_ids": ["a"], "enabled": True},
    ],
)
def test_frontend_never_infers_or_expands_write_scope(forwarder, parameters):
    workspace, _, request = forwarder
    with pytest.raises(SyncError):
        desktop_control(workspace, workspace, "enable_write", parameters)
    request.assert_not_called()


@pytest.mark.parametrize(
    "reply",
    [
        {"write_enabled": True, "write_projects": ["b"]},
        {"write_enabled": True, "write_projects": ["a", "a"]},
        {"write_enabled": False, "write_projects": []},
        {},
        None,
    ],
)
def test_uncertain_enable_acknowledgement_revokes_before_error(forwarder, reply):
    workspace, state, request = forwarder
    request.side_effect = [reply, {"write_enabled": False, "write_projects": []}]
    with pytest.raises(SyncError, match="did not confirm"):
        desktop_control(workspace, workspace, "enable_write", {"project_ids": ["a"]})
    assert request.call_args_list[0].args == (state, "enable_write", {"project_ids": ["a"]})
    assert request.call_args_list[1].args == (state, "disable_write", {})


def test_enable_transport_failure_attempts_revoke_and_never_succeeds(forwarder):
    workspace, _, request = forwarder
    request.side_effect = [SourceError("CONTROL_UNAVAILABLE"), SourceError("CONTROL_UNAVAILABLE")]
    with pytest.raises(SourceError, match="CONTROL_UNAVAILABLE"):
        desktop_control(workspace, workspace, "enable_write", {"project_ids": ["a"]})
    assert [call.args[1] for call in request.call_args_list] == ["enable_write", "disable_write"]


def test_disable_requires_false_and_empty_scope_acknowledgement(forwarder):
    workspace, _, request = forwarder
    request.return_value = {"write_enabled": False, "write_projects": ["a"]}
    with pytest.raises(SyncError, match="did not confirm"):
        desktop_control(workspace, workspace, "disable_write", {})
    request.return_value = {"write_enabled": False, "write_projects": []}
    assert desktop_control(workspace, workspace, "disable_write", {}) == request.return_value


def test_actual_local_frontend_grant_read_edit_diff_undo_and_disable(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="dwrite-", dir=Path.cwd() / ".artifacts"))
    root, state = base / "r", base / "s"
    root.mkdir()
    (root / "module.py").write_text("VALUE = 1\n")
    pid = initialize_workspace(root, state, "synthetic")["project_id"]
    monkeypatch.setattr(
        "code_context.desktop.desktop_binding", lambda *_: SimpleNamespace(data=state)
    )
    with WorkspaceRuntime(root, state, base / "sock", recovery_dir=base / "recovery") as runtime:
        assert not runtime.status()["write_enabled"]
        result = desktop_control(base, root, "enable_write", {"project_ids": [pid]})
        assert result["write_enabled"] and result["write_projects"] == [pid]
        task = runtime.begin_task(pid, result["next_task_request_id"], paths=["module.py"])[
            "task_id"
        ]
        runtime.apply_edit(
            pid,
            task,
            "desktop_edit_001",
            "module.py",
            runtime.backend.source(pid).fingerprint("module.py"),
            {"kind": "replace_fragment", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
        )
        assert "VALUE = 2" in runtime.backend.read_file(pid, "module.py")["content"]
        assert runtime.backend.get_recent_diff(pid)["summary"]["files_changed"] == 1
        with pytest.raises(SyncError, match="unsupported desktop action"):
            desktop_control(
                base,
                root,
                "rollback_write_task",
                {"project_id": pid, "task_id": task, "request_id": "desktop_undo_001"},
            )
        assert runtime.backend.get_recent_diff(pid)["summary"]["files_changed"] == 1
        assert (root / "module.py").read_text() == "VALUE = 2\n"
        assert not desktop_control(base, root, "disable_write", {})["write_enabled"]
        assert not (state / "server" / "mirror.sqlite3").exists()

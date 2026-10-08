"""MCP must report the same local grants that authorize native terminal calls.

Fresh fixture workspaces only; native availability is simulated, not certified.
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from code_context.local_control import control_request
from code_context.server import build_mcp
from code_context.source_access import SourceError
from code_context.workspace import WorkspaceRuntime, initialize_workspace


@pytest.fixture
def workspace_status(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="mstatus-", dir=Path.cwd() / ".artifacts"))
    root, state = base / "r", base / "s"
    root.mkdir()
    (root / "README.md").write_text("synthetic workspace\n")
    for name in ("a", "b"):
        child = root / name
        child.mkdir()
        (child / "pyproject.toml").write_text(f'[project]\nname="{name}"\n')
        (child / "module.py").write_text("VALUE = 1\n")
    initialize_workspace(root, state)
    gate = [True]
    monkeypatch.setattr("code_context.workspace.native_gate", lambda: gate[0])
    monkeypatch.setattr("code_context.execution_sandbox.NativeSandbox.available", lambda _: True)
    with WorkspaceRuntime(root, state, base / "ctl", recovery_dir=base / "recovery") as runtime:
        projects = {}
        for row in runtime.registry.list_projects(enabled_only=False)["projects"]:
            if row["relative_root"] in {"a", "b"}:
                projects[row["relative_root"]] = row["project_id"]
                control_request(
                    state, "set_enabled", {"project_id": row["project_id"], "enabled": True}
                )
        yield runtime, projects, gate


def status(mcp):
    result = asyncio.run(mcp.call_tool("connection_status", {}))
    assert not result.is_error
    return result.structured_content


def grant(runtime, project_id):
    control_request(runtime.data_dir, "enable_write", {"project_ids": [project_id]})
    return control_request(
        runtime.data_dir, "enable_execution", {"project_ids": [project_id], "ports": []}
    )


@pytest.mark.parametrize("explicit_provider", [False, True])
def test_mcp_mode_lifecycle_matches_local_control_and_real_authorizer(
    workspace_status, explicit_provider
):
    runtime, projects, _ = workspace_status
    a, b = projects["a"], projects["b"]
    options = {"status_provider": runtime.backend.mcp_status} if explicit_provider else {}
    mcp = build_mcp(runtime.backend, **options)
    initial = status(mcp)
    assert initial["execution_available"] and initial["execution_gate"] == "passed"
    assert not initial["write_enabled"] and not initial["execution_enabled"]
    assert initial["execution_projects"] == []

    control_request(runtime.data_dir, "enable_write", {"project_ids": [a]})
    write = status(mcp)
    assert write["write_enabled"] and not write["execution_enabled"]
    with pytest.raises(ToolError, match="EXECUTION_DISABLED"):
        asyncio.run(
            mcp.call_tool(
                "terminal_start",
                {"project_id": a, "request_id": "closed-probe", "command": ["/bin/echo", "ok"]},
            )
        )

    local = grant(runtime, a)
    development = status(mcp)
    for key in ("execution_available", "execution_enabled", "execution_gate", "execution_limits"):
        assert development[key] == local[key]
    assert development["execution_enabled"] and development["execution_projects"] == [a]
    assert runtime.execution.authorize(a)
    with pytest.raises(SourceError, match="EXECUTION_DISABLED"):
        runtime.execution.authorize(b)
    secret = json.loads((runtime.data_dir / "control.json").read_text())["token"]
    assert secret not in json.dumps(development)
    assert str(runtime.registry.workspace) not in json.dumps(development)
    assert "next_task_request_id" not in development and "local_actions" not in development

    control_request(runtime.data_dir, "disable_execution", {})
    downgraded = status(mcp)
    assert downgraded["write_enabled"] and not downgraded["execution_enabled"]
    assert downgraded["execution_projects"] == []
    control_request(runtime.data_dir, "disable_write", {})
    readonly = status(mcp)
    assert not readonly["write_enabled"] and not readonly["execution_enabled"]
    assert (runtime.registry.workspace / "a/module.py").read_text() == "VALUE = 1\n"

    root, state = runtime.registry.workspace, runtime.data_dir
    runtime.close()
    with WorkspaceRuntime(
        root, state, state.parent / "ctl-restart", recovery_dir=state.parent / "recovery"
    ) as restarted:
        fresh = status(
            build_mcp(
                restarted.backend,
                **({"status_provider": restarted.backend.mcp_status} if explicit_provider else {}),
            )
        )
        assert fresh["execution_available"] and fresh["execution_gate"] == "passed"
        assert not fresh["execution_enabled"] and fresh["execution_projects"] == []
        assert not fresh["write_enabled"] and fresh["write_projects"] == []


@pytest.mark.parametrize("scope", [None, "a", "b"])
def test_execution_status_is_filtered_to_connection_project_scope(workspace_status, scope):
    runtime, projects, _ = workspace_status
    a = projects["a"]
    grant(runtime, a)
    mcp = build_mcp(runtime.backend, project_scope=projects[scope] if scope else None)
    result = status(mcp)
    expected = [] if scope == "b" else [a]
    assert result["execution_projects"] == expected
    assert result["execution_enabled"] is bool(expected)
    assert result["write_enabled"] is bool(expected)


def test_mcp_diagnosis_stays_available_with_closed_gate_and_lost_control(
    workspace_status, monkeypatch
):
    runtime, projects, gate = workspace_status
    mcp = build_mcp(runtime.backend, status_provider=runtime.backend.mcp_status)
    gate[0] = False
    closed = status(mcp)
    assert closed["execution_gate"] == "closed" and not closed["execution_available"]
    assert not closed["execution_enabled"]
    gate[0] = True
    grant(runtime, projects["a"])
    monkeypatch.setattr(runtime.control, "is_alive", lambda: False)
    lost = status(mcp)
    assert lost["server_reachable"]
    assert not lost["execution_available"] and not lost["write_available"]
    assert not lost["execution_enabled"] and not lost["write_enabled"]
    assert lost["execution_projects"] == [] and lost["write_projects"] == []

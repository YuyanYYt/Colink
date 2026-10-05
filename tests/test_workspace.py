import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from code_context.local_control import control_request
from code_context.server import build_mcp
from code_context.source_access import SourceError
from code_context.workspace import WorkspaceRuntime, initialize_workspace


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("workspace\n")
    for name in ("a", "b"):
        project = root / name
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname="example"\n')
        (project / "module.py").write_text("class Example:\n    def run(self):\n        return 1\n")
    return root, tmp_path / "state"


def socket_directory():
    return Path(tempfile.mkdtemp(prefix="wctl-", dir=Path.cwd() / ".artifacts"))


def test_init_discovers_pending_children_and_authorizes_only_selected_root(workspace):
    root, state = workspace
    initialized = initialize_workspace(root, state, "工作区")
    runtime = WorkspaceRuntime(root, state, socket_directory())
    try:
        parent = initialized["project_id"]
        assert runtime.backend.list_projects()["projects"][0]["display_name"] == "工作区"
        with pytest.raises(SourceError, match="PATH_EXCLUDED"):
            runtime.backend.read_file(parent, "a/module.py")
        pending = [p for p in runtime.status()["projects"] if not p["enabled"]]
        assert {p["relative_root"] for p in pending} == {"a", "b"}
        for project in pending:
            with pytest.raises(SourceError, match="PROJECT_NOT_AUTHORIZED"):
                runtime.backend.source(project["project_id"])
    finally:
        runtime.close()


def test_private_control_enables_a_only_and_old_handle_revoked(workspace):
    root, state = workspace
    initialize_workspace(root, state)
    with WorkspaceRuntime(root, state, socket_directory()) as runtime:
        project = next(p for p in runtime.status()["projects"] if p["relative_root"] == "a")
        control_request(
            state, "set_enabled", {"project_id": project["project_id"], "enabled": True}
        )
        backend = runtime.backend
        handle, _ = backend.resolve_snapshot(project["project_id"])
        assert "Example" in backend.read_file(project["project_id"], "module.py", handle)["content"]
        control_request(
            state, "set_enabled", {"project_id": project["project_id"], "enabled": False}
        )
        with pytest.raises(SourceError):
            backend.read_file(project["project_id"], "module.py", handle)
        assert not control_request(state, "status")["write_enabled"]
        with pytest.raises(SourceError, match="WRITE_NOT_IMPLEMENTED"):
            control_request(state, "enable_write", {})


def test_mcp_cannot_grant_permissions_and_nine_structural_tools_available(workspace):
    root, state = workspace
    initial = initialize_workspace(root / "a", state, "Project A")
    runtime = WorkspaceRuntime(root / "a", state, socket_directory())

    async def inspect():
        mcp = build_mcp(runtime.backend, status_provider=runtime.backend.mcp_status)
        tools = await mcp.list_tools()
        assert len(tools) == 15
        assert all(t.annotations.read_only_hint for t in tools)
        names = {t.name for t in tools}
        assert "enable_write" not in names and "set_enabled" not in names
        pid = initial["project_id"]
        overview = await mcp.call_tool("repo_overview", {"project_id": pid})
        handle = overview.structured_content["snapshot"]
        symbols = await mcp.call_tool(
            "symbol_search", {"project_id": pid, "snapshot": handle, "query": "Example"}
        )
        assert not symbols.is_error and symbols.structured_content["symbols"]
        assert symbols.structured_content["display_name"] == "Project A"
        sid = symbols.structured_content["symbols"][0]["symbol_id"]
        arguments = {
            "find_references": {"symbol_id": sid},
            "read_symbol": {"symbol_id": sid},
            "get_call_graph": {"symbol_id": sid},
            "get_class_graph": {"symbol_id": sid},
            "get_file_dependencies": {"path": "module.py"},
            "get_project_architecture": {},
            "get_external_dependencies": {},
            "get_impact_analysis": {"path": "module.py"},
        }
        for name, values in arguments.items():
            result = await mcp.call_tool(name, {"project_id": pid, "snapshot": handle, **values})
            assert not result.is_error, (name, result.content)
            assert not result.structured_content.get("index_not_ready"), name
            assert "revision" not in result.structured_content

    try:
        asyncio.run(inspect())
    finally:
        runtime.close()


def test_duplicate_runtime_rejected_and_restart_closed(workspace):
    root, state = workspace
    initialize_workspace(root, state)
    runtime = WorkspaceRuntime(root, state, socket_directory())
    try:
        with pytest.raises(SourceError, match="WORKSPACE_ALREADY_RUNNING"):
            WorkspaceRuntime(root, state, socket_directory())
    finally:
        runtime.close()
    replacement = WorkspaceRuntime(root, state, socket_directory())
    assert not replacement.status()["write_enabled"]
    replacement.close()


def test_workspace_stdio_routes_saved_sources_and_reports_no_whole_project_limit():
    # A short independent root also keeps native AF_UNIX paths within macOS's
    # byte ceiling. This fixture lives under the authorized workspace only.
    base = Path(tempfile.mkdtemp(prefix="wstdio-", dir=Path.cwd() / ".artifacts"))
    root, state = base / "r", base / "s"
    root.mkdir()
    (root / "models.py").write_text("class Model: pass\n")
    initial = initialize_workspace(root, state, "示例")

    async def inspect():
        import sys

        process = StdioServerParameters(
            command=sys.executable,
            args=[
                "-B",
                "-m",
                "code_context",
                "workspace",
                "--root",
                str(root),
                "--data-dir",
                str(state),
            ],
        )
        async with stdio_client(process) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=10) as session:
                await session.initialize()
                result = await session.call_tool("list_projects", {})
                assert result.structured_content["projects"][0]["display_name"] == "示例"
                status = await session.call_tool("connection_status", {})
                assert status.structured_content["limits"]["max_project_bytes"] is None
                assert not status.structured_content["write_enabled"]
                symbols = await session.call_tool(
                    "symbol_search", {"project_id": initial["project_id"], "query": "Model"}
                )
                assert symbols.structured_content["symbols"]
                saved = await session.call_tool(
                    "read_file", {"project_id": initial["project_id"], "path": "models.py"}
                )
                assert saved.structured_content["content"] == "class Model: pass\n"
                assert json.loads((state / "control.json").read_text())["token"] not in str(saved)

    asyncio.run(inspect())
    assert not (state / "server" / "mirror.sqlite3").exists()

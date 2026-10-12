"""The Live dual entrypoint preserves evidence and removes obsolete registrations."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from code_context.analysis_diff import bound_diff_result
from code_context.cli import parser
from code_context.execution_tools import EXECUTION_TOOL_NAMES
from code_context.live import LiveQueries
from code_context.project_index import ProjectIndexService
from code_context.recovery_store import RecoveryStore
from code_context.server import LIVE_READ_TOOL_NAMES, READ_TOOL_NAMES, build_mcp
from code_context.source_access import SourceAccess
from code_context.storage import MirrorStore
from code_context.workspace import WorkspaceRuntime, initialize_workspace
from code_context.write_coordinator import WriteCoordinator
from code_context.write_tools import WRITE_TOOL_NAMES

REMOVED_LIVE_TOOL_NAMES = frozenset(
    {
        "get_call_graph",
        "get_class_graph",
        "get_file_dependencies",
        "get_diff",
        "search_code",
        "read_file",
        "symbol_search",
        "read_symbol",
    }
)


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from types_only import Options\n"
        "from helpers import leaf\n"
        "class Base: pass\n"
        "class Service(Base):\n    def run(self): return start()\n"
        "def mid(): return leaf()\n"
        "def start():\n    mid(); mid()\n    return loop()\n"
        "def loop(): return start()\n"
        "def unknown(obj): return obj.dynamic()\n"
        + "\n".join(f"def branch_{i}(): return start()" for i in range(12))
        + "\n"
    )
    (root / "helpers.py").write_text("def leaf(): return 1\n")
    (root / "types_only.py").write_text("class Options: pass\n")
    backend = LiveQueries({"sample": SourceAccess(root)})
    pool = ProjectIndexService(tmp_path / "indexes")
    backend.index_service = pool
    recovery = RecoveryStore(tmp_path / "recovery")
    coordinator = WriteCoordinator(
        recovery,
        backend.source,
        control_alive=lambda: True,
        on_change=backend.contexts.invalidate_project,
    )
    backend.write_coordinator = coordinator
    handle = backend.resolve_snapshot("sample")[0]
    assert backend.code_query("sample", handle, "symbol_search", query="start")["total"] == 1
    yield root, backend, pool, coordinator, handle
    coordinator.close()
    backend.close()
    pool.close()
    recovery.close()


def call(mcp, name, **arguments):
    result = asyncio.run(mcp.call_tool(name, {"project_id": "sample", **arguments}))
    assert not result.is_error, result
    return result.structured_content


@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
@pytest.mark.parametrize("depth", [1, 3, 5])
@pytest.mark.parametrize("budget", [(50, 100), (3, 2)])
@pytest.mark.parametrize("kind", ["call", "class", "file"])
def test_analysis_graphs_retain_algorithm_nodes_edges_and_truncation(
    parts, direction, depth, budget, kind
):
    _, backend, _, _, handle = parts
    mcp = build_mcp(backend)
    symbols = backend.code_query(
        "sample",
        handle,
        "symbol_search",
        query="Service" if kind == "class" else "start",
        exact=True,
    )["symbols"]
    selected = next(
        symbol
        for symbol in symbols
        if symbol["qualname"] == ("Service" if kind == "class" else "start")
    )
    options = dict(
        snapshot=handle,
        direction=direction,
        max_nodes=budget[0],
        max_edges=budget[1],
        max_chars=50_000,
    )
    selector = {"path": "main.py"} if kind == "file" else {"symbol_id": selected["symbol_id"]}
    baseline = backend.code_query(
        "sample",
        handle,
        "file_dependencies" if kind == "file" else "symbol_graph",
        **selector,
        **({} if kind == "file" else {"graph": kind}),
        depth=depth,
        **{key: value for key, value in options.items() if key != "snapshot"},
    )
    new = call(
        mcp,
        "analyze_code",
        mode={"call": "call_graph", "class": "class_relations", "file": "file_dependencies"}[kind],
        selector=selector,
        max_depth=depth,
        **options,
    )
    for key in ("nodes", "edges", "unresolved", "truncated", "root", "path"):
        if key in baseline:
            assert new[key] == baseline[key], key


@pytest.mark.parametrize("include_type_only", [True, False])
def test_file_dependency_type_only_option_is_preserved(parts, include_type_only):
    _, backend, _, _, handle = parts
    mcp = build_mcp(backend)
    options = dict(
        snapshot=handle, direction="both", include_type_only=include_type_only, max_chars=50_000
    )
    baseline = backend.code_query(
        "sample",
        handle,
        "file_dependencies",
        path="main.py",
        **{key: value for key, value in options.items() if key != "snapshot"},
    )
    new = call(
        mcp, "analyze_code", mode="file_dependencies", selector={"path": "main.py"}, **options
    )
    assert new["nodes"] == baseline["nodes"] and new["edges"] == baseline["edges"]
    assert ("types_only.py" in {node["path"] for node in new["nodes"]}) == include_type_only


def test_live_discovery_matches_actual_registration_with_nine_read_tools(parts):
    _, backend, _, _, handle = parts
    mcp = build_mcp(backend)
    visible = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert visible == LIVE_READ_TOOL_NAMES | WRITE_TOOL_NAMES
    assert visible == {tool.name for tool in mcp._tool_manager.list_tools()}
    assert len(LIVE_READ_TOOL_NAMES) == 9 and len(visible) == 19
    assert not visible & REMOVED_LIVE_TOOL_NAMES
    assert {"read_code", "analyze_code", "find_references", "get_impact_analysis"} <= visible
    assert call(mcp, "analyze_code", mode="diff", snapshot=handle)["reason"] == "NO_TASK_BASELINE"


@pytest.mark.parametrize("name", sorted(REMOVED_LIVE_TOOL_NAMES))
def test_live_old_tool_names_are_unknown_even_to_cached_clients(parts, name):
    _, backend, _, _, handle = parts
    with pytest.raises(ToolError, match="Unknown tool"):
        call(build_mcp(backend), name, snapshot=handle)


@pytest.mark.parametrize("command", ["live", "workspace"])
def test_cli_no_longer_accepts_legacy_analysis_flag(command, capsys):
    with pytest.raises(SystemExit) as rejected:
        parser().parse_args(
            [command, "--root", "/tmp/test", "--project", "sample", "--legacy-analysis-tools"]
        )
    assert rejected.value.code == 2
    assert "unrecognized arguments: --legacy-analysis-tools" in capsys.readouterr().err


def test_mirror_keeps_all_fifteen_historical_tools_without_live_entrypoints(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    mcp = build_mcp(store)
    visible = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert visible == READ_TOOL_NAMES
    assert visible == {tool.name for tool in mcp._tool_manager.list_tools()}
    assert len(visible) == 15
    assert REMOVED_LIVE_TOOL_NAMES <= visible
    for name in ("read_code", "analyze_code"):
        with pytest.raises(ToolError, match="Unknown tool"):
            call(mcp, name)


def test_complete_workspace_schema_count_and_terminal_permissions_are_preserved(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text("VALUE = 1\n")
    state = tmp_path / "runtime"
    initialize_workspace(root, state)
    controls = Path(tempfile.mkdtemp(prefix="a32c-", dir=Path.cwd() / ".artifacts"))
    with WorkspaceRuntime(root, state, controls, recovery_dir=tmp_path / "recovery") as runtime:
        mcp = build_mcp(runtime.backend)
        tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
        assert set(tools) == LIVE_READ_TOOL_NAMES | WRITE_TOOL_NAMES | EXECUTION_TOOL_NAMES
        assert len(tools) == 40
        assert set(tools) == {tool.name for tool in mcp._tool_manager.list_tools()}
        assert not set(tools) & REMOVED_LIVE_TOOL_NAMES
        assert not runtime.write_coordinator.status()["write_enabled"]
        assert tools["analyze_code"].annotations.read_only_hint
        assert tools["read_code"].annotations.read_only_hint
        assert not tools["terminal_start"].annotations.read_only_hint


@pytest.mark.parametrize("detail", ["summary", "patch"])
@pytest.mark.parametrize("baseline", ["previous", "empty"])
def test_diff_preserves_baseline_patch_and_pagination_without_index_queries(
    parts, monkeypatch, detail, baseline
):
    _, backend, _, coordinator, _ = parts
    coordinator.enable(["sample"])
    task = coordinator.begin_write_task(
        "sample", coordinator.status()["next_task_request_id"], paths=["main.py", "new.py"]
    )["task_id"]
    original = backend.read_file("sample", "main.py")
    coordinator.apply_edit(
        "sample",
        task,
        "edit-diff",
        "main.py",
        original["sha256"],
        {"kind": "replace_fragment", "old_text": "return loop()", "new_text": "return 42"},
    )
    coordinator.create_file("sample", task, "new-diff", "new.py", "RESULT = 2\n")
    coordinator.finish_write_task("sample", task, "finish-diff")

    def fail_index(*args, **kwargs):
        pytest.fail("diff must not load/build the structural index")

    monkeypatch.setattr(backend, "code_query", fail_index)
    mcp = build_mcp(backend)
    for path in (None, "main.py"):
        for offset in (0, 1):
            options = dict(
                baseline=baseline, detail=detail, offset=offset, limit=1, max_chars=12_000
            )
            expected = backend.get_recent_diff("sample", path=path, **options)
            new = call(
                mcp,
                "analyze_code",
                mode="diff",
                selector={"path": path} if path else None,
                snapshot=expected["snapshot"],
                **options,
            )
            assert {key: new[key] for key in expected} == expected


def test_diff_no_baseline_is_unavailable_and_old_live_context_still_fails(parts):
    root, backend, _, _, handle = parts
    mcp = build_mcp(backend, project_scope="sample")
    result = call(mcp, "analyze_code", mode="diff", snapshot=handle)
    assert result["reason"] == "NO_TASK_BASELINE"
    assert result["analysis_status"] == "unavailable" and not result["changes_available"]
    (root / "main.py").write_text("CHANGED = 2\n")
    with pytest.raises(ToolError, match="INVALID|CHANGED"):
        call(mcp, "analyze_code", mode="diff", snapshot=handle)
    with pytest.raises(ToolError, match="outside"):
        asyncio.run(mcp.call_tool("analyze_code", {"project_id": "other", "mode": "diff"}))


@pytest.mark.parametrize(
    "extra",
    [
        {"selector": {"symbol_id": "sym_" + "a" * 32}},
        {"target": {"path": "main.py"}},
        {"include": ["calls"]},
        {"max_chars": "1000"},
        {"selector": {"path": "../outside"}},
    ],
)
def test_diff_rejects_ambiguous_selectors_and_wrong_types(parts, extra):
    _, backend, _, _, _ = parts
    with pytest.raises(ToolError):
        call(build_mcp(backend), "analyze_code", mode="diff", **extra)


def test_diff_response_budget_clips_escaped_patch_text_without_changing_scan_cursor():
    result = {
        "changes_available": True,
        "changes": [{"path": "a.py", "patch": '\\"\n' * 2000, "patch_truncated": False}],
        "offset": 10,
        "has_more": True,
        "next_offset": 14,
        "truncated": False,
    }
    bounded = bound_diff_result(result, 1000)
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 1000
    assert bounded["next_offset"] == 14 and bounded["has_more"]
    assert bounded["changes"][0]["patch_truncated"]
    assert bounded["analysis_status"] == "partial"
    assert result["changes"][0]["patch_truncated"] is False
    with pytest.raises(ValueError, match="metadata"):
        bound_diff_result({**result, "metadata": "x" * 2000}, 1000)

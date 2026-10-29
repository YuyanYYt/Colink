"""Daily selector routing and hidden, strictly validated legacy strategies."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from code_context.analysis_routing import resolve_analysis_mode
from code_context.intelligence_analysis import AnalysisSelector
from code_context.live import LiveQueries
from code_context.live_dataflow import FlowSelector
from code_context.project_index import ProjectIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess


@pytest.fixture
def routing_parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "left.py").write_text(
        "from right import leaf\n"
        "def run(value):\n    return leaf(value)\n"
        "class Box:\n    def echo(self, value): return value\n"
    )
    (root / "right.py").write_text("def leaf(value):\n    return value + 1\n")
    backend = LiveQueries({"sample": SourceAccess(root)})
    indexes = ProjectIndexService(tmp_path / "indexes")
    backend.index_service = indexes
    handle = backend.resolve_snapshot("sample")[0]
    yield backend, build_mcp(backend, project_scope="sample"), handle
    backend.close()
    indexes.close()


def selected(path="left.py", qualname="run", value=None):
    result = {"path": path, "qualname": qualname}
    if value is not None:
        result["value"] = {"kind": value}
        if value == "parameter":
            result["value"]["name"] = "value"
    return result


def call(parts, **arguments):
    _, mcp, handle = parts
    reply = asyncio.run(
        mcp.call_tool(
            "analyze_code",
            {"project_id": "sample", "snapshot": handle, "max_chars": 50_000, **arguments},
        )
    )
    assert not reply.is_error
    return reply.structured_content


def test_daily_discovery_does_not_publish_legacy_mode_choices(routing_parts):
    _, mcp, _ = routing_parts
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "analyze_code")
    assert "mode" not in tool.input_schema["properties"]
    assert "mode" not in tool.input_schema.get("required", [])
    assert "call_graph" not in json.dumps(tool.input_schema)
    assert "data_impact" not in json.dumps(tool.input_schema)
    assert "value" in tool.input_schema["$defs"]["FlowSelector"]["properties"]
    assert tool.annotations.read_only_hint


@pytest.mark.parametrize(
    "mode,selector,target",
    [
        ("file_context", {"path": "left.py"}, None),
        ("symbol_context", selected(), None),
        ("combined", selected(value="parameter"), None),
        ("call_path", selected(), selected("right.py", "leaf")),
        ("data_path", selected(value="parameter"), selected(value="return")),
    ],
)
def test_daily_selector_routes_to_same_evidence_as_explicit_strategy(
    routing_parts, mode, selector, target
):
    options = {"selector": selector, "include": ["source"]}
    if target is not None:
        options["target"] = target
    expected = call(routing_parts, mode=mode, **options)
    actual = call(routing_parts, **options)
    assert actual == expected


@pytest.mark.parametrize("selector", [{"path": "left.py"}, selected()])
def test_context_only_requests_do_not_start_value_flow(routing_parts, selector, monkeypatch):
    import code_context.live_dataflow as flow

    def unexpected(*args, **kwargs):
        pytest.fail("file/symbol-only context must not compute value flow")

    monkeypatch.setattr(flow, "analyze_live_dataflow", unexpected)
    result = call(routing_parts, selector=selector)
    assert result["mode"] in {"file_context", "symbol_context"}


@pytest.mark.parametrize(
    "mode,selector,target",
    [
        ("file_context", {"path": "left.py"}, None),
        ("symbol_context", selected(), None),
        ("callers", selected("right.py", "leaf"), None),
        ("callees", selected(), None),
        ("call_graph", selected(), None),
        ("call_path", selected(), selected("right.py", "leaf")),
        ("class_relations", selected(qualname="Box"), None),
        ("file_dependencies", {"path": "left.py"}, None),
        ("data_origin", selected(value="return"), None),
        ("data_impact", selected(value="parameter"), None),
        ("data_path", selected(value="parameter"), selected(value="return")),
        ("combined", selected(value="parameter"), None),
        ("diff", None, None),
    ],
)
def test_all_explicit_legacy_modes_remain_callable(routing_parts, mode, selector, target):
    options = {"mode": mode}
    if selector is not None:
        options["selector"] = selector
    if target is not None:
        options["target"] = target
    result = call(routing_parts, **options)
    assert result["mode"] == mode


@pytest.mark.parametrize("mode", ["auto", "untrusted-mode", True, 1, [], {}])
def test_hidden_mode_keeps_strict_validation_and_safe_errors(routing_parts, mode):
    with pytest.raises(ToolError, match="INVALID_ANALYSIS_REQUEST") as rejected:
        call(routing_parts, selector=selected(), mode=mode)
    assert "untrusted-mode" not in str(rejected.value)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"selector": selected(), "target": selected(value="return")},
        {"selector": selected(value="parameter"), "target": selected()},
        {"selector": {"path": "left.py"}, "target": {"path": "right.py"}},
        {"selector": {"path": "left.py", "value": {"kind": "parameter", "name": "value"}}},
    ],
)
def test_daily_routing_requires_explicit_compatible_endpoints(routing_parts, arguments):
    with pytest.raises(ToolError):
        call(routing_parts, **arguments)


def test_unknown_fields_are_not_admitted_by_hidden_compatibility(routing_parts):
    with pytest.raises(ToolError, match="INVALID_ANALYSIS_REQUEST"):
        call(routing_parts, selector=selected(), arbitrary=True)
    _, mcp, _ = routing_parts
    with pytest.raises(ToolError, match="INVALID_READ_REQUEST"):
        asyncio.run(mcp.call_tool("repo_overview", {"project_id": "sample", "mode": "diff"}))


def test_explicit_mode_is_not_reinterpreted_by_daily_routing():
    selected_value = FlowSelector.model_validate(selected(value="parameter"))
    assert resolve_analysis_mode(selected_value, mode="data_impact") == "data_impact"
    assert resolve_analysis_mode(AnalysisSelector(path="left.py")) == "file_context"

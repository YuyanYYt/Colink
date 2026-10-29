"""Public selector requests compose evidence without hidden strategy constraints."""

import asyncio
import json

import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError

import code_context.intelligence_analysis as analysis
from code_context.live import LiveQueries
from code_context.project_index import ProjectIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess


@pytest.fixture
def evidence_parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "flow.py").write_text(
        "def leaf(value):\n    return value + 1\n\n"
        "def run(value):\n    return leaf(value)\n\n"
        "class Base: pass\nclass Box(Base): pass\n"
    )
    backend = LiveQueries(
        {"sample": SourceAccess(root)},
        index_factory=lambda: ProjectIndexService(tmp_path / "indexes"),
    )
    yield backend, build_mcp(backend)
    backend.close()


def selected(name="run", *, value=None):
    result = {"path": "flow.py", "qualname": name}
    if value is not None:
        result["value"] = {"kind": value}
        if value == "parameter":
            result["value"]["name"] = "value"
    return result


def call(parts, **options):
    _, mcp = parts
    reply = asyncio.run(mcp.call_tool("analyze_code", {"project_id": "sample", **options}))
    assert not reply.is_error, reply.content
    return reply.structured_content


@pytest.mark.parametrize("value_path", [False, True])
def test_web_path_request_accepts_calls_and_source_on_first_attempt(evidence_parts, value_path):
    selector = selected(value="parameter") if value_path else selected()
    target = selected(value="return") if value_path else selected("leaf")
    options = {
        "selector": selector,
        "target": target,
        "include": ["source", "calls"],
        "direction": "outgoing",
        "max_depth": 5,
        "max_chars": 18000 if value_path else 12000,
    }
    _, mcp = evidence_parts
    tool = mcp._tool_manager.get_tool("analyze_code")
    Draft202012Validator(tool.parameters).validate({"project_id": "sample", **options})
    result = call(evidence_parts, **options)
    assert result["mode"] == ("data_path" if value_path else "call_path")
    assert result["path_found"] is True
    assert result["context"]["mode"] == "symbol_context"
    assert result["context"]["calls"]["edges"]
    assert "def run(value)" in result["source"]["content"]
    assert len(json.dumps(result, ensure_ascii=False)) <= options["max_chars"]


@pytest.mark.parametrize(
    "mode", ["call_graph", "class_relations", "file_dependencies", "data_origin", "data_impact"]
)
def test_legacy_strategy_can_request_context_without_changing_primary_mode(evidence_parts, mode):
    selector = (
        {"path": "flow.py"}
        if mode == "file_dependencies"
        else selected(value="parameter")
        if mode.startswith("data_")
        else selected("Box")
        if mode == "class_relations"
        else selected()
    )
    result = call(
        evidence_parts,
        mode=mode,
        selector=selector,
        include=["ownership", "calls", "dependencies", "source"],
        max_chars=30000,
    )
    assert result["mode"] == mode
    assert {"ownership", "calls", "dependencies"} <= result["context"].keys()
    assert result["source"]["path"] == "flow.py"


def test_public_schema_keeps_auto_entry_and_requires_real_selector(evidence_parts):
    _, mcp = evidence_parts
    schema = mcp._tool_manager.get_tool("analyze_code").parameters
    validator = Draft202012Validator(schema)
    assert "mode" not in schema["properties"]
    assert set(schema["required"]) == {"project_id", "selector"}
    assert validator.is_valid({"project_id": "sample", "selector": selected()})
    assert not validator.is_valid({"project_id": "sample"})
    assert not validator.is_valid({"project_id": "sample", "selector": None})
    assert not validator.is_valid(
        {"project_id": "sample", "selector": selected(), "mode": "call_graph"}
    )


@pytest.mark.parametrize(
    "direction,mode",
    [("incoming", "data_origin"), ("outgoing", "data_impact"), ("both", "combined")],
)
def test_single_value_direction_selects_requested_flow_without_public_mode(
    evidence_parts, direction, mode
):
    result = call(evidence_parts, selector=selected(value="parameter"), direction=direction)
    assert result["mode"] == mode
    assert result["analysis_status"] == "ready"
    assert "structure" in result if direction == "both" else "structure" not in result


@pytest.mark.parametrize(
    "options,explanation",
    [
        ({"max_depth": 6}, "max_depth bounds"),
        ({"mode": "data_path", "target": selected(value="return")}, "explicit value selector"),
    ],
)
def test_request_constraints_have_specific_safe_errors(evidence_parts, options, explanation):
    _, mcp = evidence_parts
    with pytest.raises(ToolError) as failure:
        asyncio.run(
            mcp.call_tool(
                "analyze_code", {"project_id": "sample", "selector": selected(), **options}
            )
        )
    error = str(failure.value)
    assert "INVALID_ANALYSIS_REQUEST:" in error
    assert explanation in error
    assert "INVALID_STRUCTURE_QUERY" not in error


def test_extra_context_obeys_the_same_source_change_guard(evidence_parts, monkeypatch):
    backend, mcp = evidence_parts
    original = analysis.structure_evidence

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (backend.sources["sample"].root / "flow.py").write_text("def run(value): return 0\n")
        return result

    monkeypatch.setattr(analysis, "structure_evidence", changed)
    with pytest.raises(ToolError, match="CHANGED|INVALID"):
        asyncio.run(
            mcp.call_tool(
                "analyze_code",
                {
                    "project_id": "sample",
                    "selector": selected(),
                    "target": selected("leaf"),
                    "include": ["calls", "source"],
                },
            )
        )

"""Recover from valid, unresolved selectors without guessing a different symbol."""

import asyncio
import json

import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError

from code_context.live import LiveQueries
from code_context.project_index import ProjectIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "Example.java").write_text(
        "package demo;\nclass Example {\n"
        "  String unique(String value) { return value; }\n"
        "  String overloaded(String value) { return value; }\n"
        "  int overloaded(int value) { return value; }\n}\n"
    )
    (root / "Other.java").write_text(
        "class Other { String unique(String value) { return value; } }\n"
    )
    (root / "flow.py").write_text("def run(value):\n    return value\n")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "Example.java").write_text(
        "class Example { int foreignOnly(int value) { return value; } }\n"
    )
    backend = LiveQueries(
        {"sample": SourceAccess(root), "foreign": SourceAccess(foreign)},
        index_factory=lambda: ProjectIndexService(tmp_path / "indexes"),
    )
    yield backend, build_mcp(backend, project_scope="sample")
    backend.close()


def call(parts, tool="analyze_code", **options):
    _, mcp = parts
    arguments = {"project_id": "sample", **options}
    Draft202012Validator(mcp._tool_manager.get_tool(tool).parameters).validate(arguments)
    reply = asyncio.run(mcp.call_tool(tool, arguments))
    assert not reply.is_error, reply.content
    result = reply.structured_content
    assert len(json.dumps(result, ensure_ascii=False)) <= options.get("max_chars", 12000)
    return result


def test_unique_java_stem_uses_same_fact_and_saved_source_as_exact_signature(parts):
    stem = {"path": "Example.java", "qualname": "Example.unique"}
    result = call(parts, selector=stem, include=["source", "calls"])
    symbol = result["symbol"]
    assert symbol["qualname"] == "Example.unique(String)"
    assert "String unique(String value)" in result["source"]["content"]
    exact = call(parts, selector={**stem, "qualname": symbol["qualname"]}, include=[])
    assert exact["symbol"]["symbol_id"] == symbol["symbol_id"]
    read = call(parts, "read_code", request={"mode": "symbol", **stem})
    assert read["symbol"]["symbol_id"] == symbol["symbol_id"]
    assert read["content"] == result["source"]["content"]


def test_java_overloads_are_paginated_and_never_read_before_selection(parts):
    selector = {"path": "Example.java", "qualname": "Example.overloaded"}
    first = call(parts, selector=selector, include=["source"], limit=1)
    assert first["analysis_status"] == "ambiguous"
    assert "content" not in first["source"]
    assert first["total"] == 2 and first["next_offset"] == 1
    second = call(parts, selector=selector, include=["source"], limit=1, offset=1)
    candidates = first["candidates"] + second["candidates"]
    assert {c["qualname"] for c in candidates} == {
        "Example.overloaded(String)",
        "Example.overloaded(int)",
    }
    assert not second["has_more"]
    chosen = call(parts, selector={"symbol_id": candidates[1]["symbol_id"]}, include=["source"])
    assert chosen["symbol"]["symbol_id"] == candidates[1]["symbol_id"]


@pytest.mark.parametrize(
    "selector",
    [
        {"path": "Example.java", "qualname": "Example.noSuchMethod"},
        {"path": "Example.java", "qualname": "Other.unique"},
        {"path": "Other.java", "qualname": "Example.unique"},
        {"path": "Example.java", "qualname": "unique"},
        {"path": "Example.java", "qualname": "Example.Unique"},
        {"path": "Example.java", "qualname": "Example.foreignOnly"},
        {"symbol_id": "sym_" + "0" * 32},
    ],
)
def test_missing_selector_returns_guidance_without_fuzzy_or_cross_scope_rebinding(parts, selector):
    result = call(parts, selector=selector, include=["source"], max_chars=6000)
    assert result["analysis_status"] == "selection_not_found"
    assert result["selection_status"] == "symbol_not_found"
    assert result["total"] == 0 and result["candidates"] == []
    assert not result["truncated"]
    assert "symbol" not in result and "nodes" not in result
    assert result["source"] == {"read_status": "selection_not_found"}
    continuation = result["continuation"]
    assert continuation["tool"] == "read_code"
    if "next_request" in continuation:
        found = call(
            parts, "read_code", snapshot=result["snapshot"], **continuation["next_request"]
        )
        assert "symbols" in found
    else:
        assert "symbol_id" in selector


@pytest.mark.parametrize("max_chars", [1000, 2000, 6000])
def test_read_symbol_reports_missing_selection_and_fits_the_budget(parts, max_chars):
    result = call(
        parts,
        "read_code",
        request={"mode": "symbol", "path": "Example.java", "qualname": "Example.missing"},
        max_chars=max_chars,
    )
    assert result["read_status"] == "selection_not_found"
    assert not result.get("index_not_ready")
    assert result["continuation"]["tool"] == "read_code"


@pytest.mark.parametrize("max_chars", [1000, 2000])
def test_missing_analysis_selection_fits_small_budgets(parts, max_chars):
    result = call(
        parts,
        selector={"path": "flow.py", "qualname": "missing"},
        include=[],
        max_chars=max_chars,
    )
    assert result["analysis_status"] == "selection_not_found"
    assert result["continuation"]["tool"] == "read_code"


@pytest.mark.parametrize("value_flow", [False, True])
def test_missing_target_is_distinct_from_a_missing_root_or_no_path(parts, value_flow):
    root = {"path": "flow.py", "qualname": "run"}
    target = {"path": "flow.py", "qualname": "missing"}
    if value_flow:
        root["value"] = {"kind": "parameter", "name": "value"}
        target["value"] = {"kind": "return"}
    result = call(parts, selector=root, target=target, include=["source", "calls"])
    assert result["analysis_status"] == "target_not_found"
    assert result["selection_scope"] == "target"
    assert "ambiguous_selector" not in result and "path_found" not in result
    assert "def run(value)" in result["source"]["content"]
    if value_flow:
        assert result["conclusion"]["status"] == "selection_not_resolved"
        assert result["conclusion"]["absence_proven"] is False


def test_missing_value_root_does_not_fabricate_a_dataflow_slice(parts):
    result = call(
        parts,
        selector={
            "path": "flow.py",
            "qualname": "missing",
            "value": {"kind": "parameter", "name": "value"},
        },
    )
    assert result["analysis_status"] == "selection_not_found"
    assert result["conclusion"]["status"] == "selection_not_resolved"
    assert result["conclusion"]["absence_proven"] is False
    assert not result.get("nodes") and not result.get("edges")


def test_malformed_or_unauthorized_selectors_still_raise_safe_errors(parts):
    _, mcp = parts
    for arguments, message in [
        ({"project_id": "sample", "selector": {"symbol_id": "malformed"}}, "INVALID"),
        (
            {"project_id": "foreign", "selector": {"path": "Example.java"}},
            "outside this connection",
        ),
    ]:
        with pytest.raises(ToolError, match=message):
            asyncio.run(mcp.call_tool("analyze_code", arguments))

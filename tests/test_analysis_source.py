"""Shared-context structural/source evidence and fail-closed composition."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from code_context.live import LiveQueries
from code_context.project_index import ProjectIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess


@pytest.fixture
def source_parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "service.py").write_text(
        "from helpers import helper\n"
        "class Service:\n"
        "    def run(self, x):\n"
        "        return helper(x)\n"
        "UNRELATED_MARKER = True\n"
    )
    (root / "helpers.py").write_text("def helper(x):\n    return x + 1\n")
    backend = LiveQueries({"sample": SourceAccess(root)})
    indexes = ProjectIndexService(tmp_path / "indexes")
    backend.index_service = indexes
    yield root, backend, build_mcp(backend, project_scope="sample")
    backend.close()
    indexes.close()


def analyze(mcp, **arguments):
    result = asyncio.run(mcp.call_tool("analyze_code", {"project_id": "sample", **arguments}))
    assert not result.is_error
    return result.structured_content


@pytest.mark.parametrize(
    "mode,selector,extra",
    [
        ("file_context", {"path": "service.py"}, {}),
        ("file_dependencies", {"path": "service.py"}, {}),
        ("symbol_context", {"path": "service.py", "qualname": "Service.run"}, {}),
        ("callers", {"path": "service.py", "qualname": "Service.run"}, {}),
        ("callees", {"path": "service.py", "qualname": "Service.run"}, {}),
        ("call_graph", {"path": "service.py", "qualname": "Service.run"}, {}),
        ("class_relations", {"path": "service.py", "qualname": "Service"}, {}),
        (
            "call_path",
            {"path": "service.py", "qualname": "Service.run"},
            {"target": {"path": "helpers.py", "qualname": "helper"}},
        ),
        (
            "combined",
            {
                "path": "service.py",
                "qualname": "Service.run",
                "value": {"kind": "parameter", "name": "x"},
            },
            {},
        ),
        (
            "data_impact",
            {
                "path": "service.py",
                "qualname": "Service.run",
                "value": {"kind": "parameter", "name": "x"},
            },
            {},
        ),
    ],
)
def test_analysis_attaches_exact_source_in_one_context(source_parts, mode, selector, extra):
    _, backend, mcp = source_parts
    result = analyze(mcp, mode=mode, selector=selector, include=["source"], **extra)
    assert len(json.dumps(result, ensure_ascii=False)) <= 12_000
    source = result["source"]
    assert source["snapshot"] == result["snapshot"]
    assert source["path"] == "service.py" and source["source_is_untrusted"]
    assert "return helper(x)" in source["content"]
    if selector.get("qualname") == "Service.run":
        assert "UNRELATED_MARKER" not in source["content"]
    assert len(backend.contexts._contexts) == 1


def test_ambiguous_definition_does_not_pick_source(source_parts):
    root, _, mcp = source_parts
    (root / "duplicate.py").write_text("def same(): return 1\ndef same(): return 2\n")
    result = analyze(
        mcp,
        mode="symbol_context",
        selector={"path": "duplicate.py", "qualname": "same"},
        include=["source"],
    )
    assert result["analysis_status"] == "ambiguous" and len(result["candidates"]) == 2
    assert result["source"]["analysis_status"] == "ambiguous"
    assert "content" not in result["source"]


def test_changed_source_between_analysis_and_body_is_rejected(source_parts, monkeypatch):
    root, backend, mcp = source_parts
    original = backend.code_query

    def change_after_query(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / "service.py").write_text("AFTER_ANALYSIS = True\n")
        return result

    monkeypatch.setattr(backend, "code_query", change_after_query)
    with pytest.raises(ToolError, match="LIVE_CONTEXT|CHANGED"):
        analyze(mcp, mode="file_context", selector={"path": "service.py"}, include=["source"])
    assert len(backend.contexts._contexts) == 0


def test_source_and_relations_share_response_budget(source_parts):
    root, _, mcp = source_parts
    (root / "service.py").write_text("ESCAPED = " + repr('\\"' * 2000) + "\n")
    result = analyze(
        mcp,
        mode="file_context",
        selector={"path": "service.py"},
        include=["source"],
        max_chars=3000,
    )
    assert len(json.dumps(result, ensure_ascii=False)) <= 3000
    assert result["source"]["content_truncated"]
    assert result["source"]["next_char_offset"] > 0
    assert result["analysis_status"] == "partial"


@pytest.mark.parametrize("include,max_chars", [(["source"], 1000), (["source", "source"], 5000)])
def test_invalid_source_composition_does_not_allocate_context(source_parts, include, max_chars):
    _, backend, mcp = source_parts
    with pytest.raises(ToolError):
        analyze(
            mcp,
            mode="file_context",
            selector={"path": "service.py"},
            include=include,
            max_chars=max_chars,
        )
    assert len(backend.contexts._contexts) == 0

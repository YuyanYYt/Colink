"""Output limits preserve facts and describe the specific evidence that was clipped."""

import asyncio
import copy
import json

import pytest

from code_context.analysis_response import AnalysisResponseBudgetExceeded
from code_context.intelligence_analysis import apply_analysis_bounds, bound_analysis_result
from code_context.live import LiveQueries
from code_context.project_index import ProjectIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess, SourceError


def encoded(value):
    return len(json.dumps(value, ensure_ascii=False))


def clipped_field(section, field):
    return next(entry for entry in section["clipped_fields"] if entry["field"] == field)


def symbol(index, *, padding=0):
    return {
        "symbol_id": f"sym_{index:032x}",
        "path": "probe.py",
        "qualname": f"function_{index}",
        "signature": f"def function_{index}(value)",
        "parameters": ["value"],
        "documentation": "x" * padding,
    }


def graph(count=10, *, kind="calls", padding=80):
    nodes = [symbol(index, padding=padding) for index in range(count + 1)]
    if kind == "dependencies":
        nodes = [{"path": f"module_{index}.py"} for index in range(count + 1)]

    def identity(node):
        return node.get("symbol_id", node.get("path"))

    return {
        "nodes": nodes,
        "edges": [
            {
                "source": identity(nodes[0]),
                "target": identity(node),
                "kind": "CALL" if kind == "calls" else "IMPORT",
                "evidence": "e" * padding,
            }
            for node in nodes[1:]
        ],
        "truncated": False,
        "truncated_reason": [],
    }


def assert_graph_consistent(value):
    ids = {node.get("id", node.get("symbol_id", node.get("path"))) for node in value["nodes"]}
    pairs = {
        (
            edge.get("source", edge.get("source_symbol_id")),
            edge.get("target", edge.get("target_symbol_id")),
        )
        for edge in value["edges"]
    }
    assert all(source in ids and (target is None or target in ids) for source, target in pairs)
    for path in value.get("paths", []):
        assert set(path) <= ids
        assert all(pair in pairs for pair in zip(path, path[1:], strict=False))


def test_only_the_clipped_partition_reports_max_chars():
    empty = {"nodes": [], "edges": [], "truncated": False, "truncated_reason": []}
    result = {
        "mode": "file_context",
        "path": "probe.py",
        "symbols": [symbol(0, padding=650), symbol(1, padding=650)],
        "offset": 0,
        "total": 2,
        "has_more": False,
        "next_offset": None,
        "calls": copy.deepcopy(empty),
    }
    result = bound_analysis_result(result, 1800)
    assert result["calls"] == empty
    assert result["truncated_reason"] == ["max_chars"]
    assert result["symbols"] == [symbol(0, padding=650)]
    assert result["next_offset"] == 1
    assert result["next_offset_scope"] == "symbols"
    assert result["next_request_reuses"] == ["project_id", "snapshot"]
    assert result["continuation"]["symbols"]["scope"] == "symbols"
    assert result["continuation"]["symbols"]["next_request"]["offset"] == 1
    assert "next_request" not in result["continuation"]
    assert encoded(result) <= 1800


def test_shared_graph_limits_mark_the_affected_context_ancestors():
    untouched = {"nodes": [], "edges": [], "truncated": False, "truncated_reason": []}
    result = {
        "analysis_status": "ready",
        "context": {"analysis_status": "ready", "calls": graph(3)},
        "dependencies": copy.deepcopy(untouched),
    }
    limited = apply_analysis_bounds(result, 3, 1)
    assert limited["context"]["analysis_status"] == "partial"
    assert limited["context"]["truncated_reason"] == ["graph_budget"]
    assert limited["context"]["calls"]["truncated_reason"] == ["graph_budget"]
    assert limited["dependencies"] == untouched
    assert_graph_consistent(limited["context"]["calls"])


def test_requested_graphs_keep_evidence_and_their_own_public_next_request():
    result = {
        "mode": "file_context",
        "path": "probe.py",
        "symbols": [symbol(index) for index in range(10)],
        "offset": 0,
        "total": 10,
        "has_more": False,
        "next_offset": None,
        "calls": graph(14),
        "dependencies": graph(14, kind="dependencies"),
    }
    result = bound_analysis_result(result, 6000)
    assert encoded(result) <= 6000
    for name in ("calls", "dependencies"):
        section = result[name]
        assert section["edges"]
        if len(section["edges"]) < 14:
            assert section["truncated"] and "max_chars" in section["truncated_reason"]
            assert clipped_field(section, "edges")["before"] == 14
            assert clipped_field(section, "edges")["returned"] == len(section["edges"])
            request = section["continuation"]["next_request"]
            assert request["selector"] == {"path": "probe.py"}
            assert request["include"] == [name]
            assert "mode" not in request and "offset" not in request
        else:
            assert section == graph(14, kind=name)
        assert_graph_consistent(section)


def test_nested_context_clipping_leaves_untouched_path_graph_and_facts_intact():
    left, right = symbol(0), symbol(1)
    pair = [left["symbol_id"], right["symbol_id"]]
    result = {
        "mode": "call_path",
        "root": pair[0],
        "target": pair[1],
        "nodes": [left, right],
        "edges": [{"source": pair[0], "target": pair[1], "kind": "CALL"}],
        "paths": [pair],
        "path_found": True,
        "context": {"mode": "symbol_context", "symbol": copy.deepcopy(left), "calls": graph(20)},
    }
    result = bound_analysis_result(result, 4000)
    assert encoded(result) <= 4000
    assert result["paths"] == [pair]
    assert result["edges"] == [{"source": pair[0], "target": pair[1], "kind": "CALL"}]
    assert result["context"]["symbol"] == left
    assert "max_chars" in result["context"]["truncated_reason"]
    calls = result["context"]["calls"]
    assert calls["edges"] and "max_chars" in calls["truncated_reason"]
    assert calls["continuation"]["next_request"]["selector"] == {"symbol_id": left["symbol_id"]}
    assert_graph_consistent(result)
    assert_graph_consistent(calls)


def test_graph_budget_is_recorded_only_on_changed_graph():
    empty = {"nodes": [], "edges": [], "truncated": False, "truncated_reason": []}
    result = {"calls": graph(5), "dependencies": copy.deepcopy(empty)}
    result = apply_analysis_bounds(result, 2, 1)
    assert "graph_budget" in result["calls"]["truncated_reason"]
    assert result["dependencies"] == empty
    assert_graph_consistent(result["calls"])


def test_dropped_path_witnesses_are_explicit_and_never_reference_removed_edges():
    nodes = [symbol(index, padding=300) for index in range(3)]
    identities = [node["symbol_id"] for node in nodes]
    result = {
        "mode": "call_path",
        "root": identities[0],
        "target": identities[-1],
        "nodes": nodes,
        "edges": [
            {"source": left, "target": right, "evidence": "x" * 500}
            for left, right in zip(identities, identities[1:], strict=False)
        ],
        "paths": [identities],
        "path_found": True,
    }
    result = bound_analysis_result(result, 2300)
    assert encoded(result) <= 2300
    assert result["paths_truncated"]
    assert result.get("path_evidence_omitted") is bool(not result["paths"])
    assert_graph_consistent(result)
    request = result["continuation"]["next_request"]
    assert request["selector"] == {"symbol_id": identities[0]}
    assert request["target"] == {"symbol_id": identities[-1]}
    assert request["max_depth"] == 5
    assert "mode" not in request


def test_large_selected_symbol_cannot_be_silently_rewritten():
    fact = symbol(0)
    fact["parameters"] = [f"parameter_{index:03}" for index in range(100)]
    fact["signature"] = "def many(" + ",".join(fact["parameters"]) + ")"
    original = copy.deepcopy(fact)
    with pytest.raises(AnalysisResponseBudgetExceeded):
        bound_analysis_result({"symbol": fact}, 2000)
    assert fact == original


def test_not_traversed_relations_are_clipped_as_whole_facts_with_counts():
    result = graph(2)
    result["path"] = "probe.py"
    deferred = [
        {
            "source_symbol_id": result["nodes"][0]["symbol_id"],
            "target_symbol_id": result["nodes"][1]["symbol_id"],
            "line": index + 1,
            "type_only": True,
            "execution_context": "deferred",
            "evidence": "x" * 250,
        }
        for index in range(20)
    ]
    result["not_traversed"] = copy.deepcopy(deferred)
    result = bound_analysis_result(result, 3000)
    assert encoded(result) <= 3000
    assert result["edges"]
    assert len(result["not_traversed"]) < len(deferred)
    assert result["not_traversed"] == deferred[: len(result["not_traversed"])]
    assert clipped_field(result, "not_traversed")["before"] == 20
    assert_graph_consistent(result)


@pytest.fixture
def live_parts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    parameters = [f"p{index:03}_long_parameter" for index in range(100)]
    (source / "many.py").write_text(
        "def many(" + ", ".join(parameters) + "):\n    return " + parameters[0] + "\n"
    )
    (source / "probe.py").write_text(
        "def leaf(value): return value\n"
        + "".join(f"def caller_{index}(value): return leaf(value)\n" for index in range(18))
    )
    backend = LiveQueries({"sample": SourceAccess(source)})
    backend.index_service = ProjectIndexService(tmp_path / "index")
    yield backend
    backend.close()


@pytest.mark.parametrize("budget", [1000, 3000, 4000, 6000])
def test_real_parsed_parameters_are_complete_or_explicitly_over_budget(live_parts, budget):
    try:
        result = live_parts.code_query(
            "sample",
            None,
            "analyze_code",
            mode="symbol_context",
            selector={"path": "many.py", "qualname": "many"},
            include=[],
            max_chars=budget,
        )
    except SourceError as exc:
        assert str(exc).startswith("ANALYSIS_RESPONSE_BUDGET_EXCEEDED:")
    else:
        assert len(result["symbol"]["parameters"]) == 100
        assert encoded(result) <= budget


@pytest.mark.parametrize("budget", [6000, 12000])
def test_real_file_graph_retains_calls_and_does_not_page_them_with_symbols(live_parts, budget):
    result = live_parts.code_query(
        "sample",
        None,
        "analyze_code",
        mode="file_context",
        selector={"path": "probe.py"},
        include=["ownership", "calls", "dependencies"],
        max_chars=budget,
    )
    assert encoded(result) <= budget
    assert result["calls"]["edges"]
    assert_graph_consistent(result["calls"])
    if result["calls"]["truncated"]:
        assert result["calls"]["truncated_reason"]
        assert "offset" not in result["calls"]["continuation"]["next_request"]
    if result["has_more"]:
        assert result["next_offset_scope"] == "symbols"


def test_real_clipped_calls_next_request_uses_public_fields_and_recovers_evidence(live_parts):
    result = live_parts.code_query(
        "sample",
        None,
        "analyze_code",
        mode="file_context",
        selector={"path": "probe.py"},
        include=["ownership", "calls", "dependencies"],
        max_chars=6000,
    )
    request = result["calls"]["continuation"]["next_request"]
    assert "mode" not in request and "offset" not in request
    mcp = build_mcp(live_parts, project_scope="sample")
    followup = asyncio.run(
        mcp.call_tool(
            "analyze_code",
            {"project_id": "sample", "snapshot": result["snapshot"], **request},
        )
    )
    assert not followup.is_error
    payload = followup.structured_content
    assert payload["mode"] == "file_context"
    assert len(payload["calls"]["edges"]) >= len(result["calls"]["edges"])
    assert_graph_consistent(payload["calls"])

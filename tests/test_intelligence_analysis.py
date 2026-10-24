"""Unified views remain exact, bounded and read-only without another graph store."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

import code_context.intelligence_analysis as analysis_module
from code_context.intelligence_analysis import (
    AnalysisBudgetExceeded,
    AnalysisSelector,
    analyze_structure,
    apply_analysis_bounds,
    bound_analysis_result,
)
from code_context.models import FileChange, SyncBatch, content_hash
from code_context.server import LIVE_READ_TOOL_NAMES, READ_TOOL_NAMES, build_mcp
from code_context.storage import MirrorStore


@pytest.fixture
def facts(tmp_path):
    store = MirrorStore(tmp_path / "facts.sqlite3")
    files = {
        "hierarchy.py": "class Base: pass\n\nclass Outer(Base):\n"
        "    class Inner:\n        def method(self):\n            return 1\n"
        "    def outer(self):\n        return 2\n\ndef top():\n    return 3\n",
        "calls.py": "def end():\n    return 1\n\ndef mid():\n    return end()\n\n"
        "def start():\n    mid()\n    end()\n    return end()\n\n"
        "def unknown(obj):\n    return obj.dynamic()\n\n"
        "def cycle_a():\n    return cycle_b()\n\n"
        "def cycle_b():\n    cycle_a()\n    return end()\n\n"
        + "\n".join(f"def branch_{n}():\n    return end()\n" for n in range(12))
        + "\ndef fan():\n"
        + "\n".join(f"    branch_{n}()" for n in range(12))
        + "\n",
        "uses.py": "from calls import start\n\ndef invoke():\n    return start()\n",
        "repeat.py": "def repeated():\n    return 1\ndef repeated():\n    return 2\n",
        "large.py": "\n".join(f"def func_{n}():\n    return {n}\n" for n in range(40)),
    }
    store.apply(
        "sample",
        SyncBatch(
            request_id="fixture",
            base_revision=0,
            mode="full",
            changes=[
                FileChange(op="upsert", path=path, content=text, sha256=content_hash(text))
                for path, text in files.items()
            ],
        ),
    )
    return store


def analyze(store, mode, selector, **parameters):
    with store.read_connection() as db:
        return analyze_structure(db, "sample", 1, mode=mode, selector=selector, **parameters)


def exact(path, qualname):
    return {"path": path, "qualname": qualname}


def test_file_context_hierarchy_includes_top_level_and_nested_ownership(facts):
    result = analyze(facts, "file_context", {"path": "hierarchy.py"})
    definitions = {s["qualname"]: s for s in result["symbols"]}
    assert {"", "Base", "Outer", "Outer.Inner", "Outer.Inner.method", "top"} <= definitions.keys()
    edges = {(e["source_qualname"], e["target_qualname"]) for e in result["ownership"]}
    assert ("", "top") in edges
    assert ("Outer", "Outer.Inner") in edges
    assert ("Outer.Inner", "Outer.Inner.method") in edges
    subtree = analyze(facts, "symbol_context", exact("hierarchy.py", "Outer"), max_depth=2)
    assert {s["qualname"] for s in subtree["ownership"]["nodes"]} == {
        "",
        "Outer",
        "Outer.Inner",
        "Outer.Inner.method",
        "Outer.outer",
    }
    assert subtree["analysis_status"] == "ready"
    method = analyze(
        facts, "symbol_context", exact("hierarchy.py", "Outer.Inner.method"), max_depth=2
    )
    assert {s["qualname"] for s in method["ownership"]["nodes"]} == {
        "Outer.Inner.method",
        "Outer.Inner",
        "Outer",
    }


def test_file_symbols_paginate_without_skipping_after_output_clipping(facts):
    offset, ids = 0, []
    while True:
        page = analyze(
            facts, "file_context", {"path": "large.py"}, limit=20, offset=offset, max_chars=2300
        )
        assert len(json.dumps(page, ensure_ascii=False)) <= 2300
        ids.extend(s["symbol_id"] for s in page["symbols"])
        if not page["has_more"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(ids) == len(set(ids)) == 41


def test_repeated_qualified_definition_is_ambiguous_not_guessed(facts):
    result = analyze(facts, "callees", exact("repeat.py", "repeated"))
    assert result["analysis_status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    chosen = analyze(facts, "callees", {"symbol_id": result["candidates"][1]["symbol_id"]})
    assert chosen["root"] == result["candidates"][1]["symbol_id"]
    with pytest.raises(ValueError, match=r"path \+ qualname"):
        analyze(facts, "callees", {"path": "calls.py"})
    with pytest.raises(ValidationError):
        AnalysisSelector.model_validate({"path": "calls.py", "unknown": True})
    with pytest.raises(ValidationError):
        AnalysisSelector(path="calls.py", symbol_id=chosen["root"])


def test_context_combines_calls_and_dependencies_using_same_fact_state(facts):
    result = analyze(
        facts, "file_context", {"path": "uses.py"}, include=["ownership", "calls", "dependencies"]
    )
    assert any(e["target_qualname"] == "start" for e in result["calls"]["edges"])
    assert any(e["target"] == "calls.py" for e in result["dependencies"]["edges"])
    root = analyze(
        facts, "symbol_context", exact("uses.py", "invoke"), include=["calls", "dependencies"]
    )
    assert root["symbol"]["qualname"] == "invoke"
    assert len(root["calls"]["edges"]) == 1


def test_call_path_shortest_directed_and_preserves_distinct_callsites(facts):
    result = analyze(
        facts, "call_path", exact("calls.py", "start"), target=exact("calls.py", "end"), max_depth=3
    )
    assert result["path_found"] and result["shortest_depth"] == 1
    assert all(len(path) == 2 for path in result["paths"])
    assert len(result["edges"]) == 2
    assert len({(e["line"], e["column"]) for e in result["edges"]}) == 2
    reverse = analyze(
        facts, "call_path", exact("calls.py", "end"), target=exact("calls.py", "start"), max_depth=3
    )
    assert not reverse["path_found"] and reverse["no_path_is_not_runtime_proof"]
    unknown = analyze(
        facts, "call_path", exact("calls.py", "unknown"), target=exact("calls.py", "end")
    )
    assert not unknown["path_found"] and unknown["unresolved"]


def test_callers_callees_class_edges_and_graph_bounds(facts):
    callers = analyze(facts, "callers", exact("calls.py", "end"), max_depth=2)
    assert callers["direction"] == "incoming"
    assert {s["qualname"] for s in callers["nodes"]} >= {"end", "mid", "start"}
    classes = analyze(facts, "class_relations", exact("hierarchy.py", "Outer"))
    assert {e["kind"] for e in classes["edges"]} >= {"INHERITANCE", "CONTAINS"}
    bounded = analyze(facts, "callees", exact("calls.py", "start"), max_nodes=1, max_edges=1)
    assert bounded["truncated"] and bounded["analysis_status"] == "partial"
    assert len(bounded["nodes"]) == 1
    assert "graph_budget" in bounded["truncated_reason"]


def test_call_path_cycles_and_equal_length_paths_are_bounded(facts):
    cyclic = analyze(
        facts,
        "call_path",
        exact("calls.py", "cycle_a"),
        target=exact("calls.py", "end"),
        max_depth=5,
    )
    assert cyclic["path_found"] and cyclic["shortest_depth"] == 2
    assert len(cyclic["paths"]) == 1
    fan = analyze(
        facts,
        "call_path",
        exact("calls.py", "fan"),
        target=exact("calls.py", "end"),
        max_depth=3,
        max_chars=50_000,
    )
    assert len(fan["paths"]) == 10
    assert "max_paths" in fan["truncated_reason"] and fan["analysis_status"] == "partial"


def test_composite_output_keeps_graph_endpoints_and_explicit_clipping(facts):
    result = analyze(
        facts,
        "symbol_context",
        exact("calls.py", "start"),
        include=["calls", "dependencies"],
        max_depth=3,
        max_chars=2000,
    )
    assert len(json.dumps(result, ensure_ascii=False)) <= 2000
    assert result["truncated"] and "max_chars" in result["truncated_reason"]
    for key in ("calls", "dependencies"):
        graph = result[key]
        ids = {n.get("symbol_id", n.get("path")) for n in graph["nodes"]}
        assert all(
            e.get("source_symbol_id", e.get("source")) in ids
            and (
                e.get("target_symbol_id", e.get("target")) is None
                or e.get("target_symbol_id", e.get("target")) in ids
            )
            for e in graph["edges"]
        )


def test_sql_budget_exhaustion_has_safe_partial_result():
    class Exhausted:
        def execute(self, *args):
            raise AnalysisBudgetExceeded

    result = analyze_structure(
        Exhausted(), "sample", 1, mode="file_context", selector={"path": "calls.py"}
    )
    assert result["analysis_status"] == "partial" and result["nodes"] == result["edges"] == []
    assert result["truncated_reason"] == ["time_budget"]
    assert result["continuation"]["selector"] == {"path": "calls.py"}


def test_composite_request_shares_node_and_edge_budgets(facts):
    result = analyze(
        facts,
        "file_context",
        {"path": "uses.py"},
        include=["ownership", "calls", "dependencies"],
        max_nodes=3,
        max_edges=1,
    )
    nodes = {("symbol", node["symbol_id"]) for node in result["symbols"] + result["owners"]}
    edges = {json.dumps(edge, sort_keys=True) for edge in result["ownership"]}
    for section in ("calls", "dependencies"):
        nodes.update(
            ("symbol", node["symbol_id"]) if "symbol_id" in node else ("file", node["path"])
            for node in result[section]["nodes"]
        )
        edges.update(json.dumps(edge, sort_keys=True) for edge in result[section]["edges"])
    assert len(nodes) <= 3 and len(edges) <= 1
    assert result["analysis_status"] == "partial" and "graph_budget" in result["truncated_reason"]


def test_requested_sections_omitted_after_deadline_are_explicit(facts, monkeypatch):
    moments = iter([0.0, 1.0, 1.0, 1.0])
    monkeypatch.setattr(analysis_module.time, "monotonic", lambda: next(moments, 1.0))
    result = analyze(
        facts,
        "file_context",
        {"path": "uses.py"},
        include=["ownership", "calls", "dependencies"],
        max_ms=100,
    )
    assert result["analysis_status"] == "partial"
    assert result["omitted_sections"] == ["calls", "dependencies"]
    assert "time_budget" in result["truncated_reason"]


def test_invalid_selector_does_not_echo_supplied_input(facts):
    with pytest.raises(ValueError) as error:
        analyze(facts, "file_context", {"path": "calls.py", "unknown": "private content"})
    assert "private content" not in str(error.value)


def test_shared_bounding_prefers_dataflow_value_ids_over_symbol_metadata():
    flow = {
        "root": "df_0",
        "nodes": [
            {"id": f"df_{n}", "symbol_id": "same_owner", "path": "a.py", "kind": "value"}
            for n in range(3)
        ],
        "edges": [
            {"source": "df_0", "target": "df_1", "kind": "DATA_FLOW", "evidence": "x" * 400},
            {"source": "df_1", "target": "df_2", "kind": "DATA_FLOW", "evidence": "y" * 400},
        ],
    }
    result = {
        "mode": "combined",
        "analysis_status": "ready",
        "truncated_reason": [],
        "dataflow": flow,
    }
    bounded = apply_analysis_bounds(result, 2, 2)
    assert {node["id"] for node in bounded["dataflow"]["nodes"]} == {"df_0", "df_1"}
    bounded = bound_analysis_result(bounded, 1000)
    ids = {node["id"] for node in bounded["dataflow"]["nodes"]}
    assert "df_0" in ids
    assert all(
        edge["source"] in ids and edge["target"] in ids for edge in bounded["dataflow"]["edges"]
    )


@pytest.mark.parametrize(
    "parameter,value", [("max_depth", 6), ("max_nodes", True), ("max_edges", 0), ("max_ms", 50)]
)
def test_invalid_budgets_are_not_coerced(facts, parameter, value):
    with pytest.raises(ValueError, match="bounds"):
        analyze(facts, "callees", exact("calls.py", "start"), **{parameter: value})


def test_mcp_tool_live_only_strict_schema_and_one_backend_query(facts):
    class LiveFacade:
        source_mode = "live"
        calls = 0

        def code_query(self, project, snapshot, operation, **parameters):
            assert project == "sample" and operation == "analyze_code"
            self.calls += 1
            with facts.read_connection() as db:
                return analyze_structure(db, project, 1, **parameters)

    async def check():
        mirror = build_mcp(facts)
        assert {tool.name for tool in await mirror.list_tools()} == READ_TOOL_NAMES
        facade = LiveFacade()
        mcp = build_mcp(facade, project_scope="sample")
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        assert set(tools) == LIVE_READ_TOOL_NAMES
        assert tools["analyze_code"].annotations.read_only_hint
        assert (
            tools["analyze_code"].input_schema["$defs"]["AnalysisSelector"]["additionalProperties"]
            is False
        )
        reply = await mcp.call_tool(
            "analyze_code",
            {
                "project_id": "sample",
                "mode": "file_context",
                "selector": {"path": "uses.py"},
                "include": ["ownership", "calls", "dependencies"],
            },
        )
        assert reply.structured_content["path"] == "uses.py" and facade.calls == 1
        for extra in (
            {"unexpected": True},
            {"max_nodes": "50"},
            {"max_nodes": True},
            {"selector": {"path": "uses.py", "unexpected": True}},
        ):
            with pytest.raises(ToolError, match="INVALID_ANALYSIS_REQUEST"):
                await mcp.call_tool(
                    "analyze_code",
                    {
                        "project_id": "sample",
                        "mode": "file_context",
                        "selector": {"path": "uses.py"},
                        **extra,
                    },
                )
        with pytest.raises(ToolError, match="outside"):
            await mcp.call_tool(
                "analyze_code",
                {
                    "project_id": "other",
                    "mode": "file_context",
                    "selector": {"path": "uses.py"},
                },
            )
        assert facade.calls == 1

    asyncio.run(check())

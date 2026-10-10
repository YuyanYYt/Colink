"""Guarded Python flow integration, strict MCP schema and request-local resources."""

import asyncio
import json
from threading import Thread

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

import code_context.live_dataflow as flow
from code_context.fact_payloads import FactCacheError
from code_context.intelligence_analysis import AnalysisBudgetExceeded
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "flow.py").write_text(
        "def normalize(value, extra=1):\n"
        "    cleaned = value + extra\n"
        "    return cleaned\n\n"
        "def entry(value):\n"
        "    result = normalize(value, extra=2)\n"
        "    return result\n"
    )
    backend = LiveQueries({"sample": SourceAccess(root)})
    service = LiveIndexService(tmp_path / "index")
    backend.index_service = service
    handle = backend.resolve_snapshot("sample")[0]
    ready = backend.code_query("sample", handle, "symbol_search", query="entry", exact=True)
    assert not ready.get("index_not_ready"), ready
    yield root, backend, service, handle
    service.close()
    backend.close()


def selector(method="entry", kind="parameter", **value):
    return {
        "path": "flow.py",
        "qualname": method,
        "value": {"kind": kind, **({"name": "value"} if kind == "parameter" else {}), **value},
    }


def query(parts, mode="data_impact", **options):
    _, backend, _, handle = parts
    return backend.code_query(
        "sample", handle, "analyze_code", mode=mode, selector=selector(), **options
    )


@pytest.mark.parametrize("mode", ["data_origin", "data_impact", "data_path", "combined"])
def test_all_modes_use_actual_calls_and_saved_source(parts, mode):
    _, backend, _, handle = parts
    selected = selector(kind="return") if mode == "data_origin" else selector()
    target = selector(kind="return") if mode == "data_path" else None
    result = backend.code_query(
        "sample",
        handle,
        "analyze_code",
        mode=mode,
        selector=selected,
        target=target,
        max_depth=2,
    )
    assert result["analysis_status"] == "ready", result
    graph = result.get("dataflow", result)
    assert graph["nodes"] and graph["edges"]
    assert "ARG_TO_PARAM" in {edge["kind"] for edge in graph["edges"]}
    assert "RETURN_TO_CALLSITE" in {edge["kind"] for edge in graph["edges"]}
    assert graph["coverage"]["runtime_trace"] is False
    assert not result["index_partial"]
    if mode == "data_path":
        assert result["path_found"] and result["path_resolution"] == "static"
    if mode == "combined":
        assert result["structure"]["symbol"]["qualname"] == "entry"
        assert {"ownership", "calls", "dependencies"} <= result["structure"].keys()


def test_ast_runs_outside_fact_lock_and_uses_one_source_guard_pair(parts, monkeypatch):
    root, _, service, _ = parts
    guards, locks = [], []
    original_guard = service._guard
    original = flow.analyze_dataflow

    def guard(*args):
        guards.append(1)
        return original_guard(*args)

    def compute(**options):
        def lock_probe():
            acquired = service._lock.acquire(timeout=0.5)
            locks.append(acquired)
            if acquired:
                service._lock.release()

        thread = Thread(target=lock_probe)
        thread.start()
        thread.join(timeout=1)
        return original(**options)

    monkeypatch.setattr(service, "_guard", guard)
    monkeypatch.setattr(flow, "analyze_dataflow", compute)
    before = (root / "flow.py").read_bytes()
    tables = [tuple(row) for row in service._db.execute("SELECT name FROM sqlite_master")]
    assert query(parts)["analysis_status"] == "ready"
    assert guards == [1, 1] and locks == [True]
    assert (root / "flow.py").read_bytes() == before
    assert tables == [tuple(row) for row in service._db.execute("SELECT name FROM sqlite_master")]


@pytest.mark.parametrize("change", ["source", "publication"])
def test_finished_flow_cannot_escape_changed_source_or_publication(parts, monkeypatch, change):
    root, _, service, _ = parts
    original = flow.analyze_dataflow

    def compute(**options):
        result = original(**options)
        if change == "source":
            (root / "flow.py").write_text("def entry(value):\n    return 0\n")
        else:
            service.invalidate("sample")
        return result

    monkeypatch.setattr(flow, "analyze_dataflow", compute)
    with pytest.raises(SourceError, match="CHANGED|INVALID"):
        query(parts)


def test_callback_budget_returns_completed_local_slice(parts, monkeypatch):
    original = flow.FlowSources.load_relations

    def incoming_budget(self, identifier, *, direction):
        if direction == "incoming":
            raise flow.DataflowBudgetExceeded
        return original(self, identifier, direction=direction)

    monkeypatch.setattr(flow.FlowSources, "load_relations", incoming_budget)
    result = query(parts)
    assert result["analysis_status"] == "partial"
    assert "time_budget" in result["truncated_reason"]
    assert result["nodes"] and result["edges"]


def test_corrupt_facts_return_rebuild_status_without_decoder_content(parts, monkeypatch):
    def corrupt(*_):
        raise FactCacheError("sensitive derived payload must not escape")

    monkeypatch.setattr(flow, "decode_relation", corrupt)
    result = query(parts)
    assert result["index_not_ready"] and result["reason"] == "INDEX_CACHE_INVALID"
    assert "sensitive" not in str(result)


def test_partial_index_marks_flow_partial(parts):
    _, _, service, _ = parts
    with service._lock:
        stats = json.loads(service._cached("sample")["stats"])
        stats["partial"] = True
        service._db.execute(
            "UPDATE li_projects SET stats=? WHERE project_id=?", (json.dumps(stats), "sample")
        )
        service._db.commit()
    assert query(parts)["analysis_status"] == "partial"


def test_combined_output_budget_counts_values_symbols_and_files(parts):
    result = query(parts, mode="combined", max_nodes=8, max_edges=7, max_chars=4500)
    identities, edges = set(), set()

    def collect(value):
        if isinstance(value, dict):
            for node in value.get("nodes", []):
                identities.add(node.get("id", node.get("symbol_id", node.get("path"))))
            if "symbol" in value:
                identities.add(value["symbol"]["symbol_id"])
            for edge in value.get("edges", []):
                edges.add(json.dumps(edge, sort_keys=True))
            for child in value.values():
                collect(child)

    collect(result)
    assert len(identities) <= 8 and len(edges) <= 7
    assert len(json.dumps(result, ensure_ascii=False)) <= 4500
    assert result["analysis_status"] == "partial" and result["truncated"]


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "parameter"},
        {"kind": "parameter", "name": "not an identifier"},
        {"kind": "parameter", "name": "value", "line": 1},
        {"kind": "occurrence", "line": True, "column": 1},
        {"kind": "return", "column": 1},
        {"kind": "return", "name": "value"},
        {"kind": "occurrence", "line": 1, "column": 1, "extra": "raw token"},
    ],
)
def test_value_selector_is_strict_and_does_not_disclose_invalid_input(value):
    with pytest.raises(ValidationError):
        flow.FlowSelector.model_validate({"path": "flow.py", "qualname": "entry", "value": value})


def test_public_mcp_schema_discovers_all_modes_and_rejects_unknown_nested_fields(parts):
    _, backend, _, handle = parts

    async def run():
        mcp = build_mcp(backend)
        tool = next(tool for tool in await mcp.list_tools() if tool.name == "analyze_code")
        encoded = json.dumps(tool.input_schema)
        assert all(mode in encoded for mode in flow.DATAFLOW_MODES)
        arguments = {
            "project_id": "sample",
            "snapshot": handle,
            "mode": "data_path",
            "selector": selector(),
            "target": selector(kind="return"),
            "max_depth": 2,
        }
        response = await mcp.call_tool("analyze_code", arguments)
        assert response.structured_content["analysis_status"] == "ready"
        assert response.structured_content["path_found"] is True
        invalid = {**arguments, "selector": selector(secret="untrusted-token")}
        with pytest.raises(ToolError, match="INVALID_ANALYSIS_REQUEST") as error:
            await mcp.call_tool("analyze_code", invalid)
        assert "untrusted-token" not in str(error.value)

    asyncio.run(run())


def test_deadline_sql_interrupt_is_converted_only_to_budget_exception(parts, monkeypatch):
    _, _, service, _ = parts

    # Callback budget errors are distinct from source/cache failures, which must reject.
    def budget(*_args, **_kwargs):
        raise AnalysisBudgetExceeded

    monkeypatch.setattr(flow, "_symbol", budget)
    result = query(parts)
    assert result["analysis_status"] == "partial" and "time_budget" in result["truncated_reason"]
    assert service.status("sample")["status"] == "ready"


def test_flow_reuses_published_facts_after_restart(parts, tmp_path):
    root, backend, service, _ = parts
    before = query(parts)
    path = service.data_dir if hasattr(service, "data_dir") else tmp_path / "index"
    service.close()
    replacement = LiveIndexService(path)
    backend.index_service = replacement
    try:
        handle = backend.resolve_snapshot("sample")[0]
        result = backend.code_query(
            "sample", handle, "analyze_code", mode="data_impact", selector=selector()
        )
        assert result["nodes"] == before["nodes"] and result["edges"] == before["edges"]
        assert result["analysis_status"] == "ready"
        assert (root / "flow.py").is_file()
    finally:
        replacement.close()


@pytest.mark.parametrize("mode", ["data_origin", "combined"])
def test_multiple_returns_are_explicit_candidates_even_after_nested_clipping(parts, mode):
    root, backend, service, _ = parts
    (root / "flow.py").write_text(
        "def entry(value):\n"
        + "".join(f"    if value == {i}:\n        return value + {i}\n" for i in range(40))
        + "    return 0\n"
    )
    service.invalidate("sample")
    handle = backend.resolve_snapshot("sample")[0]
    result = backend.code_query(
        "sample",
        handle,
        "analyze_code",
        mode=mode,
        selector=selector(kind="return"),
        max_chars=6000,
    )
    selected = result.get("dataflow", result)
    assert selected["analysis_status"] == "ambiguous"
    assert selected["candidates"] and selected["total"] == 41
    assert selected["next_offset"] is None
    assert len(json.dumps(result, ensure_ascii=False)) <= 6000

"""Isolated M2 timings and interleaved structural regression against frozen M1.

This generates source and runtime data below .artifacts, never executes project
code, and reports P50/P95 from 50 samples. No web/model/energy claims are made.
"""

import argparse
import importlib.util
import json
import math
import statistics
import time
import tracemalloc
from pathlib import Path

import code_context.intelligence_queries as queries
import code_context.live_dataflow as live_flow
from code_context.live import LiveQueries
from code_context.live_dataflow import FlowSources
from code_context.live_index import LiveIndexService
from code_context.source_access import SourceAccess


def summary(values):
    return {
        "samples": len(values),
        "p50": statistics.median(values),
        "p95": sorted(values)[math.ceil(len(values) * 0.95) - 1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--m1-queries", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(repo / ".artifacts"):
        parser.error("output must be a new .artifacts directory")
    output.mkdir(parents=True)
    source = output / "source"
    source.mkdir()
    for i in range(1000):
        (source / f"f{i:04}.py").write_text(
            f"from f{(i + 1) % 1000:04} import f as next_fn\n"
            "def f(value):\n" + "    value = next_fn(value)\n" * 10 + "    return value\n"
        )
    (source / "flow.py").write_text(
        "def normalize(value, extra=1):\n    clean = value + extra\n    return clean\n"
        "def entry(value):\n    result = normalize(value, extra=2)\n    return result\n"
        "def overwritten(value):\n    result = value\n    result = 0\n    return result\n"
        "def recursive(value):\n    return recursive(value)\n"
    )
    spec = importlib.util.spec_from_file_location("m1_frozen_queries", args.m1_queries)
    m1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m1)
    backend = LiveQueries({"sample": SourceAccess(source)})
    service = LiveIndexService(output / "index")
    backend.index_service = service
    report = {"synthetic": True, "samples": 50, "structural_order": "interleaved", "cases": []}
    try:
        handle = backend.resolve_snapshot("sample")[0]
        found = backend.code_query(
            "sample", handle, "symbol_search", query="f", exact=True, path_prefix="f0000.py"
        )
        assert not found.get("index_not_ready"), found
        identifier = found["symbols"][0]["symbol_id"]
        for case, operation, options in (
            ("file_outgoing", "file_dependencies", {"path": "f0000.py"}),
            ("file_incoming", "file_dependencies", {"path": "f0000.py", "direction": "incoming"}),
            (
                "call_both",
                "symbol_graph",
                {"symbol_id": identifier, "graph": "call", "direction": "both", "depth": 3},
            ),
        ):
            original = queries.QUERIES[operation]
            metrics = {"m1": [], "m2": []}
            golden = None
            try:
                for iteration in range(50):
                    variants = [("m1", m1), ("m2", queries)]
                    if iteration % 2:
                        variants.reverse()
                    for variant, module in variants:
                        queries.QUERIES[operation] = getattr(module, operation)
                        tick = time.perf_counter()
                        result = backend.code_query("sample", handle, operation, **options)
                        metrics[variant].append((time.perf_counter() - tick) * 1000)
                        if golden is None:
                            golden = result
                        assert golden == result, (case, variant)
            finally:
                queries.QUERIES[operation] = original
            before, after = summary(metrics["m1"]), summary(metrics["m2"])
            report["cases"].append(
                {
                    "case": case,
                    "m1_live_ms": before,
                    "m2_live_ms": after,
                    "p95_ratio": after["p95"] / before["p95"],
                    "golden_equal": True,
                }
            )
        calls, timings, facts, component = [], [], [], []
        original_source = FlowSources.load_source
        original_flow = live_flow.analyze_dataflow

        def load_source(provider, path):
            calls.append(path)
            return original_source(provider, path)

        def measured_flow(**options):
            tick = time.perf_counter()
            try:
                return original_flow(**options)
            finally:
                component.append((time.perf_counter() - tick) * 1000)

        FlowSources.load_source = load_source
        live_flow.analyze_dataflow = measured_flow
        try:
            for mode in ("data_origin", "data_impact", "data_path", "combined"):
                component.clear()
                selected = {
                    "path": "flow.py",
                    "qualname": "entry",
                    "value": {"kind": "return"}
                    if mode == "data_origin"
                    else {"kind": "parameter", "name": "value"},
                }
                options = (
                    {
                        "target": {
                            "path": "flow.py",
                            "qualname": "entry",
                            "value": {"kind": "return"},
                        }
                    }
                    if mode == "data_path"
                    else {}
                )
                values, sizes, reads = [], [], []
                golden = None
                for _ in range(50):
                    calls.clear()
                    tick = time.perf_counter()
                    result = backend.code_query(
                        "sample",
                        handle,
                        "analyze_code",
                        mode=mode,
                        selector=selected,
                        max_depth=2,
                        **options,
                    )
                    values.append((time.perf_counter() - tick) * 1000)
                    sizes.append(len(json.dumps(result).encode()))
                    reads.append(len(calls))
                    assert result["analysis_status"] == "ready", result
                    if golden is None:
                        golden = result
                    assert result == golden
                timings.append(
                    {
                        "mode": mode,
                        "live_ms": summary(values),
                        "component_with_callbacks_ms": summary(component),
                        "response_bytes": summary(sizes),
                        "source_callback_calls": summary(reads),
                    }
                )
            # No persisted graph/table or request-local Python trees survive in the cache.
            before = [tuple(row) for row in service._db.execute("SELECT name FROM sqlite_master")]
            tracemalloc.start()
            peaks = []
            for _ in range(50):
                backend.code_query(
                    "sample",
                    handle,
                    "analyze_code",
                    mode="data_impact",
                    selector={
                        "path": "flow.py",
                        "qualname": "entry",
                        "value": {"kind": "parameter", "name": "value"},
                    },
                    max_depth=2,
                )
                facts.append(tracemalloc.get_traced_memory()[0])
                peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            assert before == [
                tuple(row) for row in service._db.execute("SELECT name FROM sqlite_master")
            ]
            report["dataflow"] = timings
            report["python_allocations"] = {
                "samples": 50,
                "peak_traced_bytes": max(peaks),
                "retained_first": facts[0],
                "retained_last": facts[-1],
                "process_rss_not_measured": True,
            }
            report["persisted_graph_tables_added"] = False
        finally:
            FlowSources.load_source = original_source
            live_flow.analyze_dataflow = original_flow
    finally:
        service.close()
        backend.close()
        (output / "performance.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()

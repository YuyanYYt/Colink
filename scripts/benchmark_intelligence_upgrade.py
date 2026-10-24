"""Replayable synthetic Live-query comparison; never opens user project data.

Retains the generated source and SQLite caches in a new .artifacts directory.
Timings include current source guards and durable last-used commits, but exclude
native UI, tunnel/MCP transport, and model execution. No P99 is reported.
"""

import argparse
import importlib.util
import json
import math
import resource
import statistics
import time
from pathlib import Path

import code_context.intelligence_queries as queries
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.source_access import SourceAccess

REPO = Path(__file__).resolve().parents[1]


def summary(values):
    return {
        "samples": len(values),
        "p50": statistics.median(values),
        "p95": sorted(values)[math.ceil(len(values) * 0.95) - 1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=50)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(REPO / ".artifacts") or output.exists():
        parser.error("output must be a new directory below .artifacts")
    if not 100 <= args.files <= 5000 or not 50 <= args.repeats <= 200:
        parser.error("files 100..5000; repeats 50..200")
    spec = importlib.util.spec_from_file_location("upgrade_baseline", args.baseline.resolve())
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    output.mkdir(parents=True)
    source = output / "source"
    package = source / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for index in range(args.files):
        target = (index + 1) % args.files
        (package / f"f{index:05}.py").write_text(
            f"from pkg.f{target:05} import f as next_fn\ndef f(value):\n"
            + "    value = next_fn(value)\n" * 10
            + "    return value\n"
        )
    backend = LiveQueries({"bench": SourceAccess(source)})
    service = LiveIndexService(output / "index")
    backend.index_service = service
    report = {
        "synthetic": True,
        "scope": "local Live backend including source guards; no web/tunnel/model benchmark",
        "files": args.files + 1,
        "results": [],
        "output": str(output),
    }
    try:
        handle = backend.resolve_snapshot("bench")[0]
        start = time.perf_counter()
        found = backend.code_query(
            "bench", handle, "symbol_search", query="f", exact=True, path_prefix="pkg/f00000.py"
        )
        assert not found.get("index_not_ready"), found
        report["cold_build_and_search_ms"] = (time.perf_counter() - start) * 1000
        root = found["symbols"][0]["symbol_id"]
        cases = [
            ("file_outgoing", "file_dependencies", {"path": "pkg/f00000.py"}),
            (
                "file_incoming",
                "file_dependencies",
                {"path": "pkg/f00000.py", "direction": "incoming"},
            ),
            (
                "call_both",
                "symbol_graph",
                {"symbol_id": root, "graph": "call", "direction": "both", "depth": 3},
            ),
        ]
        for case, operation, parameters in cases:
            original = queries.QUERIES[operation]
            golden = None
            for variant, module in (("baseline", baseline), ("current", queries)):
                function = getattr(module, operation)
                component, guards, total, statements, decoded = [], [], [], [], []
                counters = {"sql": 0, "decoded": 0}
                original_decode, original_guard = module.decode_relation, service._guard

                def decode(payload, _counters=counters, _decode=original_decode):
                    _counters["decoded"] += 1
                    return _decode(payload)

                def guard(*values, _guard=original_guard, _metrics=guards, **options):
                    tick = time.perf_counter()
                    try:
                        return _guard(*values, **options)
                    finally:
                        _metrics.append((time.perf_counter() - tick) * 1000)

                def measured(
                    db, project, revision, _function=function, _metrics=component, **options
                ):
                    tick = time.perf_counter()
                    try:
                        return _function(db, project, revision, **options)
                    finally:
                        _metrics.append((time.perf_counter() - tick) * 1000)

                module.decode_relation = decode
                service._guard = guard
                queries.QUERIES[operation] = measured
                service._db.set_trace_callback(
                    lambda _sql, _counts=counters: _counts.__setitem__("sql", _counts["sql"] + 1)
                )
                try:
                    for _ in range(args.repeats):
                        counters.update(sql=0, decoded=0)
                        tick = time.perf_counter()
                        result = backend.code_query("bench", handle, operation, **parameters)
                        total.append((time.perf_counter() - tick) * 1000)
                        statements.append(counters["sql"])
                        decoded.append(counters["decoded"])
                        if golden is None:
                            golden = result
                        assert result == golden, (case, variant)
                    report["results"].append(
                        {
                            "case": case,
                            "variant": variant,
                            "live_ms": summary(total),
                            "component_ms": summary(component),
                            "guard_ms": summary(guards),
                            "sql_statements": summary(statements),
                            "decoded_relations": summary(decoded),
                            "response_bytes": len(json.dumps(result).encode()),
                            "golden_equal": True,
                        }
                    )
                    print(json.dumps(report["results"][-1]), flush=True)
                finally:
                    service._db.set_trace_callback(None)
                    module.decode_relation = original_decode
                    service._guard = original_guard
                    queries.QUERIES[operation] = original
        report["pragmas"] = {
            key: service._db.execute(f"PRAGMA {key}").fetchone()[0]
            for key in (
                "journal_mode",
                "synchronous",
                "auto_vacuum",
                "temp_store",
                "cache_size",
                "page_size",
                "page_count",
                "max_page_count",
                "foreign_keys",
                "user_version",
            )
        }
        report["tables"] = {
            table: service._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("files", "ci_symbols", "ci_relations", "li_parse_cache")
        }
        report["database_bytes"] = service.path.stat().st_size
        # Evaluate the optional index only in this disposable generated database.
        # A failed end-to-end latency gate stops the candidate before adoption.
        captured = []
        service._db.set_trace_callback(captured.append)
        incoming = backend.code_query(
            "bench", handle, "file_dependencies", path="pkg/f00000.py", direction="incoming"
        )
        service._db.set_trace_callback(None)
        frontier_sql = next(sql for sql in captured if "WITH frontier(path)" in sql)
        before_plan = [
            tuple(row) for row in service._db.execute("EXPLAIN QUERY PLAN " + frontier_sql)
        ]
        tick = time.perf_counter()
        service._db.execute(
            "CREATE INDEX candidate_relation_target_path "
            "ON ci_relations(project_id, revision, target_path)"
        )
        service._db.execute("ANALYZE ci_relations")
        service._db.commit()
        index_build_ms = (time.perf_counter() - tick) * 1000
        indexed_bytes = service.path.stat().st_size
        after_plan = [
            tuple(row) for row in service._db.execute("EXPLAIN QUERY PLAN " + frontier_sql)
        ]
        candidate_ms = []
        for _ in range(args.repeats):
            tick = time.perf_counter()
            candidate = backend.code_query(
                "bench", handle, "file_dependencies", path="pkg/f00000.py", direction="incoming"
            )
            candidate_ms.append((time.perf_counter() - tick) * 1000)
            assert candidate == incoming
        before = next(
            row["live_ms"]
            for row in report["results"]
            if row["case"] == "file_incoming" and row["variant"] == "current"
        )
        after = summary(candidate_ms)
        latency_improvement = 1 - after["p95"] / before["p95"]
        report["optional_target_path_index"] = {
            "before_plan": before_plan,
            "after_plan": after_plan,
            "build_and_statistics_ms": index_build_ms,
            "database_growth_ratio": indexed_bytes / report["database_bytes"] - 1,
            "baseline_live_ms": before,
            "candidate_live_ms": after,
            "live_p95_improvement_ratio": latency_improvement,
            "passes_latency_gate": latency_improvement >= 0.20,
            "product_index_added": False,
            "remaining_build_update_gate": "not evaluated if latency gate fails",
        }
        service._db.execute("DROP INDEX candidate_relation_target_path")
        service._db.commit()
        advanced_ms = []
        for _ in range(args.repeats):
            tick = time.perf_counter()
            advanced = backend.code_query(
                "bench",
                handle,
                "analyze_code",
                mode="file_context",
                selector={"path": "pkg/f00000.py"},
                include=["ownership", "calls", "dependencies"],
            )
            advanced_ms.append((time.perf_counter() - tick) * 1000)
            assert advanced["analysis_status"] == "ready", advanced
        report["unified_file_context_live_ms"] = summary(advanced_ms)
        report["unified_file_context_response_bytes"] = len(json.dumps(advanced).encode())
        report["process_peak_rss_bytes_macos"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        changed = package / "f00000.py"
        changed.write_text(changed.read_text().replace("return value", "return value + 1"))
        service.invalidate("bench")
        handle = backend.resolve_snapshot("bench")[0]
        tick = time.perf_counter()
        result = backend.code_query("bench", handle, "file_dependencies", path="pkg/f00000.py")
        assert not result.get("index_not_ready"), result
        report["single_edit_and_query_ms"] = (time.perf_counter() - tick) * 1000
        service.close()
        service = LiveIndexService(output / "index")
        backend.index_service = service
        handle = backend.resolve_snapshot("bench")[0]
        tick = time.perf_counter()
        result = backend.code_query("bench", handle, "file_dependencies", path="pkg/f00000.py")
        assert not result.get("index_not_ready"), result
        report["restart_and_query_ms"] = (time.perf_counter() - tick) * 1000
        report["cold_edit_restart_samples"] = 1
    finally:
        service.close()
        (output / "performance.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"report": str(output / "performance.json")}), flush=True)


if __name__ == "__main__":
    main()

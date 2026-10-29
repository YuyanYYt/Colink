"""Compare Live text search against one retained source baseline.

Creates only a new synthetic fixture below .artifacts. Timings include source
discovery, reads, context guards and JSON-budget work, not MCP transport or UI.
Raw samples and exact result comparisons are retained; no user project is run.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import resource
import statistics
import time
from pathlib import Path

import code_context.read_search as current
from code_context.live import LiveQueries
from code_context.source_access import SourceAccess

REPO = Path(__file__).resolve().parents[1]


def summary(samples):
    return {
        "samples": len(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[math.ceil(len(samples) * 0.95) - 1],
    }


def normalized(result):
    return {key: value for key, value in result.items() if key not in {"snapshot", "next_cursor"}}


def measure(module, backend, handle, request):
    counters = {"json_chars_calls": 0, "encoded_chars": 0, "response_encodes": 0, "cursors": 0}
    original_chars, original_cursor = module.json_chars, module.encode_search_cursor

    def chars(value):
        size = original_chars(value)
        counters["json_chars_calls"] += 1
        counters["encoded_chars"] += size
        if isinstance(value, dict) and value.get("matches"):
            counters["response_encodes"] += 1
        return size

    def cursor(*args, **kwargs):
        counters["cursors"] += 1
        return original_cursor(*args, **kwargs)

    module.json_chars, module.encode_search_cursor = chars, cursor
    reads = backend.source("bench").metrics["body_reads"]
    started = time.perf_counter()
    try:
        result = module.search_text(backend, "bench", snapshot=handle, **request)
        elapsed = (time.perf_counter() - started) * 1000
    finally:
        module.json_chars, module.encode_search_cursor = original_chars, original_cursor
    counters["body_reads"] = backend.source("bench").metrics["body_reads"] - reads
    counters["scanned_bytes"] = result["scanned_bytes"]
    assert current.json_chars(result) <= request.get("max_chars", 20_000)
    return result, elapsed, counters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--groups", type=int, default=2)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(REPO / ".artifacts") or output.exists():
        parser.error("output must be a new directory below .artifacts")
    if not 1 <= args.files <= 5000 or not 50 <= args.repeats <= 200 or not 2 <= args.groups <= 5:
        parser.error("files 1..5000; repeats 50..200; groups 2..5")
    spec = importlib.util.spec_from_file_location("search_baseline", args.baseline.resolve())
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    output.mkdir(parents=True)
    source = output / "source"
    source.mkdir()
    for number in range(args.files):
        (source / f"file{number:05}.txt").write_text("nonmatching synthetic source\n" * 8)
    (source / "file00000.txt").write_text(
        "".join(f'needle 中\\"\t {number} short line\n' for number in range(300))
    )
    cases = {
        "many_hits": {"query": "needle", "limit": 200, "max_chars": 50_000},
        "small_budget_context": {
            "query": "needle",
            "limit": 200,
            "context_lines": 1,
            "max_chars": 1600,
        },
        "no_hits": {"query": "NO_MATCH_29B86B5", "max_chars": 12_000},
    }
    backends = {
        name: LiveQueries({"bench": SourceAccess(source)}) for name in ("baseline", "current")
    }
    modules = {"baseline": baseline, "current": current}
    handles = {name: backend.resolve_snapshot("bench")[0] for name, backend in backends.items()}
    report = {
        "scope": "synthetic Live text search, including source guards; excludes MCP/UI/model",
        "files": args.files,
        "baseline": str(args.baseline.resolve()),
        "source_sha256": {
            "baseline": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
            "current": hashlib.sha256(Path(current.__file__).read_bytes()).hexdigest(),
        },
        "group_lifecycle": (
            "fresh Live backend and context per group; alternating variants per sample"
        ),
        "groups": [],
        "equivalent": False,
    }
    try:
        for group in range(args.groups):
            if group:
                for backend in backends.values():
                    backend.close()
                backends = {name: LiveQueries({"bench": SourceAccess(source)}) for name in modules}
                handles = {
                    name: backend.resolve_snapshot("bench")[0] for name, backend in backends.items()
                }
            group_report = {"group": group, "cases": {}}
            for case, request in cases.items():
                timings = {name: [] for name in modules}
                counters = {name: [] for name in modules}
                gold = None
                # Warm both paths once, without counting the first source/context work.
                for name, module in modules.items():
                    result, _, _ = measure(module, backends[name], handles[name], request)
                    if gold is None:
                        gold = normalized(result)
                    assert normalized(result) == gold, (group, case, name)
                for repeat in range(args.repeats):
                    order = (
                        ("baseline", "current") if (repeat + group) % 2 else ("current", "baseline")
                    )
                    for name in order:
                        result, elapsed, counts = measure(
                            modules[name], backends[name], handles[name], request
                        )
                        assert normalized(result) == gold, (group, case, repeat, name)
                        timings[name].append(elapsed)
                        counters[name].append(counts)
                group_report["cases"][case] = {
                    name: {
                        **summary(timings[name]),
                        "raw_ms": timings[name],
                        "counts": counters[name],
                    }
                    for name in modules
                }
            report["groups"].append(group_report)
        report["equivalent"] = True
        # This common-process high-water mark cannot attribute memory to a variant.
        report["common_process_peak_rss"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["rss_scope"] = "shared process lifetime; not a per-variant memory comparison"
    finally:
        for backend in backends.values():
            backend.close()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output), "equivalent": report["equivalent"]}))


if __name__ == "__main__":
    main()

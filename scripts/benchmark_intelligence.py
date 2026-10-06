"""Synthetic local timings, not web/model/business performance. Retains all data."""

import argparse
import json
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

from code_context.models import FileChange, SyncBatch, content_hash
from code_context.storage import MirrorStore


def percentile(values, p):
    return sorted(values)[round((len(values) - 1) * p)]


def timings(function, count=30):
    values = []
    for _ in range(count):
        started = time.perf_counter()
        function()
        values.append((time.perf_counter() - started) * 1000)
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "p99_ms": round(percentile(values, 0.99), 3),
        "samples": count,
    }


def batch(files, revision, name):
    return SyncBatch(
        request_id=name,
        base_revision=revision,
        mode="full" if not revision else "delta",
        changes=[
            FileChange(op="upsert", path=path, content=text, sha256=content_hash(text))
            for path, text in files.items()
        ],
    )


def run(directory, count):
    files = {
        "pkg/base.py": "class Base: pass\n",
        "java/bench/Base.java": "package bench; public class Base {}\n",
    }
    for i in range(count // 2):
        files[f"pkg/part{i}.py"] = (
            f"from .base import Base\nclass Child{i}(Base):\n"
            f"    def clean(self):\n        return {i}\n"
            f"def clean_{i}():\n    return Child{i}().clean()\n"
        )
        files[f"java/bench/Part{i}.java"] = (
            f"package bench; public class Part{i} extends Base {{ "
            f"public int clean() {{ return {i}; }} "
            "public int run() { return this.clean(); } }\n"
        )
    store = MirrorStore(directory / f"benchmark-{count}.sqlite3")
    started = time.perf_counter()
    store.apply("bench", batch(files, 0, "initial"))
    initial = (time.perf_counter() - started) * 1000
    status = store.code_index_status("bench", 1)
    assert not status["partial"]
    index_query = timings(
        lambda: store.code_query("bench", None, "symbol_search", query="clean_0", exact=True)
    )
    literal_query = timings(lambda: store.search_code("bench", "clean_0", limit=20))
    updates = []
    for i in range(1, 11):
        started = time.perf_counter()
        store.apply(
            "bench",
            batch({"pkg/part0.py": files["pkg/part0.py"] + f"# edit {i}\n"}, i, f"edit-{i}"),
        )
        updates.append((time.perf_counter() - started) * 1000)
    update_status = store.code_index_status("bench", 11)
    assert update_status["parsed_files"] == 1
    assert update_status["reused_relation_files"] == len(files) - 1
    graph_query = timings(
        lambda: store.code_query(
            "bench", None, "file_dependencies", path="pkg/base.py", direction="incoming"
        )
    )
    with store.read_connection() as db:
        retained = db.execute("SELECT COUNT(*) FROM ci_snapshots").fetchone()[0]
        cache_rows = db.execute("SELECT COUNT(*) FROM ci_parse_cache").fetchone()[0]
    return {
        "source_files": len(files),
        "source_bytes": sum(len(t.encode()) for t in files.values()),
        "initial_commit_ms": round(initial, 3),
        "initial_index": status,
        "exact_symbol_query": index_query,
        "literal_search_query": literal_query,
        "reverse_file_graph_query": graph_query,
        "single_file_update": {
            "p50_ms": round(statistics.median(updates), 3),
            "p95_ms": round(percentile(updates, 0.95), 3),
            "samples": 10,
        },
        "last_update_index": update_status,
        "retained_index_states": retained,
        "parse_cache_rows": cache_rows,
        "database_bytes": store.database.stat().st_size,
        "database": str(store.database),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, default=Path(".artifacts/benchmarks"))
    args = parser.parse_args()
    args.parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="colink-intelligence-", dir=args.parent)).resolve()
    report = {
        "scope": "synthetic loopback/local database only; no web/model calls",
        "cases": [run(directory, size) for size in (100, 1000)],
    }
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["process_peak_rss_bytes"] = rss if sys.platform == "darwin" else rss * 1024
    output = directory / "report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(output), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

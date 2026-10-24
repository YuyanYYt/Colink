"""Actual-source Rust pilot, including IPC and guarded Live-query costs.

This does not enable a native production backend. Graphs are detached from a new
private Live index; Rust receives only authorized paths and resolved file edges.
No project is imported, compiled or executed. Outputs and corpora are retained.
"""

import argparse
import hashlib
import json
import math
import os
import resource
import selectors
import statistics
import subprocess
import time
from collections import defaultdict, deque
from pathlib import Path

import code_context.intelligence_queries as queries
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.source_access import SourceAccess

REPO = Path(__file__).resolve().parents[1]


def summary(values):
    return {
        "samples": len(values),
        "p50_ms": statistics.median(values),
        "p95_ms": sorted(values)[math.ceil(len(values) * 0.95) - 1],
    }


class NativeGraphAdapter:
    """Fixed bounded operations over a private child; no arbitrary commands."""

    def __init__(self, binary):
        self.process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.names = []
        self.position = {}

    def request(self, payload):
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("pilot request budget")
        self.process.stdin.write(data)
        self.process.stdin.flush()
        if not self.selector.select(timeout=10):
            raise TimeoutError("native pilot deadline")
        line = self.process.stdout.readline(8 * 1024 * 1024 + 1)
        if len(line) > 8 * 1024 * 1024 or not line.endswith(b"\n"):
            raise ValueError("native pilot response budget")
        result = json.loads(line)
        if "error" in result:
            raise ValueError("native pilot rejected request: " + result["error"])
        return result

    def load(self, nodes, adjacency):
        self.names = sorted(nodes)
        self.position = {name: index for index, name in enumerate(self.names)}
        edges = [
            [self.position[source], self.position[target]]
            for source in self.names
            for target in sorted(set(adjacency.get(source, [])))
            if target in self.position
        ]
        return self.request({"command": "load", "nodes": self.names, "edges": edges})

    def components(self, nodes, adjacency):
        # Deliberately includes graph publication/JSON on every real Live query.
        # A future cache would need the same source publication invalidation.
        self.load(nodes, adjacency)
        return self.request({"command": "components"})["components"]

    def close(self):
        self.selector.close()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process.stdout.close()


def reverse_impact(nodes, adjacency, root, depth):
    reverse = defaultdict(list)
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].append(source)
    distance = {root: 0}
    queue = deque([root])
    while queue:
        current = queue.popleft()
        if distance[current] >= depth:
            continue
        for parent in reverse[current]:
            if parent not in distance:
                distance[parent] = distance[current] + 1
                queue.append(parent)
    return {name: value for name, value in distance.items() if name != root}


def normalized(groups):
    return sorted(sorted(group) for group in groups)


def measured(function, repeats):
    values = []
    result = None
    for _ in range(repeats):
        tick = time.perf_counter()
        result = function()
        values.append((time.perf_counter() - tick) * 1000)
    return summary(values), result


def memory_worker(args):
    fixture = json.loads(args.input.read_text())
    nodes = set(fixture["nodes"])
    adjacency = fixture["adjacency"]
    adapter = None
    try:
        if args.worker == "python":
            result = queries._components(nodes, adjacency)
        else:
            adapter = NativeGraphAdapter(args.binary)
            result = adapter.components(nodes, adjacency)
        pids = [os.getpid()] + ([adapter.process.pid] if adapter else [])
        current_rss = subprocess.run(
            ["ps", "-o", "rss=", "-p", ",".join(map(str, pids))],
            check=True,
            capture_output=True,
            text=True,
        )
        print(
            json.dumps(
                {
                    "variant": args.worker,
                    "digest": hashlib.sha256(json.dumps(normalized(result)).encode()).hexdigest(),
                    "python_process_peak_rss_bytes": resource.getrusage(
                        resource.RUSAGE_SELF
                    ).ru_maxrss,
                    "combined_current_rss_kib": sum(map(int, current_rss.stdout.split())),
                    "includes_python_adapter": True,
                }
            )
        )
    finally:
        if adapter is not None:
            adapter.close()


def evaluate(source, output, binary, repeats):
    output.mkdir(parents=True)
    backend = LiveQueries({"actual": SourceAccess(source)})
    service = LiveIndexService(output / "index", wait_seconds=120)
    backend.index_service = service
    handle = backend.resolve_snapshot("actual")[0]
    adapter = None
    original_components = queries._components
    try:
        tick = time.perf_counter()
        initial = backend.code_query(
            "actual", handle, "symbol_search", query="Service", limit=1, max_chars=5000
        )
        assert not initial.get("index_not_ready"), initial
        cold_ms = (time.perf_counter() - tick) * 1000
        nodes = {
            row["path"]
            for row in service._db.execute(
                "SELECT path FROM ci_files WHERE project_id=? AND language IN ('python','java')",
                ("actual",),
            )
        }
        edges = queries._file_edges(service._db, "actual", 1)
        adjacency = defaultdict(list)
        for (left, right), edge in edges.items():
            if left in nodes and right in nodes and not edge["type_only"]:
                adjacency[left].append(right)
        actual = {
            "source_root": str(source),
            "source_kind": "saved real repository source, not generated benchmark code",
            "nodes": len(nodes),
            "edges": sum(map(len, adjacency.values())),
            "cold_index_ms": cold_ms,
            "index_coverage": initial.get("index_coverage"),
            "index_partial": initial.get("index_partial"),
            "file_states": [
                dict(row)
                for row in service._db.execute(
                    "SELECT language,status,count(*) AS files "
                    "FROM ci_files GROUP BY language,status"
                )
            ],
        }
        fixture = {"nodes": sorted(nodes), "adjacency": dict(adjacency)}
        (output / "graph.json").write_text(json.dumps(fixture, ensure_ascii=False))
        adapter = NativeGraphAdapter(binary)
        adapter.load(nodes, adjacency)
        reference = normalized(original_components(nodes, adjacency))
        native = normalized(adapter.request({"command": "components"})["components"])
        assert native == reference, "SCC membership differs"
        in_degree = defaultdict(int)
        for targets in adjacency.values():
            for target in targets:
                in_degree[target] += 1
        roots = sorted(nodes, key=lambda name: (-in_degree[name], name))[:50]
        checks = 0
        for root in roots:
            for depth in (1, 3, 5):
                expected = reverse_impact(nodes, adjacency, root, depth)
                found = adapter.request(
                    {"command": "impact", "root": adapter.position[root], "depth": depth}
                )["affected"]
                assert found == expected, "reverse dependency distances differ"
                checks += 1
        actual["accuracy"] = {
            "components_equal": True,
            "impact_checks": checks,
            "impact_equal": True,
            "scope": "same resolved input facts; not independent compiler/runtime ground truth",
        }
        actual["python_kernel"], _ = measured(
            lambda: original_components(nodes, adjacency), repeats
        )
        actual["rust_resident_ipc"], _ = measured(
            lambda: adapter.request({"command": "components"}), repeats
        )
        actual["rust_graph_publish_and_ipc"], _ = measured(
            lambda: adapter.components(nodes, adjacency), repeats
        )
        native_kernel = []
        for _ in range(repeats):
            native_kernel.append(adapter.request({"command": "components"})["compute_ns"] / 1e6)
        actual["rust_parse_and_kernel_reported"] = summary(native_kernel)
        startup = []
        for _ in range(10):
            tick = time.perf_counter()
            child = NativeGraphAdapter(binary)
            try:
                assert normalized(child.components(nodes, adjacency)) == reference
            finally:
                child.close()
            startup.append((time.perf_counter() - tick) * 1000)
        actual["rust_new_process_graph_publish_and_ipc"] = summary(startup)

        def live_architecture():
            return backend.code_query(
                "actual",
                handle,
                "project_architecture",
                max_nodes=100,
                max_edges=200,
                max_chars=20_000,
            )

        live_times = {"python": [], "rust": []}
        golden = None
        for index in range(repeats):
            # Alternate ordering to reduce drift; use actual public defaults.
            for variant in ("python", "rust") if index % 2 == 0 else ("rust", "python"):
                queries._components = (
                    original_components if variant == "python" else adapter.components
                )
                tick = time.perf_counter()
                result = live_architecture()
                live_times[variant].append((time.perf_counter() - tick) * 1000)
                if golden is None:
                    golden = result
                assert result == golden, "guarded Live responses differ"
            if (index + 1) % 10 == 0:
                print(json.dumps({"dataset": source.name, "live_pairs": index + 1}), flush=True)
        actual["python_guarded_live"] = summary(live_times["python"])
        actual["rust_guarded_live"] = summary(live_times["rust"])
        actual["live_query_budget"] = {"max_nodes": 100, "max_edges": 200, "max_chars": 20_000}
        actual["accuracy"]["guarded_live_response_equal"] = True
        queries._components = original_components
        improvement = (
            1 - actual["rust_guarded_live"]["p95_ms"] / actual["python_guarded_live"]["p95_ms"]
        )
        actual["live_p95_improvement_ratio"] = improvement
        actual["passes_20_percent_live_gate"] = improvement >= 0.20
        (output / "result.json").write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(actual, ensure_ascii=False), flush=True)
        return actual
    finally:
        queries._components = original_components
        if adapter is not None:
            adapter.close()
        service.close()
        backend.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", type=Path, action="append", default=[])
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--worker", choices=("python", "rust-adapter"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    args.binary = args.binary.resolve(strict=True)
    if args.worker:
        memory_worker(args)
        return
    if args.output is None or not 30 <= args.repeats <= 200:
        parser.error("new --output and 30..200 repeats required")
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(REPO / ".artifacts"):
        parser.error("output must be a new directory below this repository's .artifacts")
    output.mkdir(mode=0o700, parents=True)
    report = {"scope": "local actual-source pilot; no web/model/UI benchmark", "datasets": []}
    for index, path in enumerate(args.source):
        report["datasets"].append(
            evaluate(
                path.resolve(strict=True), output / f"dataset-{index}", args.binary, args.repeats
            )
        )
    report["binary_bytes"] = args.binary.stat().st_size
    report["binary_sha256"] = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    report["production_backend_enabled"] = False
    report["adoption_gate"] = {
        "accuracy": "exact graph and guarded response equivalence",
        "performance": "at least 20% guarded Live p95 improvement on every tested dataset",
        "passed": bool(report["datasets"])
        and all(row["passes_20_percent_live_gate"] for row in report["datasets"]),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

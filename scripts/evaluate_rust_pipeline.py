"""Scale/batch experiments over saved real facts; never enables a live backend.

The common byte-oriented client is outside each server implementation. Measured
requests include transport, server JSON, source pre/post guards, computation,
response clipping and output. Saved facts/parsing are outside request timings.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import selectors
import statistics
import subprocess
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from rust_pipeline_reference import PublishedGraph

REPO = Path(__file__).resolve().parents[1]
MAX_FRAME = 64 * 1024 * 1024


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def summary(values):
    if not values:
        return None
    result = {
        "samples": len(values),
        "median_ms": statistics.median(values),
        "minimum_ms": min(values),
        "maximum_ms": max(values),
    }
    if len(values) >= 20:
        result["p95_ms"] = sorted(values)[math.ceil(len(values) * 0.95) - 1]
    return result


class Worker:
    """Both directions use a whole-frame deadline, including blocked writes."""

    def __init__(self, command, log):
        self.log = log.open("wb")
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log
        )
        self.selector = selectors.DefaultSelector()
        self.input_fd = self.process.stdin.fileno()
        self.output_fd = self.process.stdout.fileno()
        os.set_blocking(self.input_fd, False)
        os.set_blocking(self.output_fd, False)
        self.pending = bytearray()

    def request_bytes(self, payload, deadline_seconds=45):
        if len(payload) > MAX_FRAME:
            raise ValueError("private request budget")
        remaining = memoryview(payload)
        deadline = time.perf_counter() + deadline_seconds
        self.selector.register(self.input_fd, selectors.EVENT_WRITE)
        try:
            while remaining:
                timeout = deadline - time.perf_counter()
                if timeout <= 0 or not self.selector.select(timeout):
                    raise TimeoutError("worker whole-frame write deadline")
                try:
                    sent = os.write(self.input_fd, remaining)
                except BlockingIOError:
                    continue
                if sent <= 0:
                    raise EOFError("worker closed input")
                remaining = remaining[sent:]
        finally:
            self.selector.unregister(self.input_fd)
        self.selector.register(self.output_fd, selectors.EVENT_READ)
        try:
            while b"\n" not in self.pending:
                timeout = deadline - time.perf_counter()
                if timeout <= 0 or not self.selector.select(timeout):
                    raise TimeoutError("worker whole-frame read deadline")
                try:
                    block = os.read(self.output_fd, 65_536)
                except BlockingIOError:
                    continue
                if not block:
                    raise EOFError("worker closed output")
                self.pending.extend(block)
                if len(self.pending) > MAX_FRAME:
                    raise ValueError("private response budget")
            response, _, rest = self.pending.partition(b"\n")
            self.pending = bytearray(rest)
            return bytes(response)
        finally:
            self.selector.unregister(self.output_fd)

    def request(self, payload, **kwargs):
        return json.loads(self.request_bytes(encode(payload), **kwargs))

    def close(self):
        self.selector.close()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process.stdout.close()
        self.log.close()


def balanced_order(paths):
    """Round-robin the actual top-level source groups; never clone files."""
    groups = defaultdict(deque)
    for path in sorted(paths):
        parts = path.split("/")
        group = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
        groups[group].append(path)
    result = []
    names = deque(sorted(groups))
    while names:
        name = names.popleft()
        result.append(groups[name].popleft())
        if groups[name]:
            names.append(name)
    return result


def make_subset(source_snapshot, output, size):
    output.mkdir(parents=True, exist_ok=False)
    root = output / "source"
    root.mkdir()
    selected = set(balanced_order(source_snapshot["nodes"])[:size])
    require_size = len(selected)
    if require_size != size:
        raise ValueError("insufficient actual files for requested subset")
    source_root = Path(source_snapshot["source_root"])
    records = {item["path"]: item for item in source_snapshot["files"]}
    files = []
    for path in sorted(selected):
        item = records[path]
        origin = source_root / path
        if origin.is_symlink() or not origin.is_file():
            raise ValueError("source preparation requires saved ordinary files")
        raw = origin.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"] or len(raw) != item["size"]:
            raise ValueError("source preparation hash mismatch")
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        files.append({"path": path, "sha256": item["sha256"], "size": len(raw)})
    snapshot = {
        "version": 1,
        "source_root": str(root.resolve()),
        "nodes": sorted(selected),
        "edges": sorted(
            [
                edge
                for edge in source_snapshot["edges"]
                if edge["source"] in selected and edge["target"] in selected
            ],
            key=lambda edge: (edge["source"], edge["target"]),
        ),
        "files": files,
        "coverage": source_snapshot.get("coverage"),
        "provenance": source_snapshot.get("provenance"),
        "sampling": {
            "strategy": "top-two-directory stratified round-robin; induced subgraph",
            "real_file_count": len(files),
            "source_file_count": len(source_snapshot["nodes"]),
            "byte_identical_to_source": True,
            "source_was_not_executed": True,
            "limitations": (
                "Dependencies to files outside each subset are omitted, not external truth."
            ),
        },
    }
    (output / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    return snapshot


def command(variant, binary, snapshot, mode):
    arguments = ["--root", snapshot["source_root"], "--guard", mode]
    if variant == "rust":
        return [str(binary), *arguments]
    return [
        sys.executable,
        "-B",
        str(REPO / "scripts/rust_pipeline_reference.py"),
        *arguments,
        "--implementation",
        variant.removeprefix("python-"),
    ]


def architecture_queries(batch_size, *, expanded=False):
    variants = (
        {"max_nodes": 100, "max_edges": 200, "max_chars": 20_000},
        {"max_nodes": 100, "max_edges": 200, "max_chars": 12_000},
        {"max_nodes": 80, "max_edges": 150, "max_chars": 8000},
    )
    if expanded:
        variants = ({"max_nodes": 500, "max_edges": 1000, "max_chars": 50_000},)
    return [
        {"command": "architecture", **variants[index % len(variants)]}
        for index in range(batch_size)
    ]


def compare_responses(left, right):
    if left != right:
        raise AssertionError("whole pipeline semantic responses differ")


def run_case(
    snapshot,
    output,
    binary,
    mode,
    batches,
    repeats,
    legacy,
    expanded=False,
    patterns=("independent_requests", "one_transport_batch"),
):
    output.mkdir(parents=True, exist_ok=False)
    variants = ["python-indexed", "rust"]
    if legacy:
        variants.insert(0, "python-legacy")
    report = {
        "nodes": len(snapshot["nodes"]),
        "edges": len(snapshot["edges"]),
        "guard_files": len(snapshot["files"]),
        "guard_bytes": sum(item["size"] for item in snapshot["files"]),
        "guard_mode": mode,
        "graph_scope": "real files; actual resolved induced graph; source parsing before timing",
        "expanded_response_budget": expanded,
        "batch_semantics": "K requests; every request has its own pre/post guard",
        "variants": {},
        "batches": [],
    }
    workers = {}
    load_payload = encode({"command": "load", "snapshot": snapshot})
    try:
        for variant in variants:
            started = time.perf_counter()
            worker = Worker(command(variant, binary, snapshot, mode), output / f"{variant}.log")
            workers[variant] = worker
            ping = worker.request({"command": "ping"})
            if ping != {"protocol": 1, "scope": "fixed-snapshot-pipeline-pilot"}:
                raise AssertionError("private protocol identity differs")
            startup_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            loaded = json.loads(worker.request_bytes(load_payload))
            load_ms = (time.perf_counter() - started) * 1000
            compare_responses(
                loaded,
                {
                    "loaded": len(snapshot["nodes"]),
                    "edges": len(snapshot["edges"]),
                    "files": len(snapshot["files"]),
                },
            )
            report["variants"][variant] = {
                "startup_to_ping_ms": startup_ms,
                "publication_ms": load_ms,
                "publication_includes": (
                    "JSON input+graph+two source guards+reply; first hash cache cold"
                ),
            }
        # Validate complete outputs before timings. Goldens remain inspectable.
        queries = architecture_queries(3 if not expanded else 1, expanded=expanded)
        golden = None
        for variant in variants:
            responses = [workers[variant].request(query) for query in queries]
            if golden is None:
                golden = responses
                (output / "golden-responses.json").write_text(
                    json.dumps(golden, ensure_ascii=False, indent=2) + "\n"
                )
            else:
                compare_responses(responses, golden)
        for batch_size in batches:
            # Legacy's repeated JSON clipping can dominate large output. Keep its
            # representative single request; indexed and Rust share reuse rules.
            active = variants if batch_size == 1 else [v for v in variants if v != "python-legacy"]
            for pattern in patterns:
                batch_queries = architecture_queries(batch_size, expanded=expanded)
                payloads = [encode(query) for query in batch_queries]
                batch_payload = encode({"command": "batch", "queries": batch_queries})
                expected = [golden[index % len(golden)] for index in range(batch_size)]
                raw = {variant: [] for variant in active}
                per_request = {variant: [] for variant in active}
                cpu_before = {
                    variant: workers[variant].request({"command": "stats"}) for variant in active
                }
                for repeat in range(repeats):
                    order = active[repeat % len(active) :] + active[: repeat % len(active)]
                    for variant in order:
                        worker = workers[variant]
                        before = time.perf_counter()
                        if pattern == "independent_requests":
                            responses = []
                            for payload in payloads:
                                request_start = time.perf_counter()
                                responses.append(worker.request_bytes(payload))
                                per_request[variant].append(
                                    (time.perf_counter() - request_start) * 1000
                                )
                        else:
                            response = worker.request_bytes(batch_payload)
                        elapsed = (time.perf_counter() - before) * 1000
                        raw[variant].append(elapsed)
                        # Decode/assert outside the timed transport/service path.
                        if pattern == "independent_requests":
                            observed = [json.loads(value) for value in responses]
                        else:
                            decoded = json.loads(response)
                            if "error" in decoded:
                                raise AssertionError("pipeline rejected bounded actual requests")
                            observed = decoded["results"]
                        compare_responses(observed, expected)
                row = {"batch_size": batch_size, "pattern": pattern, "variants": {}}
                for variant in active:
                    usage = workers[variant].request({"command": "stats"})
                    cpu_us = sum(
                        usage[key] - cpu_before[variant][key]
                        for key in ("cpu_user_us", "cpu_system_us")
                    )
                    row["variants"][variant] = {
                        "batch_latency": summary(raw[variant]),
                        "request_latency": summary(per_request[variant]),
                        "median_requests_per_second": batch_size
                        * 1000
                        / statistics.median(raw[variant]),
                        "worker_cpu_ms_per_request": cpu_us / (repeats * batch_size * 1000),
                        "peak_worker_rss_bytes": usage["peak_rss_bytes"],
                        "raw_batch_ms": raw[variant],
                        "raw_request_ms": per_request[variant],
                    }
                row["rust_speedup_vs_indexed_python"] = statistics.median(
                    raw["python-indexed"]
                ) / statistics.median(raw["rust"])
                report["batches"].append(row)
                (output / "result.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                )
                print(
                    json.dumps(
                        {
                            "nodes": report["nodes"],
                            "mode": mode,
                            "batch": batch_size,
                            "pattern": pattern,
                            "speedup": row["rust_speedup_vs_indexed_python"],
                        }
                    ),
                    flush=True,
                )
        report["whole_response_equivalence"] = True
        for variant in variants:
            report["variants"][variant]["final_stats"] = workers[variant].request(
                {"command": "stats"}
            )
    finally:
        for worker in workers.values():
            worker.close()
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", default="1,8,32,128,512,1024,2048")
    parser.add_argument("--batches", default="1,8")
    parser.add_argument("--modes", default="metadata,strict")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--expanded", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(REPO / ".artifacts"):
        parser.error("new output below the current repository's .artifacts required")
    if not 1 <= args.repeats <= 100:
        parser.error("repeats must be 1..100")
    source_snapshot = json.loads(args.snapshot.read_text())
    sizes = sorted({int(size) for size in args.sizes.split(",")})
    modes = args.modes.split(",")
    batches = [int(value) for value in args.batches.split(",")]
    if any(size < 1 or size > len(source_snapshot["nodes"]) for size in sizes):
        parser.error("all sizes must fit the actual source")
    if set(modes) - {"metadata", "strict"} or any(not 1 <= value <= 1000 for value in batches):
        parser.error("invalid mode or batch")
    if not args.prepare_only and args.binary is None:
        parser.error("a locked-build private Rust binary required")
    output.mkdir(parents=True, mode=0o700)
    report = {
        "experiment": "complete fixed-snapshot read pipeline; no production installation",
        "source": str(args.snapshot.resolve()),
        "scope": (
            "shared already-parsed facts, private capability root; "
            "no registry/write/MCP frontend port"
        ),
        "measurement": (
            "common byte client; request encoding and response decoding/accuracy outside timing"
        ),
        "machine": {
            "platform": platform.platform(),
            "python": sys.version,
            "cpu_count": os.cpu_count(),
        },
        "preexisting_artifacts_retained": True,
        "datasets": [],
    }
    if args.binary:
        report["binary_bytes"] = args.binary.stat().st_size
        report["binary_sha256"] = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    for size in sizes:
        subset = make_subset(source_snapshot, output / f"corpus-{size}", size)
        # Validate preparation with the same Python graph contract before runs.
        PublishedGraph(copy.deepcopy(subset), "indexed")
        if not args.prepare_only:
            for mode in modes:
                report["datasets"].append(
                    run_case(
                        subset,
                        output / f"run-{size}-{mode}",
                        args.binary.resolve(strict=True),
                        mode,
                        batches,
                        args.repeats,
                        args.legacy,
                        args.expanded,
                    )
                )
                (output / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                )
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

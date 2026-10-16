"""Detach real code-graph facts from new private Live indexes for Rust trials.

This is offline evaluation preparation, not a production backend or a corpus
generator. Project modules and entrypoints are never imported or executed.
All source policy, source guards and bounded static-parser limitations remain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import time
from collections import Counter, defaultdict
from pathlib import Path

from code_context.intelligence_index import MAX_SNAPSHOT_PARSE_BYTES
from code_context.intelligence_queries import _file_edges
from code_context.live import LiveQueries
from code_context.live_index import (
    MAX_LIVE_DATABASE_BYTES,
    MAX_LIVE_FACTS,
    MAX_LIVE_INDEX_BYTES,
    LiveIndexService,
)
from code_context.policy import MAX_FILES
from code_context.source_access import SourceAccess

REPO = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO / ".artifacts" / "maintenance-20261016" / "rust-scale-evaluation"
CODE_SUFFIXES = frozenset({".py", ".pyi", ".java"})


def _sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _budgets(kind):
    budgets = {
        "max_bytes": MAX_LIVE_DATABASE_BYTES,
        "max_source_bytes": MAX_SNAPSHOT_PARSE_BYTES,
        "max_parse_bytes": MAX_SNAPSHOT_PARSE_BYTES,
        "max_index_bytes": MAX_LIVE_INDEX_BYTES,
        "max_facts": MAX_LIVE_FACTS,
        "max_files": MAX_FILES,
    }
    if kind == "expanded-private-evaluation":
        # Explicit per-instance settings only. Production defaults are unchanged.
        budgets.update(
            max_bytes=1024 * 1024 * 1024,
            max_source_bytes=128 * 1024 * 1024,
            max_parse_bytes=256 * 1024 * 1024,
            max_index_bytes=512 * 1024 * 1024,
            max_facts=2_000_000,
        )
    return budgets


def prepare(args):
    source_root = args.source.resolve(strict=True)
    output = args.output.resolve()
    if not source_root.is_relative_to(REPO / ".artifacts"):
        raise ValueError("evaluation corpora must already be inside this workspace's artifacts")
    if not output.is_relative_to(ARTIFACTS):
        raise ValueError("evaluation output must remain in rust-scale-evaluation")
    output.mkdir(parents=True, exist_ok=False)
    source = SourceAccess(source_root)
    inventory = source.manifest()
    inventory_code_paths = [
        item["path"] for item in inventory["files"] if Path(item["path"]).suffix in CODE_SUFFIXES
    ]
    budgets = _budgets(args.budget)
    (output / "request.json").write_text(
        json.dumps(
            {
                "source_root": str(source_root),
                "budget_profile": args.budget,
                "budgets": budgets,
                "discovered_code_files": len(inventory_code_paths),
                "discovered_total_files": len(inventory["files"]),
                "discovery_partial": inventory["partial"],
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "phase": "index_start",
                "source": source_root.name,
                "budget_profile": args.budget,
                "discovered_code_files": len(inventory_code_paths),
            }
        ),
        flush=True,
    )
    backend = LiveQueries({"actual-scale": source})
    service = LiveIndexService(output / "index", wait_seconds=600, **budgets)
    backend.index_service = service
    started = time.perf_counter()
    try:
        handle = backend.resolve_snapshot("actual-scale")[0]
        initial = backend.code_query(
            "actual-scale", handle, "symbol_search", query="a", limit=1, max_chars=5000
        )
        if initial.get("index_not_ready"):
            (output / "index-not-ready.json").write_text(json.dumps(initial, indent=2) + "\n")
            raise RuntimeError("private index did not produce a queryable publication")
        index_ms = (time.perf_counter() - started) * 1000
        with service._lock:
            stats = json.loads(service._cached("actual-scale")["stats"])
            indexed = [
                dict(row)
                for row in service._db.execute(
                    "SELECT c.path,c.language,c.status,c.diagnostics,f.sha256,f.size "
                    "FROM ci_files c JOIN files f ON c.project_id=f.project_id "
                    "AND c.revision=f.revision AND c.path=f.path "
                    "WHERE c.project_id=? AND c.language IN ('python','java') ORDER BY c.path",
                    ("actual-scale",),
                )
            ]
            edges = _file_edges(service._db, "actual-scale", 1)
            symbol_count = service._db.execute("SELECT COUNT(*) FROM ci_symbols").fetchone()[0]
            relation_count = service._db.execute("SELECT COUNT(*) FROM ci_relations").fetchone()[0]
        # Unobserved or policy-excluded bodies cannot become Rust read-path inputs.
        # Keep their names/status in coverage so the selection is never called full.
        observed = [item for item in indexed if item["sha256"] is not None]
        nodes = sorted(item["path"] for item in observed)
        node_set = set(nodes)
        selected_edges = [
            edge
            for (left, right), edge in sorted(edges.items())
            if left in node_set and right in node_set
        ]
        adjacency = defaultdict(list)
        for edge in selected_edges:
            if not edge["type_only"]:
                adjacency[edge["source"]].append(edge["target"])
        relation_evidence = sum(edge["evidence_count"] for edge in selected_edges)
        unknown_sources = [item for item in indexed if item["sha256"] is None]
        codes = Counter()
        for item in indexed:
            diagnostic = json.loads(item["diagnostics"])
            entries = diagnostic.get("items", []) if isinstance(diagnostic, dict) else diagnostic
            codes.update({entry["code"] for entry in entries if "code" in entry})
        archive = args.archive.resolve(strict=True) if args.archive else None
        provenance = {
            "repository": args.repository,
            "commit": args.commit,
            "tag": args.tag,
            "source_kind": "actual distinct upstream source files; no graph replication",
            "project_code_executed": False,
            "production_defaults_changed": False,
            "budget_profile": args.budget,
            "budgets": budgets,
            "archive_path": str(archive) if archive else None,
            "archive_sha256": _sha256(archive) if archive else None,
            "archive_bytes": archive.stat().st_size if archive else None,
            "same_fact_semantics": "existing Python/Java parser, conservative resolver; "
            "resolved local file edges only",
        }
        coverage = {
            "index_partial": initial["index_partial"],
            "index_coverage": initial.get("index_coverage"),
            "discovery_partial": inventory["partial"],
            "discovery_skipped": inventory["skipped"],
            "discovered_code_files": len(inventory_code_paths),
            "indexed_code_files": len(indexed),
            "observed_snapshot_files": len(observed),
            "files_by_language": dict(Counter(item["language"] for item in indexed)),
            "files_by_status": dict(Counter(item["status"] for item in indexed)),
            "snapshot_node_status": dict(Counter(item["status"] for item in observed)),
            "diagnostic_files_by_code": dict(sorted(codes.items())),
            "omitted_unobserved_files": [
                {"path": item["path"], "status": item["status"]} for item in unknown_sources
            ],
            "symbol_facts": symbol_count,
            "relation_facts": relation_count,
            "resolved_file_edges": len(selected_edges),
            "non_type_only_file_edges": sum(not edge["type_only"] for edge in selected_edges),
            "resolved_relation_evidence": relation_evidence,
            "parse_fact_count": stats.get("parse_fact_count"),
            "published_fact_count": stats.get("fact_count"),
            "source_bytes": stats.get("source_bytes"),
            "parse_payload_bytes": stats.get("parse_payload_bytes"),
            "index_payload_bytes": stats.get("index_payload_bytes"),
            "index_build_ms": round(index_ms, 3),
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "bounds_are_known": True,
            "accuracy_scope": "same resolved static input facts; not compiler/runtime ground truth",
        }
        snapshot = {
            "version": 1,
            "source_root": str(source_root),
            "nodes": nodes,
            "edges": selected_edges,
            "files": [
                {"path": item["path"], "sha256": item["sha256"], "size": item["size"]}
                for item in observed
            ],
            "coverage": coverage,
            "provenance": provenance,
            "inventory_code_paths": inventory_code_paths,
            "adjacency": dict(adjacency),
        }
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
        (output / "graph.json").write_bytes(payload)
        summary = {
            "graph_path": str(output / "graph.json"),
            "graph_sha256": hashlib.sha256(payload).hexdigest(),
            "graph_bytes": len(payload),
            "source_root": str(source_root),
            "provenance": provenance,
            "coverage": coverage,
        }
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps({"phase": "graph_ready", **summary}, ensure_ascii=False), flush=True)
    finally:
        service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--budget",
        choices=("production-defaults", "expanded-private-evaluation"),
        default="production-defaults",
    )
    prepare(parser.parse_args())


if __name__ == "__main__":
    main()

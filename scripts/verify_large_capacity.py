"""Retained, isolated Live capacity checks against real and synthetic sources.

Run with ``uv run python scripts/verify_large_capacity.py``. Every run creates
a new directory below this checkout's .artifacts and retains all source copies,
fixtures, derived databases and reports. It never opens an installed app's data.
The real-source checks use the same copied runtime for both budget settings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIB = 1024**2
STAT_KEYS = (
    "file_count",
    "files_by_status",
    "files_by_language",
    "index_diagnostics",
    "source_bytes",
    "parse_payload_bytes",
    "index_payload_bytes",
    "parse_fact_count",
    "fact_count",
    "metadata_payload_bytes",
    "discovery_partial",
    "build_ms",
    "partial",
)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_current_sources(destination):
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO).decode("utf-8").split("\0")
    )
    # New production modules in the current change must enter the copied runtime.
    names = sorted(
        set(filter(None, tracked))
        | {p.relative_to(REPO).as_posix() for p in (REPO / "src").rglob("*.py")}
    )
    manifest = []
    for name in names:
        source = REPO / name
        if source.is_symlink() or not source.is_file():
            continue
        if any(
            part in {".git", ".artifacts", ".code-context", "__pycache__"} for part in source.parts
        ):
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        raw = target.read_bytes()
        manifest.append(
            {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    return manifest


def worker_index(args):
    # Imports intentionally happen after the child PYTHONPATH selects its copied runtime.
    from code_context.live import LiveQueries
    from code_context.live_index import LiveIndexService
    from code_context.source_access import SourceAccess

    options = {"wait_seconds": 600, "manifest_seconds": 120}
    if args.worker == "real-legacy":
        options.update(
            max_bytes=500 * MIB,
            max_source_bytes=32 * MIB,
            max_parse_bytes=32 * MIB,
            max_index_bytes=48 * MIB,
            max_facts=200_000,
            max_files=50_000,
        )
    source = SourceAccess(args.source)
    backend = LiveQueries({"capacity": source})
    service = LiveIndexService(args.output / args.worker, **options)
    backend.index_service = service
    started = time.perf_counter()
    try:
        result = backend.code_query(
            "capacity",
            None,
            "symbol_search",
            query="__capacity_probe__",
            exact=True,
            max_chars=50_000,
        )
        if result.get("index_not_ready"):
            raise RuntimeError("CAPACITY_INDEX_NOT_READY")
        status = service.status("capacity")
        stats = status["stats"]
        with sqlite3.connect(service.path) as db:
            rows = db.execute(
                "SELECT path,status,diagnostics FROM ci_files ORDER BY path"
            ).fetchall()
            budget_paths, diagnostics = [], Counter()
            for path, _status, payload in rows:
                codes = {d["code"] for d in json.loads(payload).get("items", []) if "code" in d}
                diagnostics.update(codes)
                if any(code.startswith("PROJECT_") and "BUDGET" in code for code in codes):
                    budget_paths.append(path)
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            symbols = db.execute("SELECT count(*) FROM ci_symbols").fetchone()[0]
            relations = db.execute("SELECT count(*) FROM ci_relations").fetchone()[0]
        value = {
            "scenario": args.worker,
            "source": str(args.source),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "stats": {key: stats.get(key) for key in STAT_KEYS},
            "limits": status["limits"],
            "storage_bytes": service.path.stat().st_size,
            "symbols": symbols,
            "relations": relations,
            "budget_limited_files": len(budget_paths),
            "budget_limited_paths": budget_paths,
            "diagnostics": dict(sorted(diagnostics.items())),
            "integrity_check": integrity,
            "source_metrics": dict(source.metrics),
            "benchmark_overrides": {"wait_seconds": 600, "manifest_seconds": 120},
        }
    finally:
        backend.close()
    if args.worker == "real-default":
        reopened_source = SourceAccess(args.source)
        reopened = LiveQueries({"capacity": reopened_source})
        reopened.index_service = LiveIndexService(args.output / args.worker, **options)
        extraction_calls = []
        original_extract = reopened.index_service._extract

        def counted_extract(*parameters, **keywords):
            extraction_calls.append(True)
            return original_extract(*parameters, **keywords)

        reopened.index_service._extract = counted_extract
        started = time.perf_counter()
        try:
            result = reopened.code_query(
                "capacity",
                None,
                "symbol_search",
                query="__capacity_probe__",
                exact=True,
                max_chars=50_000,
            )
            assert not result.get("index_not_ready")
            value["reopen"] = {
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "source_body_reads": reopened_source.metrics["body_reads"],
                "extraction_calls": len(extraction_calls),
                "fact_publication_occurred": bool(reopened.index_service._last_publication),
                "fact_count": reopened.index_service.status("capacity")["stats"]["fact_count"],
            }
        finally:
            reopened.close()
    return value


def worker_discovery(args):
    from code_context.source_access import SourceAccess

    value = {"scenario": "discovery", "source": str(args.source)}
    for name, options in (("legacy", {"max_files": 50_000}), ("default", {})):
        source = SourceAccess(args.source)
        started = time.perf_counter()
        result = source.manifest(max_seconds=120, **options)
        value[name] = {
            "file_count": len(result["files"]),
            "partial": result["partial"],
            "directories": result["directories"],
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "body_reads": source.metrics["body_reads"],
        }
    return value


def run_worker(output, copied_runtime, name, source):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(copied_runtime / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        name,
        "--source",
        str(source),
        "--output",
        str(output),
    ]
    completed = subprocess.run(command, cwd=copied_runtime, env=env, capture_output=True, text=True)
    (output / f"{name}.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    (output / f"{name}.stdout.json").write_text(completed.stdout, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(f"CAPACITY_SCENARIO_FAILED: {name}; retained stderr in output")
    return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--worker", choices=("real-legacy", "real-default", "synthetic-default", "discovery")
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        if args.source is None or args.output is None:
            parser.error("worker requires its retained source and output")
        output = args.output.resolve()
        if (
            args.output.is_symlink()
            or output.parent != (REPO / ".artifacts").resolve()
            or not output.name.startswith("capacity-")
            or not args.source.resolve().is_relative_to(output)
        ):
            parser.error("worker source and output must belong to this run's artifact directory")
        value = worker_discovery(args) if args.worker == "discovery" else worker_index(args)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        value["peak_rss_bytes"] = rss if sys.platform == "darwin" else rss * 1024
        print(json.dumps(value, ensure_ascii=False))
        return
    if args.source or args.output:
        parser.error("the normal runner creates its own isolated output; no source paths accepted")
    artifact_root = REPO / ".artifacts"
    if artifact_root.is_symlink():
        raise RuntimeError("CAPACITY_UNSAFE_OUTPUT")
    artifact_root.mkdir(exist_ok=True)
    output = artifact_root / (
        "capacity-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
    )
    output.mkdir()
    runtime = output / "source-copy"
    manifest = copy_current_sources(runtime)
    dump(output / "source-manifest.json", manifest)
    print(f"Retained output: {output}", flush=True)
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "runtime_python": sys.version,
        "platform": platform.platform(),
        "source_copy_files": len(manifest),
        "source_manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
        "synthetic_scope": "300 Python files, 200 functions/file; "
        "discovery is separate, 50001 empty files",
        "scenarios": {},
    }
    for name in ("real-legacy", "real-default"):
        print(f"Running {name}", flush=True)
        report["scenarios"][name] = run_worker(output, runtime, name, runtime)
        dump(output / "report.json", report)
    synthetic = output / "synthetic-source"
    synthetic.mkdir()
    text = "".join(
        f"def function_{i}(value):\n    result = value + {i}\n    return result\n"
        for i in range(200)
    )
    for index in range(300):
        (synthetic / f"module_{index:04}.py").write_text(text, encoding="utf-8")
    print("Running synthetic-default", flush=True)
    report["scenarios"]["synthetic-default"] = run_worker(
        output, runtime, "synthetic-default", synthetic
    )
    dump(output / "report.json", report)
    discovery = output / "discovery-source"
    discovery.mkdir()
    for index in range(50_001):
        parent = discovery / f"group_{index // 1000:03}"
        parent.mkdir(exist_ok=True)
        (parent / f"file_{index:06}.py").touch()
    print("Running discovery", flush=True)
    report["scenarios"]["discovery"] = run_worker(output, runtime, "discovery", discovery)
    old = report["scenarios"]["real-legacy"]
    new = report["scenarios"]["real-default"]
    recovered = sorted(set(old["budget_limited_paths"]) - set(new["budget_limited_paths"]))
    report["real_budget_recovery"] = {"recovered_files": len(recovered), "paths": recovered}
    checks = {
        "real_default_has_no_project_budget_limited_files": new["budget_limited_files"] == 0,
        "real_old_budget_limit_reproduced_and_recovered": old["budget_limited_files"] > 0
        and len(recovered) == old["budget_limited_files"],
        "real_restart_reuses_facts": new["reopen"]["fact_count"] == new["stats"]["fact_count"]
        and new["reopen"]["extraction_calls"] == 0
        and not new["reopen"]["fact_publication_occurred"],
        "synthetic_retains_over_200k_facts": report["scenarios"]["synthetic-default"]["stats"][
            "fact_count"
        ]
        > 200_000,
        "synthetic_has_no_project_budget_limited_files": report["scenarios"]["synthetic-default"][
            "budget_limited_files"
        ]
        == 0,
        "discovery_passes_old_50k_limit": report["scenarios"]["discovery"]["default"]["file_count"]
        == 50_001
        and not report["scenarios"]["discovery"]["default"]["partial"],
        "legacy_discovery_stops_at_50k": report["scenarios"]["discovery"]["legacy"]["file_count"]
        == 50_000
        and report["scenarios"]["discovery"]["legacy"]["partial"],
        "discovery_never_reads_source_bodies": all(
            report["scenarios"]["discovery"][key]["body_reads"] == 0
            for key in ("legacy", "default")
        ),
        "indexed_databases_pass_integrity": all(
            report["scenarios"][key]["integrity_check"] == "ok"
            for key in ("real-legacy", "real-default", "synthetic-default")
        ),
    }
    report["checks"] = checks
    report["passed"] = all(checks.values())
    dump(output / "report.json", report)
    lines = [
        "# Live capacity verification",
        "",
        f"Output: `{output.name}`",
        "",
        "| Scenario | Files | Facts | Limited files | Time (s) | Peak RSS (MiB) | DB (MiB) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ("real-legacy", "real-default", "synthetic-default"):
        item = report["scenarios"][key]
        lines.append(
            f"| {key} | {item['stats']['file_count']} | {item['stats']['fact_count']} | "
            f"{item['budget_limited_files']} | {item['elapsed_seconds']} | "
            f"{item['peak_rss_bytes'] / MIB:.1f} | {item['storage_bytes'] / MIB:.1f} |"
        )
    lines.extend(
        [
            "",
            "Discovery: " + json.dumps(report["scenarios"]["discovery"], ensure_ascii=False),
            "",
            "Checks:",
            "",
        ]
    )
    lines.extend(f"- {'PASS' if passed else 'FAIL'}: {name}" for name, passed in checks.items())
    lines.extend(
        [
            "",
            "These are bounded local checks, not verification of the configured maximum, "
            "a public deployment, or a ChatGPT web session. The benchmark overrides build wait "
            "to 600 seconds and discovery timeout to 120 seconds; runtime defaults are "
            "reported separately.",
            "",
        ]
    )
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {"output": str(output), "passed": report["passed"], "checks": checks},
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""One bounded synthetic live-read benchmark; retain fixtures and all reports.

Run from this checkout with UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 and
``uv run --no-sync --frozen python scripts/benchmark_live.py``. No user-selected
source root is accepted. An explicit output must be an empty, real directory
directly below this checkout's .artifacts, named live-benchmark.<random suffix>.
Keep uv's TMPDIR separate from an explicit output: uv may leave a lock file,
which must trigger the nonempty-output guard rather than be overwritten.
"""

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import resource
import secrets
import sqlite3
import stat
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
DATABASE_BYTES = 64 * MIB
MANAGED_PEAK_BYTES = 128 * MIB
DEFAULT_DEFINITIONS_PER_FILE = 50
MAX_FIXTURE_ENTRIES = 6000
LARGE_BYTES = 3 * MIB
LARGE_LINE = ("Synthetic pagination fixture. " + "abcdefghij" * 24)[:255] + "\n"
PRUNED_DIRS = (
    "node_modules",
    ".venv",
    "target",
    "build",
    ".artifacts",
    ".code-context",
    "generated_ignore",
    "scratch_ignore",
)
TABLES = ("li_projects", "files", "li_parse_cache", "ci_files", "ci_symbols", "ci_relations")
STAT_KEYS = (
    "partial",
    "supported_languages",
    "files_by_language",
    "files_by_status",
    "parsed_files",
    "reused_parse_files",
    "resolved_files",
    "reused_relation_files",
    "source_bytes",
    "parse_payload_bytes",
    "index_payload_bytes",
    "parse_fact_count",
    "fact_count",
    "file_count",
    "metadata_payload_bytes",
    "discovery_partial",
    "build_ms",
)
PROVENANCE_FILES = (
    "scripts/benchmark_live.py",
    "src/code_context/live.py",
    "src/code_context/live_index.py",
    "src/code_context/live_watch.py",
    "src/code_context/source_access.py",
    "src/code_context/fingerprint_cache.py",
    "src/code_context/scanner.py",
    "src/code_context/project_registry.py",
    "src/code_context/read_context.py",
    "src/code_context/policy.py",
    "src/code_context/intelligence_queries.py",
    "src/code_context/intelligence_python.py",
    "src/code_context/intelligence_java.py",
    "src/code_context/intelligence_resolver.py",
    "src/code_context/intelligence_roots.py",
    "src/code_context/workspace.py",
    "uv.lock",
)


class BenchmarkError(RuntimeError):
    pass


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse's default error may echo an arbitrary path or input token.
        super().error("invalid benchmark arguments; use --help")


def bounded_int(minimum, maximum):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("integer required") from None
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError("outside benchmark bounds")
        return number

    return parse


def safe_error(exc):
    match = re.match(r"([A-Z][A-Z0-9_]{0,63}):", str(exc))
    return {"type": type(exc).__name__, "code": match[1] if match else "BENCHMARK_FAILED"}


def real_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BenchmarkError("UNSAFE_OUTPUT: real owned directory required")


def output_path(value, *, worker=False):
    parent = REPO / ".artifacts"
    if not parent.exists():
        if worker:
            raise BenchmarkError("INVALID_WORKER: missing fixture")
        parent.mkdir(mode=0o700)
    real_directory(parent)
    if value is None:
        for _ in range(10):
            candidate = parent / ("live-benchmark." + secrets.token_hex(3))
            try:
                candidate.mkdir(mode=0o700)
                return candidate
            except FileExistsError:
                continue
        raise BenchmarkError("OUTPUT_COLLISION: cannot allocate a new directory")
    candidate = Path(os.path.abspath(REPO / value))
    if candidate.parent != parent or not re.fullmatch(
        r"live-benchmark\.[A-Za-z0-9_-]{6,12}", candidate.name
    ):
        raise BenchmarkError("INVALID_OUTPUT: use a new benchmark directory in .artifacts")
    if not candidate.exists() and not worker:
        candidate.mkdir(mode=0o700)
    real_directory(candidate)
    if not worker and any(candidate.iterdir()):
        raise BenchmarkError("OUTPUT_NOT_EMPTY: refusing to overwrite existing output")
    if not worker:
        os.chmod(candidate, 0o700)
    return candidate


def write_json(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if str(REPO) in text or re.search(r"/(?:Users|home|private|tmp|var)/", text):
        raise BenchmarkError("UNSAFE_REPORT: absolute private path in report")
    with path.open("w", encoding="utf-8") as stream:
        stream.write(text)


def provenance():
    hashes = {
        name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
        for name in PROVENANCE_FILES
        if (REPO / name).is_file()
    }
    versions = {}
    for name in ("watchfiles", "tree-sitter", "tree-sitter-java", "pathspec"):
        try:
            version = importlib.metadata.version(name)
            versions[name] = version if re.fullmatch(r"[\w.+-]{1,60}", version) else "unknown"
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=3
    ).stdout.strip()
    return {
        "git_head": head if re.fullmatch(r"[a-f0-9]{40,64}", head) else None,
        "source_sha256": hashes,
        "python": platform.python_version(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "package_versions": versions,
    }


def make_fixture(output, definitions, repeats, definitions_per_file=DEFAULT_DEFINITIONS_PER_FILE):
    started = time.perf_counter()
    workspace = output / "workspace"
    workspace.mkdir(mode=0o700)
    counts = {}
    for label, total in (("A", definitions), ("B", 100)):
        root = workspace / label
        package = root / "pkg"
        package.mkdir(parents=True, mode=0o700)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "synthetic-{label.lower()}"\nversion = "0.0.0"\n',
            encoding="utf-8",
        )
        (root / ".gitignore").write_text("generated_ignore/\n", encoding="utf-8")
        (root / ".codecontextignore").write_text("scratch_ignore/\n", encoding="utf-8")
        (package / "__init__.py").write_text("# Synthetic package, not enterprise code.\n")
        for file_number, first in enumerate(range(0, total, definitions_per_file)):
            text = "# Synthetic independent definitions; no provider or project execution.\n"
            text += "".join(
                f"def {label.lower()}_func_{number:05d}(value=0):\n    return value + {number}\n\n"
                for number in range(first, min(total, first + definitions_per_file))
            )
            (package / f"module_{file_number:03d}.py").write_text(text, encoding="utf-8")
        for name in PRUNED_DIRS:
            directory = root / name / "fixture_dependency" / "nested"
            directory.mkdir(parents=True, mode=0o700)
            (directory / "pyproject.toml").write_text('[project]\nname="excluded-fixture"\n')
            (directory / "ignored.py").write_text("def excluded_fixture():\n    return 0\n")
        counts[label] = {
            "python_definitions": total,
            "python_definition_files": math.ceil(total / definitions_per_file),
            "package_marker_files": 1,
            "java_files": 3 if label == "A" else 0,
            "java_classes": 3 if label == "A" else 0,
            "java_methods": 6 if label == "A" else 0,
            "excluded_fixture_subtrees": len(PRUNED_DIRS),
        }
    java = workspace / "A" / "java" / "bench"
    java.mkdir(parents=True, mode=0o700)
    for number in range(3):
        (java / f"Example{number}.java").write_text(
            f"package bench; public class Example{number} {{\n"
            f"  public int convert(int value) {{ return value + {number}; }}\n"
            "  public int run(int value) { return convert(value); }\n}\n",
            encoding="utf-8",
        )
    with (workspace / "A" / "large.txt").open("w", encoding="utf-8", newline="\n") as stream:
        for _ in range(3):
            stream.write(LARGE_LINE * (MIB // len(LARGE_LINE)))
    fixture = {
        "schema": 1,
        "generator": "bounded-synthetic-live-v1",
        "synthetic": True,
        "fixture_root": "workspace",
        "projects": counts,
        "definitions_per_file": definitions_per_file,
        "large_text_bytes": LARGE_BYTES,
        "repeat_requests": repeats,
        "generation_ms": round((time.perf_counter() - started) * 1000, 3),
        "generated_file_bytes": sum(p.stat().st_size for p in workspace.rglob("*") if p.is_file()),
    }
    (output / "tmp").mkdir(mode=0o700)
    (output / "state").mkdir(mode=0o700)
    write_json(output / "fixture.json", fixture)
    return fixture


def check_fixture(output):
    marker = output / "fixture.json"
    if not stat.S_ISREG(marker.lstat().st_mode) or marker.stat().st_size > 16_384:
        raise BenchmarkError("INVALID_WORKER: expected a bounded fixture marker")
    fixture = json.loads(marker.read_text(encoding="utf-8"))
    if (
        fixture.get("generator") != "bounded-synthetic-live-v1"
        or fixture.get("schema") != 1
        or fixture.get("synthetic") is not True
        or fixture.get("fixture_root") != "workspace"
        or type(fixture.get("definitions_per_file")) is not int
        or fixture["definitions_per_file"] not in (1, 50)
        or not 1000 <= fixture["projects"]["A"]["python_definitions"] <= 5000
        or fixture["projects"]["B"]["python_definitions"] != 100
        or not 1 <= fixture["repeat_requests"] <= 5
    ):
        raise BenchmarkError("INVALID_WORKER: unrecognized synthetic fixture")
    # No user roots, symlinks or unbounded pre-existing directory trees accepted.
    seen = 0
    for directory, directories, files in os.walk(output, followlinks=False):
        for name in (*directories, *files):
            path = Path(directory) / name
            info = path.lstat()
            seen += 1
            if seen > MAX_FIXTURE_ENTRIES or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise BenchmarkError("UNSAFE_FIXTURE: unexpected entry or fixture size")
    for relative in ("workspace", "workspace/A", "workspace/B", "state", "tmp"):
        real_directory(output / relative)
    return fixture


class ReadLedger:
    """Count decoded text returned by Scanner, including real root-ignore reads.

    This is not physical disk I/O: OS cache, interpreter loading and SQLite I/O
    are outside the counters. SourceAccess.metrics alone misses ignore loading.
    """

    def __init__(self, workspace):
        from code_context.scanner import Scanner

        self.scanner = Scanner
        self.original = Scanner._read_text
        self.lock = threading.Lock()
        self.roots = {workspace / label: label for label in ("A", "B")}
        self.values = {label: self.empty() for label in ("A", "B", "other")}

        def observed(scanner, parent, name, path):
            label = self.roots.get(scanner.root, "other")
            category = (
                "root_ignore"
                if path in {".gitignore", ".codecontextignore"}
                else "code"
                if Path(path).suffix in {".py", ".pyi", ".java"}
                else "other_text"
            )
            with self.lock:
                self.values[label][category + "_attempts"] += 1
            content, problem = self.original(scanner, parent, name, path)
            if content is not None and problem is None:
                with self.lock:
                    self.values[label][category + "_reads"] += 1
                    self.values[label][category + "_bytes"] += len(content.encode("utf-8"))
            return content, problem

        Scanner._read_text = observed

    @staticmethod
    def empty():
        return {
            category + "_" + metric: 0
            for category in ("root_ignore", "code", "other_text")
            for metric in ("attempts", "reads", "bytes")
        }

    def snapshot(self):
        with self.lock:
            return {label: dict(value) for label, value in self.values.items()}

    def close(self):
        self.scanner._read_text = self.original


def delta(after, before):
    return {
        label: {key: value - before[label][key] for key, value in counts.items()}
        for label, counts in after.items()
    }


def process_resources():
    result = {"pid": os.getpid(), "current_rss_bytes": None, "process_fd_count": None}
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result["process_peak_rss_bytes"] = (
        int(usage.ru_maxrss) if sys.platform == "darwin" else int(usage.ru_maxrss * 1024)
    )
    result["peak_rss_method"] = "getrusage process lifetime high-water mark"
    try:
        if sys.platform.startswith("linux"):
            resident = int(Path("/proc/self/statm").read_text().split()[1])
            result["current_rss_bytes"] = resident * os.sysconf("SC_PAGE_SIZE")
            result["rss_method"] = "proc statm resident pages"
            result["process_fd_count"] = len(list(Path("/proc/self/fd").iterdir()))
            result["fd_method"] = "proc fd entries; enumeration may include one transient fd"
        else:
            rss = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if rss.returncode == 0 and rss.stdout.strip().isdigit():
                result["current_rss_bytes"] = int(rss.stdout.strip()) * 1024
                result["rss_method"] = "ps resident set KiB; sampled, not a memory cap"
            fds = subprocess.run(
                ["lsof", "-nP", "-a", "-p", str(os.getpid()), "-Ff"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if fds.returncode == 0:
                result["process_fd_count"] = len(
                    {line for line in fds.stdout.splitlines() if re.fullmatch(r"f\d+", line)}
                )
                result["fd_method"] = "lsof numeric process descriptors; includes probe pipes"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result["probe_partial"] = True
    result["kernel_watch_resource_count"] = None
    result["kernel_watch_resource_reason"] = "not instrumented; directories and process FDs differ"
    return result


def storage(path, project_ids):
    result = {"database_file_bytes": path.stat().st_size, "project_rows": {}}
    result["database_allocated_bytes"] = getattr(path.stat(), "st_blocks", 0) * 512
    result["sidecar_file_bytes"] = {
        suffix: candidate.stat().st_size if candidate.exists() else 0
        for suffix in ("-journal", "-wal", "-shm")
        for candidate in (Path(str(path) + suffix),)
    }
    with sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True) as db:
        result["sqlite_page_count"] = db.execute("PRAGMA page_count").fetchone()[0]
        result["sqlite_page_size"] = db.execute("PRAGMA page_size").fetchone()[0]
        for label, project_id in project_ids.items():
            result["project_rows"][label] = {
                table: db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?", (project_id,)
                ).fetchone()[0]
                for table in TABLES
            }
        result["raw_content_columns"] = [
            table
            for table in TABLES
            if any(row[1] == "content" for row in db.execute(f"PRAGMA table_info({table})"))
        ]
    return result


def index_status(service, project_id):
    value = service.status(project_id)
    return {
        "status": value["status"],
        "reason": value["reason"],
        "index_partial": value["index_partial"],
        "requires_validation": value["requires_validation"],
        "stats": {key: value["stats"][key] for key in STAT_KEYS if key in value["stats"]},
        "limits": value["limits"],
    }


def query_summary(value):
    return {
        "index_not_ready": bool(value.get("index_not_ready")),
        "index_partial": value.get("index_partial"),
        "reason": value.get("reason"),
        "total": value.get("total"),
        "returned_symbols": len(value.get("symbols", [])),
    }


class Measurements:
    def __init__(self, output, phase, ledger):
        self.output = output
        self.ledger = ledger
        self.report = {"phase": phase, "steps": {}, "resources": {}, "checks": {}}

    def save(self):
        write_json(self.output / ("phase-" + self.report["phase"] + ".json"), self.report)

    def measure(self, name, function, summarize=lambda _value: {}):
        before = self.ledger.snapshot()
        started, cpu = time.perf_counter(), time.process_time()
        try:
            value = function()
        except Exception as exc:
            self.report["steps"][name] = {
                "ok": False,
                "wall_ms": round((time.perf_counter() - started) * 1000, 3),
                "cpu_ms": round((time.process_time() - cpu) * 1000, 3),
                "error": safe_error(exc),
                "text_read_delta": delta(self.ledger.snapshot(), before),
            }
            self.save()
            raise
        self.report["steps"][name] = {
            "ok": True,
            "wall_ms": round((time.perf_counter() - started) * 1000, 3),
            "cpu_ms": round((time.process_time() - cpu) * 1000, 3),
            "text_read_delta": delta(self.ledger.snapshot(), before),
            **summarize(value),
        }
        self.save()
        return value

    def sample(self, name):
        self.report["resources"][name] = process_resources()
        self.save()


class NativeWatchProbe:
    def __init__(self, service, project_ids):
        self.service, self.project_ids = service, project_ids
        self.lock = threading.Lock()
        self.values = {"sessions": 0, "iterations": 0, "event_batches": 0}
        self.invalidations = {label: 0 for label in project_ids}
        self.ready = threading.Event()

    def invalidate(self, project_id):
        self.service.invalidate(project_id)
        with self.lock:
            for label, value in self.project_ids.items():
                if project_id == value:
                    self.invalidations[label] += 1

    def watch(self, *paths, **parameters):
        from watchfiles import watch

        with self.lock:
            self.values.update(
                sessions=self.values["sessions"] + 1,
                requested_paths=len(paths),
                requested_recursive=parameters.get("recursive"),
            )
        for changes in watch(*paths, **parameters):
            with self.lock:
                self.values["iterations"] += 1
                self.values["event_batches"] += bool(changes)
            self.ready.set()
            yield changes

    def snapshot(self):
        with self.lock:
            return {**self.values, "invalidations": dict(self.invalidations)}


def watch_summary(watcher, probe, sources):
    with watcher.lock:
        paths = {
            label: [p.relative_to(sources[label].root).as_posix() for p in item["directories"]]
            for label, project_id in probe.project_ids.items()
            if (item := watcher.metadata.get(project_id)) is not None
        }
    included = {
        label: [path for path in values if Path(path).parts and Path(path).parts[0] in PRUNED_DIRS]
        for label, values in paths.items()
    }
    status = watcher.status()
    return {
        "state": status["state"],
        "failure": status["failure"],
        "nonrecursive_directory_count": status["directories"],
        "directory_limit": status["directory_limit"],
        "partial_project_count": len(status["partial_projects"]),
        "event_overflows": status["event_overflows"],
        "directory_paths": paths,
        "excluded_subtree_names": list(PRUNED_DIRS),
        "excluded_subtrees_in_watch_set": included,
        "recursive": status["recursive"],
        "native_probe": probe.snapshot(),
        "polling_env_present": "WATCHFILES_FORCE_POLLING" in os.environ,
        "kernel_watch_resources_measured": False,
    }


def phase_worker(output, phase):
    if (output / "performance.json").exists() or (output / f"phase-{phase}.json").exists():
        raise BenchmarkError("OUTPUT_NOT_EMPTY: this phase has already been run")
    fixture = check_fixture(output)
    from code_context.live import LiveQueries
    from code_context.live_index import LiveIndexService
    from code_context.live_watch import WatchCoordinator
    from code_context.project_registry import ProjectRegistry

    workspace = output / "workspace"
    ledger = ReadLedger(workspace)
    measurements = Measurements(output, phase, ledger)
    report = measurements.report
    report["provenance"] = provenance()
    backend = watcher = service = None
    ids, sources = {}, {}
    try:
        measurements.sample("before_components")
        registry = measurements.measure("registry_setup", lambda: ProjectRegistry(workspace))
        discovery = measurements.measure(
            "discovery",
            registry.discover,
            lambda value: {
                "candidate_count": len(value["candidates"]),
                "partial": value["partial"],
                "reason": value["reason"],
                "directories": value["directories"],
                "initially_enabled_count": sum(row["enabled"] for row in value["candidates"]),
            },
        )
        for label in ("A", "B"):
            ids[label] = registry.register(label, display_name=label, enabled=True)
            sources[label] = registry.source(ids[label])
        backend = LiveQueries(registry=registry)
        service = measurements.measure(
            "index_service_setup",
            lambda: LiveIndexService(
                output / "state", max_bytes=DATABASE_BYTES, max_peak_bytes=MANAGED_PEAK_BYTES
            ),
        )
        backend.index_service = service
        report["fingerprint_cache_before"] = backend.fingerprint_cache.stats()
        report["configured_limits"] = index_status(service, ids["A"])["limits"]
        report["structure_wait_seconds"] = service.wait_seconds
        report["a_index_before_query"] = index_status(service, ids["A"])
        measurements.sample("before_watch")
        probe = NativeWatchProbe(service, ids)
        watcher = WatchCoordinator(backend.sources, probe.invalidate, watch_function=probe.watch)
        backend.watcher = watcher
        measurements.measure("watch_metadata_reconcile", watcher.reconcile)
        measurements.measure(
            "watch_start_and_idle",
            lambda: (watcher.start(), probe.ready.wait(3))[1],
            lambda ready: {"first_native_iteration_observed": ready},
        )
        report["watch_idle"] = watch_summary(watcher, probe, sources)
        measurements.sample("idle_watch")
        overview = measurements.measure(
            "a_overview",
            lambda: backend.repo_overview(ids["A"], None),
            lambda value: {
                "file_count": value["file_count"],
                "discovery_partial": value["discovery_partial"],
                "returned_files": len(value["files"]),
            },
        )
        report["storage_before_query"] = storage(service.path, ids)
        before_query = ledger.snapshot()
        report["checks"]["discovery_and_overview_no_source_text_reads"] = all(
            before_query[label]["code_reads"] == before_query[label]["other_text_reads"] == 0
            for label in ("A", "B")
        )
        report["checks"]["two_disabled_candidates_initially"] = (
            len(discovery["candidates"]) == 2
            and not discovery["partial"]
            and not any(row["enabled"] for row in discovery["candidates"])
        )
        report["checks"]["dependencies_pruned_from_watch_set"] = (
            len(report["watch_idle"]["directory_paths"]) == 2
            and report["watch_idle"]["nonrecursive_directory_count"] > 0
            and not report["watch_idle"]["partial_project_count"]
            and not any(report["watch_idle"]["excluded_subtrees_in_watch_set"].values())
        )
        report["checks"]["nonrecursive_native_iteration_observed"] = (
            probe.ready.is_set()
            and probe.snapshot().get("requested_recursive") is False
            and report["watch_idle"]["state"] == "watching"
        )
        report["checks"]["dependencies_pruned_from_overview"] = not any(
            Path(item["path"]).parts[0] in PRUNED_DIRS for item in overview["files"]
        )

        def query(name, selection=None):
            return backend.code_query(
                ids["A"], selection, "symbol_search", query=name, exact=True, limit=5
            )

        if phase == "cold":
            first = measurements.measure(
                "first_a_structure",
                lambda: query("a_func_00000", overview["snapshot"]),
                query_summary,
            )
            report["a_index_first"] = index_status(service, ids["A"])
            report["storage_first"] = storage(service.path, ids)
            measurements.sample("after_first_structure")
            report["checks"]["first_query_ready_and_exact"] = (
                not first.get("index_not_ready")
                and not first.get("index_partial")
                and first.get("total") == 1
            )
            repeated = []
            for number in range(fixture["repeat_requests"]):
                result = measurements.measure(
                    f"repeat_a_structure_{number + 1}",
                    lambda: query("a_func_00000", first["snapshot"]),
                    query_summary,
                )
                repeated.append(not result.get("index_not_ready") and result.get("total") == 1)
            report["checks"]["repeat_queries_ready_and_exact"] = all(repeated)
            report["fingerprint_cache_entries_after_repeats"] = {
                label: len(source._fingerprints) for label, source in sources.items()
            }
            report["fingerprint_cache_after_repeats"] = backend.fingerprint_cache.stats()
            measurements.sample("after_repeats")

            def page_summary(value):
                return {
                    "source_size_bytes": value["size"],
                    "returned_chars": len(value["content"]),
                    "has_more": value["has_more"],
                    "next_start_line": value["next_start_line"],
                    "next_char_offset": value["next_char_offset"],
                }

            page_one = measurements.measure(
                "large_file_page_one",
                lambda: backend.read_file(ids["A"], "large.txt", max_chars=20000, end_line=1000),
                page_summary,
            )
            page_two = measurements.measure(
                "large_file_page_two",
                lambda: backend.read_file(
                    ids["A"],
                    "large.txt",
                    page_one["snapshot"],
                    start_line=page_one["next_start_line"],
                    end_line=page_one["next_start_line"] + 999,
                    char_offset=page_one["next_char_offset"],
                    max_chars=20000,
                ),
                page_summary,
            )
            expected = (LARGE_LINE * math.ceil(40000 / len(LARGE_LINE)))[:40000]
            report["checks"]["large_file_exact_bounded_contiguous_pages"] = (
                page_one["size"] == LARGE_BYTES
                and page_one["content"] + page_two["content"] == expected
                and len(page_one["content"]) == len(page_two["content"]) == 20000
            )
            del page_one, page_two, expected
            measurements.sample("after_large_file_pages")
            prior = probe.snapshot()
            with (workspace / "A" / "pkg" / "module_000.py").open("a", encoding="utf-8") as stream:
                stream.write("\ndef changed_once(value=0):\n    return value + 1\n")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                now = probe.snapshot()
                if (
                    now["invalidations"]["A"] > prior["invalidations"]["A"]
                    and now["sessions"] > prior["sessions"]
                ):
                    break
                threading.Event().wait(0.02)
            now = probe.snapshot()
            report["live_edit_watch_event"] = {
                "a_invalidations": now["invalidations"]["A"] - prior["invalidations"]["A"],
                "b_invalidations": now["invalidations"]["B"] - prior["invalidations"]["B"],
                "native_event_batches": now["event_batches"] - prior["event_batches"],
            }

            def old_context():
                try:
                    backend.resolve_snapshot(ids["A"], first["snapshot"])
                except Exception as exc:
                    return {"rejected": True, "error": safe_error(exc)}
                return {"rejected": False}

            rejected = measurements.measure("old_context_after_edit", old_context, lambda v: v)
            changed = measurements.measure(
                "a_single_file_rebuild", lambda: query("changed_once"), query_summary
            )
            report["a_index_after_edit"] = index_status(service, ids["A"])
            report["checks"]["old_context_rejected"] = (
                rejected["rejected"]
                and rejected.get("error", {}).get("code") == "LIVE_CONTEXT_INVALID"
            )
            report["checks"]["new_context_detects_live_edit"] = (
                not changed.get("index_not_ready")
                and changed.get("total") == 1
                and changed["snapshot"] != first["snapshot"]
            )
            report["checks"]["only_one_file_reparsed_live_edit"] = (
                report["a_index_after_edit"]["stats"].get("parsed_files") == 1
            )
        else:
            report["checks"]["restart_cache_requires_validation"] = (
                report["a_index_before_query"]["status"] == "cached"
                and report["a_index_before_query"]["requires_validation"]
            )
            result = measurements.measure(
                "restart_a_structure", lambda: query("changed_while_stopped"), query_summary
            )
            report["a_index_after_restart"] = index_status(service, ids["A"])
            report["checks"]["offline_edit_detected"] = (
                not result.get("index_not_ready") and result.get("total") == 1
            )
            report["checks"]["one_file_reparsed_after_restart"] = (
                report["a_index_after_restart"]["stats"].get("parsed_files") == 1
            )
        measurements.sample("after_final_query")
        report["fingerprint_cache_final_before_close"] = backend.fingerprint_cache.stats()
        report["storage_final"] = storage(service.path, ids)
        report["b_index_status"] = index_status(service, ids["B"])
        report["watch_final"] = watch_summary(watcher, probe, sources)
        reads_b = ledger.snapshot()["B"]
        report["checks"]["unqueried_b_no_source_text_reads"] = (
            reads_b["code_reads"] == reads_b["other_text_reads"] == 0
            and sources["B"].metrics["body_reads"] == 0
        )
        report["checks"]["unqueried_b_no_index_rows"] = not any(
            report["storage_final"]["project_rows"]["B"].values()
        )
        report["checks"]["no_raw_content_columns_in_fact_database"] = not report["storage_final"][
            "raw_content_columns"
        ]
        report["completed"] = True
    except Exception as exc:
        report["completed"] = False
        report["error"] = safe_error(exc)
    finally:
        for component in (watcher, backend if backend is not None else service):
            if component is not None:
                try:
                    component.close()
                except Exception as exc:
                    report["close_error"] = safe_error(exc)
        report["scanner_text_reads"] = ledger.snapshot()
        report["source_access_metrics"] = {
            label: dict(source.metrics) for label, source in sources.items()
        }
        report["root_ignore_load_reads_omitted_by_source_metrics"] = {
            label: sum(
                report["scanner_text_reads"][label][category + "_reads"]
                for category in ("code", "other_text", "root_ignore")
            )
            - source.metrics["body_reads"]
            for label, source in sources.items()
        }
        measurements.sample("after_close")
        ledger.close()
        report["passed_measured_checks"] = bool(report.get("completed")) and all(
            report["checks"].values()
        )
        measurements.save()
    return 0 if report["passed_measured_checks"] else 1


def run_worker(output, phase, timeout):
    started = time.perf_counter()
    environment = dict(os.environ)
    environment.update(UV_NO_CACHE="1", PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(output / "tmp"))
    process = subprocess.Popen(
        [
            sys.executable,
            str(REPO / "scripts/benchmark_live.py"),
            "--_worker",
            phase,
            "--output",
            str(output),
        ],
        cwd=REPO,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.terminate()  # Only the subprocess just created by this benchmark.
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    report_path = output / ("phase-" + phase + ".json")
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    return {
        "process_pid": process.pid,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "process_wall_ms": round((time.perf_counter() - started) * 1000, 3),
        "captured_stdout_bytes": len(stdout),
        "captured_stderr_bytes": len(stderr),
        "raw_process_output_saved": False,
        "measurements": report,
    }


def markdown(report):
    cold = report["phases"].get("cold", {}).get("measurements", {})
    restart = report["phases"].get("restart", {}).get("measurements", {})
    rows = []
    for title, phase, step in (
        ("发现（冷进程）", cold, "discovery"),
        ("A 元数据概览", cold, "a_overview"),
        ("首次 A 结构请求", cold, "first_a_structure"),
        ("A 单文件编辑后重建", cold, "a_single_file_rebuild"),
        ("约 3 MiB 文件第 1 页", cold, "large_file_page_one"),
        ("约 3 MiB 文件第 2 页", cold, "large_file_page_two"),
        ("新进程停机编辑检测", restart, "restart_a_structure"),
    ):
        value = phase.get("steps", {}).get(step, {})
        rows.append(
            f"| {title} | {value.get('wall_ms', 'unavailable')} | {value.get('ok', False)} | "
            f"{value.get('index_not_ready', 'n/a')} | {value.get('index_partial', 'n/a')} |"
        )
    repeated = [
        step["wall_ms"]
        for name, step in cold.get("steps", {}).items()
        if name.startswith("repeat_a_structure_")
    ]
    repeat_text = (
        f"{repeated} ms，样本数 {len(repeated)}，中位数 {statistics.median(repeated):.3f} ms"
        if repeated
        else "未完成"
    )
    fixture = report["fixture"]
    sizes = cold.get("storage_final", {})
    watch = cold.get("watch_idle", {})
    idle = cold.get("resources", {}).get("idle_watch", {})
    peak = max(
        [value.get("process_peak_rss_bytes", 0) for value in cold.get("resources", {}).values()]
        or [0]
    )
    return "\n".join(
        [
            "# Synthetic 原文件直读 / 按需索引基准",
            "",
            "这是一次本机 synthetic 组件测量，不是实际企业项目、业务效果或网页延迟证明。",
            f"A 为 {fixture['projects']['A']['python_definitions']} 个独立 Python 函数，"
            f"分布 {fixture['projects']['A']['python_definition_files']} 个定义文件"
            f"（每文件 {fixture['definitions_per_file']} 个），另有 3 个 Java 文件；"
            f"B 为 100 个函数、{fixture['projects']['B']['python_definition_files']} 个定义文件。"
            "实际规模见 fixture.json。依赖剪枝样例只有少量占位文件。",
            "",
            f"- 实际 A 定义数：{fixture['projects']['A']['python_definitions']}；"
            f"生成文件总字节：{fixture['generated_file_bytes']}。",
            f"- 本轮测量检查通过：{report['passed_measured_checks']}；"
            f"代码在运行期间变化：{report['source_changed_during_run']}。",
            "- 初次为空的新数据库；重复请求复用同一上下文与派生缓存。"
            "第二个新 OS 进程复用数据库，在停机期间追加一个定义后重新校验。",
            "- 冷启动指新进程/空应用缓存，不保证清空 OS 文件缓存；未执行 drop-caches。",
            "",
            "| 操作 | 实测 wall ms | 调用完成 | index_not_ready | index_partial |",
            "| --- | ---: | --- | --- | --- |",
            *rows,
            "",
            f"重复 A 请求：{repeat_text}。小样本不报告 P95/P99。",
            "构建等待使用主线默认 10 秒，计于 Future.result(timeout)；整次请求还包含"
            "枚举、上下文及参与文件校验，所以 wall 超过 10 秒不自动表示等待超时。"
            "index_not_ready / reason / DB index_partial 在 JSON 分开保留。",
            "",
            f"SQLite 实际文件 {sizes.get('database_file_bytes', 'unavailable')} 字节，"
            f"分配 {sizes.get('database_allocated_bytes', 'unavailable')} 字节。",
            "DB 配置 64 MiB，managed_peak_config 128 MiB；当前 DELETE journal 的 3 倍"
            "保留策略把有效页上限降至约 42.66 MiB。这不是进程 RSS 上限，也不是实测磁盘峰值。",
            f"空闲 watch 非递归目录 {watch.get('nonrecursive_directory_count', 'unavailable')}；"
            f"进程 FD {idle.get('process_fd_count', 'unavailable')}；"
            f"当前 RSS {idle.get('current_rss_bytes', 'unavailable')} 字节；"
            f"cold 进程生命周期 RSS 高水位 {peak} 字节。",
            "目录列表证明剪枝，不代表内核 watch 数。FD 是整个进程的采样值，包含探针管道；"
            "没有直接统计 kqueue/FSEvents/inotify 内核监听资源，也不据此宣称零资源开销。",
            "",
            "B 没有结构或正文请求。scanner_text_reads 分开记录 code / other_text / root_ignore；"
            "SourceAccess.metrics 的 body_reads 未计真实 root-ignore 加载，不能单用该计数证明"
            "所有文件零读取。只应把 B 的源码/其他正文读取与索引行数为零作为对应隔离证据。",
            "约 3 MiB 普通文件只取连续两页，各 20,000 字符；SourceAccess 当前仍整文件读入，"
            "分页响应有界不等于物理读取 20,000 字节。",
            "",
            "测量边界：直接调用 registry / LiveQueries / LiveIndexService / WatchCoordinator，"
            "使用与 Runtime 相同的 DB / managed peak 配置及默认 10 秒请求等待；registry 仅内存。"
            "不含完整 WorkspaceRuntime 控制通道、desktop、隧道、MCP 网络、模型调用、"
            "真实企业源码、依赖解析质量、目录海量压力、CPU/磁盘峰值或配额边界验收。",
            "Scanner 计数为解码后返回的完整文本字节，不是内核物理 I/O；采样 CPU 包含本进程"
            "后台线程。各操作后记录数据库/sidecar 大小，不追踪瞬时 journal 峰值。",
            "慢、失败、部分结果与未就绪结果原样标记；本脚本不修改生产源码来凑绿。",
            "",
            "详细结果：performance.json、phase-cold.json、phase-restart.json；"
            "fixture 与数据库保留，不自动清理。",
            "",
        ]
    )


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new empty .artifacts/live-benchmark.XXXXXX")
    parser.add_argument("--definitions-a", type=bounded_int(1000, 5000), default=5000)
    parser.add_argument(
        "--definitions-per-file",
        type=int,
        choices=(1, 50),
        default=DEFAULT_DEFINITIONS_PER_FILE,
        help="Python definitions per file in both A and B (1 or 50; default 50)",
    )
    parser.add_argument("--repeats", type=bounded_int(1, 5), default=3)
    parser.add_argument("--phase-timeout", type=bounded_int(30, 180), default=120)
    parser.add_argument("--_worker", choices=("cold", "restart"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        if args.output is None:
            raise BenchmarkError("INVALID_WORKER: fixture required")
        return phase_worker(output_path(args.output, worker=True), args._worker)
    output = output_path(args.output)
    before = provenance()
    fixture = make_fixture(output, args.definitions_a, args.repeats, args.definitions_per_file)
    report = {
        "schema": 1,
        "synthetic": True,
        "scope": "local component timings; no enterprise, web, tunnel or model benchmark",
        "created_at": datetime.now(UTC).isoformat(),
        "output": output.relative_to(REPO).as_posix(),
        "fixture": fixture,
        "managed_peak_config": MANAGED_PEAK_BYTES,
        "database_config_bytes": DATABASE_BYTES,
        "phase_timeout_seconds": args.phase_timeout,
        "measurement_boundaries": {
            "cold": "new OS process and empty application database; OS file cache not cleared",
            "steady": "same live context and unchanged source; three requests by default",
            "restart": "second OS process; retained fact DB; one offline fixture edit",
            "components": "in-memory registry, LiveQueries, LiveIndexService, WatchCoordinator",
            "excluded": "desktop, tunnel, MCP transport, model, business and enterprise workload",
            "managed_peak": "configured DB/journal reservation, not measured peak or RSS limit",
            "text_reads": "Scanner decoded text bytes, not physical disk I/O",
            "root_ignore": "includes policy loading and explicit index config reads",
            "source_metrics": "body_reads omits actual root-ignore policy loading",
            "fingerprint_budget": "backend-wide entries and estimated charged metadata, not RSS",
            "process_metrics": "RSS sampled and lifetime high-water; FDs include probe pipes",
            "kernel_watch": "not instrumented; directory count is not native watch resources",
            "disk_peak": "post-operation DB/sidecar samples, not transient journal peak",
            "cpu": "process CPU including background threads; not exclusive query CPU",
            "structure_wait": "default 10s Future wait; manifest/context validation adds wall time",
        },
        "provenance_before": before,
        "phases": {},
    }
    cold = run_worker(output, "cold", args.phase_timeout)
    report["phases"]["cold"] = cold
    if cold["measurements"].get("completed") and not cold["timed_out"]:
        with (output / "workspace/A/pkg/module_000.py").open("a", encoding="utf-8") as stream:
            stream.write("\ndef changed_while_stopped(value=0):\n    return value + 2\n")
        report["phases"]["restart"] = run_worker(output, "restart", args.phase_timeout)
    else:
        report["restart_skipped_reason"] = "cold phase incomplete; no automatic full rerun"
    after = provenance()
    report["provenance_after"] = after
    report["changed_source_files"] = [
        name
        for name in PROVENANCE_FILES
        if before["source_sha256"].get(name) != after["source_sha256"].get(name)
    ]
    report["source_changed_during_run"] = bool(report["changed_source_files"])
    report["distinct_process_restart"] = (
        len(report["phases"]) == 2
        and cold["process_pid"] != report["phases"]["restart"]["process_pid"]
    )
    report["passed_measured_checks"] = (
        report["distinct_process_restart"]
        and all(
            phase["exit_code"] == 0
            and not phase["timed_out"]
            and phase["measurements"].get("passed_measured_checks")
            for phase in report["phases"].values()
        )
        and not report["source_changed_during_run"]
    )
    write_json(output / "performance.json", report)
    (output / "summary.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"output": report["output"], "passed": report["passed_measured_checks"]}))
    return 0 if report["passed_measured_checks"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"error": safe_error(error)}), file=sys.stderr)
        sys.exit(2)

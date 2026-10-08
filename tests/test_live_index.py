"""Independent live fact-cache contracts; fixtures never touch real projects."""

import json
import sqlite3
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock, current_thread

import pytest

import code_context.live_index as module
from code_context.intelligence_models import ParsedFile
from code_context.intelligence_queries import decode_relation
from code_context.intelligence_resolver import Resolver
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.read_context import ReadContexts
from code_context.source_access import SourceAccess, SourceError


def write_files(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")


def backend_for(tmp_path, projects):
    sources = {}
    for project_id, files in projects.items():
        root = tmp_path / "sources" / project_id
        write_files(root, files)
        sources[project_id] = SourceAccess(root)
    return LiveQueries(sources)


@pytest.fixture
def service_factory(tmp_path):
    services = []

    def create(backend, **options):
        data_dir = options.pop("data_dir", tmp_path / f"index-{len(services)}")
        service = LiveIndexService(data_dir, **options)
        backend.index_service = service
        services.append(service)
        return service

    yield create
    for service in services:
        service.close()


def handle(backend, project="a"):
    return backend.resolve_snapshot(project)[0]


def query(backend, snapshot, operation, project="a", **parameters):
    return backend.code_query(project, snapshot, operation, **parameters)


def find(backend, snapshot, name, project="a", **parameters):
    result = query(
        backend, snapshot, "symbol_search", project, query=name, exact=True, **parameters
    )
    assert not result.get("index_not_ready"), result
    assert len(result["symbols"]) == 1, result
    return result["symbols"][0]


def rows(service, sql, args=()):
    with sqlite3.connect(service.path) as db:
        return db.execute(sql, args).fetchall()


def python_files(root="Code"):
    prefix = root + "/" if root else ""
    return {
        prefix + "pkg/__init__.py": "",
        prefix + "pkg/models.py": "class Base: pass\nclass Document(Base):\n    value: int\n",
        prefix + "pkg/helper.py": "from pkg.models import Document\n"
        "def normalize(doc: Document):\n    return doc.value\n",
        prefix + "pkg/service.py": "from pkg.helper import normalize as clean\n"
        "from pydantic import BaseModel\n"
        "def ingest(doc):\n    return clean(doc)\n",
    }


JAVA_FILES = {
    "src/main/java/demo/Base.java": "package demo; public class Base {}\n",
    "src/main/java/demo/Readable.java": "package demo; public interface Readable "
    "{ String text(); }\n",
    "src/main/java/demo/Document.java": "package demo; public class Document extends Base "
    'implements Readable { public String text() { return "ok"; } }\n',
    "src/main/java/demo/Helper.java": "package demo; public class Helper { "
    "public static String clean(Document d) { return d.text(); } }\n",
    "src/main/java/api/Service.java": "package api; import demo.Document; import demo.Helper; "
    "import java.util.List; public class Service { "
    "public String ingest(Document d) { return Helper.clean(d); } }\n",
}


def test_first_request_only_indexes_a_and_text_never_requests_index(
    tmp_path, service_factory, monkeypatch
):
    backend = backend_for(tmp_path, {"a": python_files(), "b": {"b.py": "def other(): pass\n"}})
    service = service_factory(backend)

    def forbidden(*args, **kwargs):
        pytest.fail("project B must not be enumerated or read for A")

    monkeypatch.setattr(backend.sources["b"], "read", forbidden)
    monkeypatch.setattr(backend.sources["b"], "manifest", forbidden)
    snapshot = handle(backend)
    backend.repo_overview("a", snapshot)
    backend.read_file("a", "Code/pkg/helper.py", snapshot)
    backend.search_code("a", "normalize", snapshot)
    assert service.status("a")["status"] == "not_requested"
    assert rows(service, "SELECT project_id FROM li_projects") == []
    assert find(backend, snapshot, "normalize")["language"] == "python"
    assert rows(service, "SELECT project_id FROM li_projects") == [("a",)]
    assert service.status("b")["status"] == "not_requested"


@pytest.mark.parametrize("root", ["", "src", "Code", "services/api/Code"])
def test_python_all_nine_tool_families_and_exact_nested_roots(tmp_path, service_factory, root):
    backend = backend_for(tmp_path, {"a": python_files(root)})
    service = service_factory(backend)
    snapshot = handle(backend)
    normalize = find(backend, snapshot, "normalize")
    ingest = find(backend, snapshot, "ingest")
    document = find(backend, snapshot, "Document")
    body = query(backend, snapshot, "read_symbol", symbol_id=normalize["symbol_id"])
    assert body["content"] == "def normalize(doc: Document):\n    return doc.value\n"
    refs = query(backend, snapshot, "find_references", symbol_id=normalize["symbol_id"])
    assert {r["kind"] for r in refs["references"]} >= {"IMPORT", "CALL"}
    calls = query(backend, snapshot, "get_call_graph", symbol_id=ingest["symbol_id"])
    assert any(e["target_symbol_id"] == normalize["symbol_id"] for e in calls["edges"])
    classes = query(backend, snapshot, "get_class_graph", symbol_id=document["symbol_id"])
    assert any(
        e["kind"] == "INHERITANCE" and e["target_qualname"] == "Base" for e in classes["edges"]
    )
    prefix = root + "/" if root else ""
    deps = query(
        backend, snapshot, "get_file_dependencies", path=prefix + "pkg/service.py", depth=3
    )
    assert {e["target"] for e in deps["edges"]} >= {
        prefix + "pkg/helper.py",
        prefix + "pkg/models.py",
    }
    architecture = query(backend, snapshot, "get_project_architecture")
    layers = {f["path"]: f["layer"] for f in architecture["files"]}
    assert (
        layers[prefix + "pkg/models.py"]
        < layers[prefix + "pkg/helper.py"]
        < layers[prefix + "pkg/service.py"]
    )
    external = query(backend, snapshot, "get_external_dependencies")
    assert any(d["module"].startswith("pydantic") for d in external["dependencies"])
    impact = query(backend, snapshot, "get_impact_analysis", path=prefix + "pkg/models.py", depth=3)
    assert {f["path"] for f in impact["affected_files"]} >= {
        prefix + "pkg/helper.py",
        prefix + "pkg/service.py",
    }
    for result in (body, refs, calls, classes, deps, architecture, external, impact):
        assert result["project_id"] == "a" and result["snapshot"] == snapshot
        assert result["source_mode"] == "live" and not result["index_partial"]
        assert "revision" not in result
        assert len(json.dumps(result, ensure_ascii=False)) <= 20_000
    assert root in service.status("a")["stats"]["python_source_roots"]


def test_java_all_nine_tool_families(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": JAVA_FILES})
    service_factory(backend)
    snapshot = handle(backend)
    ingest = find(backend, snapshot, "ingest", language="java")
    clean = find(backend, snapshot, "clean", language="java")
    document = find(backend, snapshot, "Document", language="java")
    assert (
        "Helper.clean(d)"
        in query(backend, snapshot, "read_symbol", symbol_id=ingest["symbol_id"])["content"]
    )
    refs = query(backend, snapshot, "find_references", symbol_id=clean["symbol_id"])
    assert any(e["kind"] == "CALL" for e in refs["references"])
    calls = query(backend, snapshot, "symbol_graph", symbol_id=ingest["symbol_id"], graph="call")
    assert any(e["target_symbol_id"] == clean["symbol_id"] for e in calls["edges"])
    classes = query(
        backend, snapshot, "symbol_graph", symbol_id=document["symbol_id"], graph="class"
    )
    assert {e["kind"] for e in classes["edges"]} >= {"INHERITANCE", "IMPLEMENTS", "CONTAINS"}
    assert any(
        e["target"] == clean["path"]
        for e in query(backend, snapshot, "file_dependencies", path=ingest["path"])["edges"]
    )
    assert query(backend, snapshot, "project_architecture")["total_files"] == len(JAVA_FILES)
    external = query(backend, snapshot, "external_dependencies")
    assert any(e["module"].startswith("java.util") for e in external["dependencies"])
    impact = query(
        backend, snapshot, "impact_analysis", path=clean["path"], symbol_id=clean["symbol_id"]
    )
    assert ingest["path"] in {f["path"] for f in impact["affected_files"]}


def test_only_facts_on_disk_no_blobs_no_mirror_no_literal_bodies(
    tmp_path, service_factory, monkeypatch
):
    from code_context.storage import MirrorStore

    tokens = ["UNIQUE_BODY_PY_461ABCD", "UNIQUE_BODY_JAVA_634ZXCV", "UNIQUE_UNSUPPORTED_95AB"]
    backend = backend_for(
        tmp_path,
        {
            "a": {
                "a.py": f'# {tokens[0]}\ndef value(default="{tokens[0]}"):\n'
                f'    """{tokens[0]}"""\n    return "{tokens[0]}"\n',
                "A.java": "public class A { public String value() { "
                f'return "{tokens[1]}"; }} }}\n',
                "README.md": tokens[2] + "\n",
            }
        },
    )

    def forbidden(*args, **kwargs):
        pytest.fail("live indexing must never create a MirrorStore")

    monkeypatch.setattr(MirrorStore, "__init__", forbidden)
    service = service_factory(backend)
    snapshot = handle(backend)
    value = find(backend, snapshot, "value", language="python")
    assert (
        tokens[0]
        in query(backend, snapshot, "read_symbol", symbol_id=value["symbol_id"])["content"]
    )
    tables = {r[0] for r in rows(service, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {
        "li_projects",
        "files",
        "li_parse_cache",
        "li_binding_cache",
        "ci_files",
        "ci_symbols",
        "ci_relations",
    }
    for table in tables:
        assert "content" not in {r[1] for r in rows(service, f"PRAGMA table_info({table})")}
    for path in service.path.parent.iterdir():
        if path.is_file():
            assert all(token.encode() not in path.read_bytes() for token in tokens)
    assert rows(service, "SELECT sha256 FROM files WHERE path='README.md'") == [(None,)]
    assert rows(service, "PRAGMA foreign_key_check") == []
    assert rows(service, "PRAGMA integrity_check") == [("ok",)]


def test_graph_repeat_and_unchanged_invalidations_do_not_reread_or_reparse(
    tmp_path, service_factory, monkeypatch
):
    backend = backend_for(tmp_path, {"a": python_files()})
    service = service_factory(backend)
    snapshot = handle(backend)
    find(backend, snapshot, "normalize")
    reads = backend.sources["a"].metrics["body_reads"]

    def forbidden(*args):
        pytest.fail("unchanged parsed files should be reused")

    monkeypatch.setattr(module, "parse_file", forbidden)
    query(backend, snapshot, "project_architecture")
    assert backend.sources["a"].metrics["body_reads"] == reads
    service.invalidate("a")
    query(backend, snapshot, "project_architecture")
    assert backend.sources["a"].metrics["body_reads"] == reads
    assert service.status("a")["stats"]["parsed_files"] == 0
    assert service.status("a")["stats"]["reused_parse_files"] == 4


def test_edit_reuses_other_parses_rebinds_and_rejects_stale_context(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": python_files()})
    service = service_factory(backend)
    old = handle(backend)
    clean = find(backend, old, "normalize")
    source = backend.sources["a"]
    (source.root / "Code/pkg/helper.py").write_text("def replacement(): return 2\n")
    with pytest.raises(SourceError, match="LIVE_CONTEXT"):
        service.query(backend, "a", old, "read_symbol", symbol_id=clean["symbol_id"])
    assert service.status("a")["status"] == "dirty"
    fresh = handle(backend)
    ingest = find(backend, fresh, "ingest")
    graph = query(backend, fresh, "symbol_graph", symbol_id=ingest["symbol_id"], graph="call")
    assert all(e["target_symbol_id"] != clean["symbol_id"] for e in graph["edges"])
    assert any(e["resolution"] != "resolved" for e in graph["edges"])
    stats = service.status("a")["stats"]
    assert stats["parsed_files"] == 1 and stats["reused_parse_files"] == 3
    assert stats["resolved_files"] == 4 and stats["reused_relation_files"] == 2
    assert stats["publication_mode"] == "local"
    assert rows(service, "SELECT COUNT(*) FROM li_projects") == [(1,)]
    assert rows(service, "SELECT COUNT(*) FROM li_parse_cache") == [(4,)]
    with pytest.raises(SourceError):
        query(backend, fresh, "read_symbol", symbol_id=clean["symbol_id"])


def test_new_file_changes_old_context_topology_and_new_context_binds(tmp_path, service_factory):
    backend = backend_for(
        tmp_path, {"a": {"client.py": "from helper import value\ndef run(): return value()\n"}}
    )
    service = service_factory(backend)
    old = handle(backend)
    run = find(backend, old, "run")
    (backend.sources["a"].root / "helper.py").write_text("def value(): return 7\n")
    with pytest.raises(SourceError, match="CONTEXT_CHANGED"):
        query(backend, old, "symbol_graph", symbol_id=run["symbol_id"], graph="call")
    fresh = handle(backend)
    value = find(backend, fresh, "value")
    graph = query(backend, fresh, "symbol_graph", symbol_id=run["symbol_id"], graph="call")
    assert any(e["target_symbol_id"] == value["symbol_id"] for e in graph["edges"])
    assert service.status("a")["stats"]["reused_parse_files"] == 1


def test_root_config_change_rebinds_unchanged_facts_and_guards_config(tmp_path, service_factory):
    files = python_files()
    files["pyproject.toml"] = '[tool.colink]\npython_source_roots=[""]\n'
    backend = backend_for(tmp_path, {"a": files})
    service = service_factory(backend)
    old = handle(backend)
    ingest = find(backend, old, "ingest")
    graph = query(backend, old, "symbol_graph", symbol_id=ingest["symbol_id"], graph="call")
    assert all(e["resolution"] != "resolved" for e in graph["edges"])
    assert "pyproject.toml" in backend.contexts.get("a", backend.sources["a"].source_id, old).files
    (backend.sources["a"].root / "pyproject.toml").write_text(
        '[tool.colink]\npython_source_roots=["Code"]\n'
    )
    with pytest.raises(SourceError, match="LIVE_CONTEXT"):
        query(backend, old, "project_architecture")
    fresh = handle(backend)
    normalize = find(backend, fresh, "normalize")
    graph = query(backend, fresh, "symbol_graph", symbol_id=ingest["symbol_id"], graph="call")
    assert any(e["target_symbol_id"] == normalize["symbol_id"] for e in graph["edges"])
    stats = service.status("a")["stats"]
    assert stats["parsed_files"] == 0 and stats["reused_parse_files"] == 4
    assert stats["python_source_roots"] == ["Code"]


@pytest.mark.parametrize("change", ["edit", "add", "remove"])
def test_change_during_build_refuses_mixed_generation_and_marks_dirty(
    tmp_path, service_factory, monkeypatch, change
):
    backend = backend_for(
        tmp_path, {"a": {"a.py": "def first(): return 1\n", "b.py": "def second(): return 2\n"}}
    )
    service = service_factory(backend)
    original = module.parse_file

    def mutate(path, content):
        result = original(path, content)
        if path == "a.py":
            root = backend.sources["a"].root
            if change == "edit":
                (root / "a.py").write_text("def changed(): return 3\n")
            elif change == "add":
                (root / "c.py").write_text("def added(): return 3\n")
            else:
                (root / "b.py").rename(root / "b.saved")
        return result

    monkeypatch.setattr(module, "parse_file", mutate)
    with pytest.raises(SourceError):
        query(backend, handle(backend), "project_architecture")
    assert service.status("a")["status"] == "dirty"
    assert rows(service, "SELECT COUNT(*) FROM li_projects") == [(0,)]
    monkeypatch.setattr(module, "parse_file", original)
    assert not query(backend, handle(backend), "project_architecture").get("index_not_ready")


def test_read_symbol_checks_hash_even_if_index_metadata_is_forced_unchanged(
    tmp_path, service_factory, monkeypatch
):
    backend = backend_for(tmp_path, {"a": {"a.py": "def value():\n    return 1\n"}})
    service = service_factory(backend)
    snapshot = handle(backend)
    value = find(backend, snapshot, "value")
    source = backend.sources["a"]
    original_read = source.read

    def changed_read(path):
        (source.root / "a.py").write_text("# inserted\ndef value():\n    return 2\n")
        return original_read(path)

    monkeypatch.setattr(source, "read", changed_read)
    with pytest.raises(SourceError, match="LIVE_INDEX_CHANGED"):
        query(backend, snapshot, "read_symbol", symbol_id=value["symbol_id"])
    assert service.status("a")["status"] == "dirty"


@pytest.mark.parametrize("language", ["python", "java"])
def test_read_symbol_long_line_and_escaped_unicode_pages_are_exact(
    tmp_path, service_factory, language
):
    literal = ('中\\"字符' * 1500) + "END"
    content = (
        f'def long_value():\r\n    return "{literal}"\r\n'
        if language == "python"
        else f'public class A {{ public String long_value() {{ return "{literal}"; }} }}\r\n'
    )
    path = "a.py" if language == "python" else "A.java"
    backend = backend_for(tmp_path, {"a": {path: content}})
    service_factory(backend)
    snapshot = handle(backend)
    value = find(backend, snapshot, "long_value", language=language)
    pages, line_offset, char_offset = [], 0, 0
    for _ in range(100):
        page = query(
            backend,
            snapshot,
            "read_symbol",
            symbol_id=value["symbol_id"],
            line_offset=line_offset,
            char_offset=char_offset,
            max_chars=1800,
            max_lines=1,
        )
        assert len(json.dumps(page, ensure_ascii=False)) <= 1800
        pages.append(page["content"])
        if not page["has_more"]:
            break
        assert page["next_line_offset"] is not None
        line_offset, char_offset = page["next_line_offset"], page["next_char_offset"]
    else:
        pytest.fail("long-line pagination did not terminate")
    lines = content.splitlines(keepends=True)
    assert "".join(pages) == "".join(lines[max(1, value["start_line"]) - 1 : value["end_line"]])
    assert any(len(piece) < len(lines[-1]) for piece in pages)


def test_unknown_stale_and_cross_project_handles_do_not_trigger_builds(tmp_path, service_factory):
    backend = backend_for(
        tmp_path, {"a": {"a.py": "def value(): pass\n"}, "b": {"b.py": "def other(): pass\n"}}
    )
    service = service_factory(backend)
    for selector in [None, "current", "previous", "snap_old", "live_unknown"]:
        with pytest.raises(SourceError, match="LIVE_CONTEXT"):
            service.query(backend, "a", selector, "symbol_search", query="value")
    with pytest.raises(SourceError):
        service.query(backend, "b", handle(backend), "project_architecture")
    with pytest.raises(SourceError, match="PROJECT_NOT_AUTHORIZED"):
        service.query(backend, "unknown", "live_unknown", "project_architecture")
    assert rows(service, "SELECT project_id FROM li_projects") == []
    assert backend.sources["b"].metrics["body_reads"] == 0


def test_source_replacement_isolated_and_old_source_facts_not_reused(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": {"a.py": "def old_name(): return 1\n"}})
    service = service_factory(backend)
    old = handle(backend)
    find(backend, old, "old_name")
    source = backend.sources["a"]
    source.root.rename(source.root.with_name("a.saved"))
    write_files(source.root, {"a.py": "def new_name(): return 2\n"})
    with pytest.raises(SourceError, match="SOURCE_"):
        query(backend, old, "project_architecture")
    backend.sources["a"] = SourceAccess(source.root)
    with pytest.raises(SourceError, match="LIVE_CONTEXT"):
        query(backend, old, "symbol_search", query="new_name")
    fresh = handle(backend)
    find(backend, fresh, "new_name")
    assert not query(backend, fresh, "symbol_search", query="old_name")["symbols"]
    assert service.status("a")["stats"]["reused_parse_files"] == 0
    assert rows(service, "SELECT source_id FROM li_projects") == [(backend.sources["a"].source_id,)]
    assert rows(service, "SELECT COUNT(*) FROM li_parse_cache") == [(1,)]


def test_single_worker_coalesces_same_project_and_bounds_wait_queue(
    tmp_path, service_factory, monkeypatch
):
    backend = backend_for(
        tmp_path,
        {"a": {"a.py": "def value(): return 1\n"}, "b": {"b.py": "def other(): return 2\n"}},
    )
    service = service_factory(backend, wait_seconds=0.02, max_pending_projects=1)
    entered, release = Event(), Event()
    calls, lock = [], Lock()
    original = module.parse_file

    def slow(path, content):
        with lock:
            calls.append((path, current_thread().name))
        entered.set()
        assert release.wait(10)
        return original(path, content)

    monkeypatch.setattr(module, "parse_file", slow)
    snapshot = handle(backend)
    try:
        pending = query(backend, snapshot, "symbol_search", query="value")
        assert pending["reason"] == "BUILD_PENDING" and pending["index_not_ready"]
        assert entered.wait(2)
        task = service._tasks["a"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: query(backend, snapshot, "project_architecture"), range(4))
            )
        assert all(result["reason"] == "BUILD_PENDING" for result in results)
        b = query(backend, handle(backend, "b"), "project_architecture", "b")
        assert b["reason"] == "BUILD_QUEUE_LIMIT"
        assert backend.sources["b"].metrics["body_reads"] == 0
        assert service._tasks["a"] is task and len(calls) == 1
    finally:
        release.set()
    task.result(timeout=5)
    assert find(backend, snapshot, "value")["name"] == "value"
    assert calls[0][1].startswith("colink-live-index")


def test_invalidation_during_build_prevents_publication_and_never_builds_b(
    tmp_path, service_factory, monkeypatch
):
    backend = backend_for(
        tmp_path, {"a": {"a.py": "def value(): pass\n"}, "b": {"b.py": "def other(): pass\n"}}
    )
    service = service_factory(backend, wait_seconds=0)
    entered, release = Event(), Event()
    original = module.parse_file

    def slow(path, content):
        entered.set()
        assert release.wait(10)
        return original(path, content)

    monkeypatch.setattr(module, "parse_file", slow)
    try:
        query(backend, handle(backend), "project_architecture")
        assert entered.wait(2)
        task = service._tasks["a"]
        service.invalidate("a")
        service.invalidate("b")
        assert backend.sources["b"].metrics["body_reads"] == 0
        assert "b" not in service._tasks
    finally:
        release.set()
    with pytest.raises(SourceError, match="LIVE_INDEX_CHANGED"):
        task.result(timeout=5)
    assert rows(service, "SELECT project_id FROM li_projects") == []


def test_lru_project_limit_only_evicts_new_facts_and_preserves_original_data(
    tmp_path, service_factory
):
    backend = backend_for(tmp_path, {p: {f"{p}.py": f"def value_{p}(): pass\n"} for p in "abc"})
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original_db = data_dir / "mirror.sqlite3"
    original_db.write_bytes(b"ORIGINAL_MIRROR_DO_NOT_TOUCH")
    service = service_factory(backend, data_dir=data_dir, max_projects=2)
    snapshots = {p: handle(backend, p) for p in "abc"}
    for p in "ab":
        find(backend, snapshots[p], f"value_{p}", p)
    find(backend, snapshots["a"], "value_a")  # A is now most recent.
    find(backend, snapshots["c"], "value_c", "c")
    assert {r[0] for r in rows(service, "SELECT project_id FROM li_projects")} == {"a", "c"}
    assert service.status("b")["reason"] == "LRU_EVICTED"
    assert original_db.read_bytes() == b"ORIGINAL_MIRROR_DO_NOT_TOUCH"
    assert (backend.sources["b"].root / "b.py").read_text() == "def value_b(): pass\n"
    find(backend, snapshots["b"], "value_b", "b")
    assert len(rows(service, "SELECT project_id FROM li_projects")) == 2


def test_global_physical_page_cap_eviction_peak_strategy_and_atomic_failure(
    tmp_path, service_factory
):
    text = "".join(f"def value_{n}(): return {n}\n" for n in range(140))
    backend = backend_for(
        tmp_path, {"a": {"a.py": text}, "b": {"b.py": text}, "c": {"c.py": text * 8}}
    )
    probe = service_factory(backend)
    find(backend, handle(backend), "value_0")
    budget = probe.path.stat().st_size + 16 * 4096
    probe.close()
    service = service_factory(backend, max_bytes=budget, max_peak_bytes=3 * budget, max_projects=4)
    snapshots = {p: handle(backend, p) for p in "abc"}
    find(backend, snapshots["a"], "value_0")
    find(backend, snapshots["b"], "value_0", "b")
    assert rows(service, "SELECT project_id FROM li_projects") == [("b",)]
    assert service.path.stat().st_size <= budget
    result = query(backend, snapshots["c"], "project_architecture", "c")
    assert result["index_not_ready"] and result["reason"] == "GLOBAL_STORAGE_BUDGET_EXCEEDED"
    assert rows(service, "SELECT project_id FROM li_projects") == [("b",)]
    assert rows(service, "PRAGMA integrity_check") == [("ok",)]
    assert service.path.stat().st_size <= budget
    assert rows(service, "PRAGMA journal_mode") == [("delete",)]
    assert rows(service, "PRAGMA auto_vacuum") == [(1,)]
    assert not service.path.with_name(service.path.name + "-wal").exists()
    assert service.status("b")["limits"]["temporary_peak_bytes"] == 3 * budget
    assert backend.read_file("c", "c.py", snapshots["c"])["content"].startswith("def value_0")


@pytest.mark.parametrize(
    "options",
    [{"max_source_bytes": 1}, {"max_parse_bytes": 1}, {"max_index_bytes": 1}, {"max_facts": 1}],
)
def test_per_project_budgets_are_explicit_partial_and_text_remains_usable(
    tmp_path, service_factory, options
):
    backend = backend_for(tmp_path, {"a": python_files()})
    service = service_factory(backend, **options)
    snapshot = handle(backend)
    result = query(backend, snapshot, "symbol_search", query="normalize")
    assert result["index_partial"]
    stats = service.status("a")["stats"]
    assert stats["parse_payload_bytes"] <= service.max_parse_bytes
    assert stats["index_payload_bytes"] <= service.max_index_bytes
    assert stats["parse_fact_count"] <= service.max_facts
    assert stats["fact_count"] <= service.max_facts
    assert "return doc.value" in backend.read_file("a", "Code/pkg/helper.py", snapshot)["content"]


def test_tiny_global_budget_fails_safely_then_can_reopen_with_larger_budget(
    tmp_path, service_factory
):
    backend = backend_for(tmp_path, {"a": {"a.py": "def value(): return 1\n"}})
    data_dir = tmp_path / "tiny-data"
    service = service_factory(backend, data_dir=data_dir, max_bytes=4096)
    snapshot = handle(backend)
    result = query(backend, snapshot, "symbol_search", query="value")
    assert result["index_not_ready"]
    assert service.path.stat().st_size <= 4096
    assert backend.read_file("a", "a.py", snapshot)["content"].startswith("def value")
    service.close()
    reopened = service_factory(backend, data_dir=data_dir)
    find(backend, handle(backend), "value")
    assert reopened.status("a")["status"] == "ready"


def test_parse_failure_unknown_language_and_invalid_roots_report_partial(tmp_path, service_factory):
    backend = backend_for(
        tmp_path,
        {
            "a": {
                "bad.py": "def BROKEN(\n",
                "ok.py": "def value(): pass\n",
                "ignored.ts": "export const value = 1;\n",
                "pyproject.toml": '[tool.colink]\npython_source_roots=["../escape"]\n',
            }
        },
    )
    service = service_factory(backend)
    snapshot = handle(backend)
    result = query(backend, snapshot, "symbol_search", query="value")
    assert result["index_partial"] and result["symbols"]
    assert service.status("a")["stats"]["files_by_status"]["parse_error"] == 1
    assert service.status("a")["stats"]["python_source_roots_diagnostics"] == [
        {"code": "PYTHON_SOURCE_ROOT_CONFIG_INVALID"}
    ]
    assert rows(service, "SELECT path FROM li_parse_cache WHERE path='ignored.ts'") == []
    assert backend.read_file("a", "bad.py", snapshot)["content"] == "def BROKEN(\n"


def test_restart_reuses_cache_without_scanning_unqueried_projects_and_detects_offline_edit(
    tmp_path, service_factory, monkeypatch
):
    backend = backend_for(tmp_path, {"a": python_files(), "b": {"b.py": "def other(): pass\n"}})
    data_dir = tmp_path / "restart-data"
    service = service_factory(backend, data_dir=data_dir)
    find(backend, handle(backend), "normalize")
    service.close()
    backend = LiveQueries({p: SourceAccess(source.root) for p, source in backend.sources.items()})
    reopened = service_factory(backend, data_dir=data_dir)
    assert reopened.status("a")["status"] == "cached"
    assert all(source.metrics["body_reads"] == 0 for source in backend.sources.values())
    original = module.parse_file
    parsed = []

    def count(path, content):
        parsed.append(path)
        return original(path, content)

    monkeypatch.setattr(module, "parse_file", count)
    find(backend, handle(backend), "normalize")
    assert parsed == [] and backend.sources["b"].metrics["body_reads"] == 0
    reopened.close()
    (backend.sources["a"].root / "Code/pkg/helper.py").write_text("def changed(): return 9\n")
    again = service_factory(backend, data_dir=data_dir)
    find(backend, handle(backend), "changed")
    assert parsed == ["Code/pkg/helper.py"]
    assert again.status("a")["stats"]["reused_parse_files"] == 3
    assert backend.sources["b"].metrics["body_reads"] == 0


def test_compact_live_relations_and_safe_reuse_after_edits(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": python_files()})
    service = service_factory(backend)
    original = handle(backend)
    symbol = find(backend, original, "normalize")
    reference_result = query(backend, original, "find_references", symbol_id=symbol["symbol_id"])
    assert reference_result["references"]
    payload = rows(service, "SELECT data FROM ci_relations ORDER BY rowid LIMIT 1")[0][0]
    assert isinstance(payload, bytes) and payload.startswith(b"CL1:")
    parsed_payload = rows(
        service, "SELECT data FROM li_parse_cache WHERE path='Code/pkg/models.py'"
    )[0][0]
    assert isinstance(parsed_payload, bytes) and parsed_payload.startswith(b"CP1:")

    helper = backend.sources["a"].root / "Code/pkg/helper.py"
    helper.write_text(helper.read_text().replace("return doc.value", "return doc.value + 1"))
    changed = handle(backend)
    symbol = find(backend, changed, "normalize")
    stats = service.status("a")["stats"]
    assert stats["parsed_files"] == 1
    assert stats["reused_parse_files"] == 3
    assert stats["reused_relation_files"] == 3
    assert not stats["partial"]
    assert (
        query(backend, changed, "find_references", symbol_id=symbol["symbol_id"])["references"]
        == reference_result["references"]
    )
    ingest = find(backend, changed, "ingest")
    graph = query(backend, changed, "get_call_graph", symbol_id=ingest["symbol_id"])
    assert any(edge["target_symbol_id"] == symbol["symbol_id"] for edge in graph["edges"])

    helper.write_text(helper.read_text() + "\ndef extra():\n    pass\n")
    topology_changed = handle(backend)
    assert find(backend, topology_changed, "extra")
    stats = service.status("a")["stats"]
    assert stats["reused_relation_files"] == 3
    assert stats["publication_mode"] == "local"
    assert not stats["partial"]


def test_stable_content_exclusions_allow_reuse_but_changed_exclusions_rebind(
    tmp_path, service_factory
):
    files = python_files()
    files["blocked.py"] = "credential = 'sk-" + "x" * 35 + "'\n"
    backend = backend_for(tmp_path, {"a": files})
    service = service_factory(backend)
    original = find(backend, handle(backend), "normalize")
    assert service.status("a")["stats"]["partial"]
    helper = backend.sources["a"].root / "Code/pkg/helper.py"
    helper.write_text(helper.read_text().replace("return doc.value", "return doc.value + 1"))
    snapshot = handle(backend)
    current = find(backend, snapshot, "normalize")
    assert current["symbol_id"] == original["symbol_id"]
    stats = service.status("a")["stats"]
    assert stats["partial"] and stats["relation_reuse_safe"]
    assert stats["reused_relation_files"] == 3 and stats["rebound_files"] == 2
    ingest = find(backend, snapshot, "ingest")
    assert any(
        edge["target_symbol_id"] == current["symbol_id"]
        for edge in query(backend, snapshot, "get_call_graph", symbol_id=ingest["symbol_id"])[
            "edges"
        ]
    )
    blocked = backend.sources["a"].root / "blocked.py"
    with pytest.raises(SourceError, match="FILE_EXCLUDED"):
        backend.sources["a"].read("blocked.py")
    blocked.write_text(blocked.read_text() + "# changed unavailable source\n")
    find(backend, handle(backend), "normalize")
    assert service.status("a")["stats"]["reused_relation_files"] == 0
    blocked.write_text("def now_visible(): return 1\n")
    assert find(backend, handle(backend), "now_visible")
    assert not service.status("a")["stats"]["partial"]
    assert service.status("a")["stats"]["reused_relation_files"] == 0


def test_changed_calls_rebind_only_changed_file_and_match_full_rebuild(tmp_path, service_factory):
    backend = backend_for(
        tmp_path,
        {
            "a": {
                "model.py": "def first(): return 1\ndef second(): return 2\n",
                "caller.py": "from model import first, second\ndef run(): return first()\n",
            }
        },
    )
    service = service_factory(backend)
    snapshot = handle(backend)
    run = find(backend, snapshot, "run")
    first = find(backend, snapshot, "first")
    second = find(backend, snapshot, "second")
    initial = query(backend, snapshot, "get_call_graph", symbol_id=run["symbol_id"])
    assert any(e["target_symbol_id"] == first["symbol_id"] for e in initial["edges"])
    caller = backend.sources["a"].root / "caller.py"
    caller.write_text(caller.read_text().replace("return first()", "return second()"))
    updated = handle(backend)
    find(backend, updated, "run")
    stats = service.status("a")["stats"]
    assert stats["parsed_files"] == 1 and stats["reused_relation_files"] == 1
    incremental = query(backend, updated, "get_call_graph", symbol_id=run["symbol_id"])
    assert any(e["target_symbol_id"] == second["symbol_id"] for e in incremental["edges"])
    assert all(e["target_symbol_id"] != first["symbol_id"] for e in incremental["edges"])
    old_facts = rows(service, "SELECT data FROM ci_relations ORDER BY source_path,rowid")
    fresh = service_factory(backend)
    find(backend, handle(backend), "run")
    assert [decode_relation(r[0]) for r in old_facts] == [
        decode_relation(r[0])
        for r in rows(fresh, "SELECT data FROM ci_relations ORDER BY source_path,rowid")
    ]


def _canonical_core(service):
    return {
        table: sorted(repr(row) for row in rows(service, f"SELECT * FROM {table}"))
        for table in ("ci_files", "ci_symbols", "ci_relations")
    }


def test_binding_read_tracker_does_not_retain_completed_resolver():
    resolver = Resolver({"a.py": ParsedFile("a.py")}, track_dependencies=True)
    resolver.resolve_file("a.py")
    reference = weakref.ref(resolver)
    # Keep the callback owner alive to prove no strong back-edge is present;
    # do not mask a cycle with an explicit gc.collect().
    tracker = resolver.binding_reads
    del resolver
    assert reference() is None
    assert tracker.files["a.py"]


def _assert_full_equivalent(backend, service, service_factory, name="run"):
    snapshot = handle(backend)
    symbol = find(backend, snapshot, name)
    incremental = {
        operation: query(backend, snapshot, operation, **parameters)
        for operation, parameters in (
            ("get_call_graph", {"symbol_id": symbol["symbol_id"]}),
            ("get_file_dependencies", {"path": symbol["path"], "depth": 3}),
        )
    }
    expected = _canonical_core(service)
    try:
        fresh = service_factory(backend)
        fresh_snapshot = handle(backend)
        assert find(backend, fresh_snapshot, name) == symbol
        assert _canonical_core(fresh) == expected
        for operation, result in incremental.items():
            parameters = (
                {"symbol_id": symbol["symbol_id"]}
                if operation == "get_call_graph"
                else {"path": symbol["path"], "depth": 3}
            )
            actual = query(backend, fresh_snapshot, operation, **parameters)
            assert {k: v for k, v in actual.items() if k != "snapshot"} == {
                k: v for k, v in result.items() if k != "snapshot"
            }
    finally:
        backend.index_service = service


def test_local_body_edit_preserves_parent_and_unrelated_rows(tmp_path, service_factory):
    backend = backend_for(
        tmp_path,
        {
            "a": {
                "provider.py": "def first(): return 1\ndef second(): return 2\n",
                "caller.py": "from provider import first, second\ndef run(): return first()\n",
                "unrelated.py": "def independent(): return 3\n",
            }
        },
    )
    service = service_factory(backend)
    find(backend, handle(backend), "run")
    columns = {
        "files": "path",
        "li_parse_cache": "path",
        "ci_files": "path",
        "ci_symbols": "path",
        "ci_relations": "source_path",
        "li_binding_cache": "path",
    }
    untouched = {
        table: rows(service, f"SELECT rowid,* FROM {table} WHERE {column}='unrelated.py'")
        for table, column in columns.items()
    }
    with sqlite3.connect(service.path) as db:
        db.executescript("""
            CREATE TRIGGER no_project_delete BEFORE DELETE ON li_projects
            BEGIN SELECT RAISE(ABORT, 'parent must survive local publication'); END;
            CREATE TRIGGER no_unrelated_delete BEFORE DELETE ON ci_relations
            WHEN OLD.source_path='unrelated.py'
            BEGIN SELECT RAISE(ABORT, 'unrelated rows must survive'); END;
        """)
    caller = backend.sources["a"].root / "caller.py"
    caller.write_text(caller.read_text().replace("return first()", "return second()"))
    find(backend, handle(backend), "run")
    stats = service.status("a")["stats"]
    assert stats["publication_mode"] == "local"
    assert stats["rebound_files"] == stats["published_fact_files"] == 1
    assert service.status("a")["storage"]["last_publication"]["inserted_rows"]["ci_symbols"] == 2
    assert untouched == {
        table: rows(service, f"SELECT rowid,* FROM {table} WHERE {column}='unrelated.py'")
        for table, column in columns.items()
    }
    _assert_full_equivalent(backend, service, service_factory)


@pytest.mark.parametrize("change", ["add_export", "rename", "shift", "new_module", "delete"])
def test_local_namespace_changes_match_fresh_build(tmp_path, service_factory, change):
    files = {
        "caller.py": "from provider import wanted\ndef run(): return wanted()\n",
        "unrelated.py": "def independent(): return 3\n",
    }
    if change != "new_module":
        files["provider.py"] = (
            "def other(): return 2\n" if change == "add_export" else "def wanted(): return 1\n"
        )
    backend = backend_for(tmp_path, {"a": files})
    service = service_factory(backend)
    find(backend, handle(backend), "run")
    provider = backend.sources["a"].root / "provider.py"
    if change in {"add_export", "new_module"}:
        provider.write_text(
            (provider.read_text() if provider.exists() else "") + "def wanted(): return 1\n"
        )
    elif change == "rename":
        provider.write_text(provider.read_text().replace("wanted", "replacement"))
    elif change == "shift":
        provider.write_text("# shifted declaration\n" + provider.read_text())
    else:
        provider.unlink()
    symbol = find(backend, handle(backend), "run")
    stats = service.status("a")["stats"]
    assert stats["publication_mode"] == "local"
    assert stats["reused_relation_files"] >= 1
    graph = query(backend, handle(backend), "get_call_graph", symbol_id=symbol["symbol_id"])
    resolved = any(e["kind"] == "CALL" and e["resolution"] == "resolved" for e in graph["edges"])
    assert resolved == (change not in {"delete", "rename"})
    _assert_full_equivalent(backend, service, service_factory)


def test_local_reexport_chain_and_cycles_match_full_build(tmp_path, service_factory):
    backend = backend_for(
        tmp_path,
        {
            "a": {
                "provider.py": "def wanted(): return 1\n",
                "facade.py": "from provider import wanted\n",
                "caller.py": "from facade import wanted\ndef run(): return wanted()\n",
                "cycle_a.py": "from cycle_b import missing\ndef a(): return missing()\n",
                "cycle_b.py": "from cycle_a import missing\ndef b(): return missing()\n",
                "unrelated.py": "def independent(): return 3\n",
            }
        },
    )
    service = service_factory(backend)
    find(backend, handle(backend), "run")
    provider = backend.sources["a"].root / "provider.py"
    provider.write_text("# shifted\n" + provider.read_text())
    find(backend, handle(backend), "run")
    stats = service.status("a")["stats"]
    assert stats["publication_mode"] == "local"
    assert stats["reused_relation_files"] == 3
    _assert_full_equivalent(backend, service, service_factory)


@pytest.mark.parametrize("change", ["overload", "new_type", "inheritance"])
def test_local_java_target_changes_match_full_build(tmp_path, service_factory, change):
    files = {
        "demo/Tools.java": "package demo; public class Tools { "
        "public static int clean(int x) { return x; } }\n",
        "api/Service.java": "package api; import demo.Tools; import demo.*; "
        "public class Service { public Object run() { "
        + ("return new Later();" if change == "new_type" else "return Tools.clean(1);")
        + " } }\n",
        "demo/Base.java": "package demo; public class Base {}\n",
        "demo/Child.java": "package demo; public class Child extends Base {}\n",
        "demo/Unrelated.java": "package demo; public class Unrelated {}\n",
    }
    backend = backend_for(tmp_path, {"a": files})
    service = service_factory(backend)
    find(backend, handle(backend), "run")
    root = backend.sources["a"].root
    if change == "overload":
        target = root / "demo/Tools.java"
        target.write_text(target.read_text().replace("int x)", "int x, int y)"))
    elif change == "new_type":
        (root / "demo/Later.java").write_text("package demo; public class Later {}\n")
    else:
        target = root / "demo/Base.java"
        target.write_text("\n" + target.read_text())
    find(backend, handle(backend), "run")
    assert service.status("a")["stats"]["publication_mode"] == "local"
    assert service.status("a")["stats"]["reused_relation_files"] >= 1
    _assert_full_equivalent(backend, service, service_factory)


def test_local_publication_failure_rolls_back_all_tables(tmp_path, service_factory):
    backend = backend_for(
        tmp_path,
        {
            "a": {
                "provider.py": "def wanted(): return 1\n",
                "caller.py": "from provider import wanted\ndef run(): return wanted()\n",
            }
        },
    )
    service = service_factory(backend)
    find(backend, handle(backend), "run")
    before = {
        table: rows(service, f"SELECT * FROM {table} ORDER BY rowid")
        for table in ("li_projects", *module._TABLES)
    }
    with sqlite3.connect(service.path) as db:
        db.executescript("""
            CREATE TRIGGER fail_relation_insert BEFORE INSERT ON ci_relations
            BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END;
        """)
    caller = backend.sources["a"].root / "caller.py"
    caller.write_text(caller.read_text().replace("return wanted()", "return wanted() + 1"))
    failed = query(backend, handle(backend), "symbol_search", query="run")
    assert failed["index_not_ready"] and failed["reason"] == "INDEX_BUILD_FAILED"
    assert before == {
        table: rows(service, f"SELECT * FROM {table} ORDER BY rowid") for table in before
    }
    with sqlite3.connect(service.path) as db:
        db.execute("DROP TRIGGER fail_relation_insert")
    find(backend, handle(backend), "run")
    _assert_full_equivalent(backend, service, service_factory)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_type", "budget"])
def test_local_evidence_unavailable_falls_back_to_full(
    tmp_path, service_factory, monkeypatch, damage
):
    backend = backend_for(tmp_path, {"a": python_files("")})
    service = service_factory(backend)
    find(backend, handle(backend), "ingest")
    if damage == "budget":
        monkeypatch.setattr(module, "_MAX_BINDING_BYTES", 1)
    else:
        with sqlite3.connect(service.path) as db:
            if damage == "missing":
                db.execute("DELETE FROM li_binding_cache WHERE path='pkg/service.py'")
            else:
                db.execute(
                    "UPDATE li_binding_cache SET data=? WHERE path='pkg/service.py'",
                    (17 if damage == "wrong_type" else b"BC1:broken",),
                )
    helper = backend.sources["a"].root / "pkg/helper.py"
    helper.write_text(helper.read_text().replace("return doc.value", "return doc.value + 1"))
    find(backend, handle(backend), "ingest")
    assert service.status("a")["stats"]["publication_mode"] == "full"
    _assert_full_equivalent(backend, service, service_factory, "ingest")


def test_local_binding_evidence_survives_restart(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": python_files("")})
    directory = tmp_path / "restart-local"
    service = service_factory(backend, data_dir=directory)
    find(backend, handle(backend), "ingest")
    service.close()
    service = service_factory(backend, data_dir=directory)
    helper = backend.sources["a"].root / "pkg/helper.py"
    helper.write_text(helper.read_text().replace("return doc.value", "return doc.value + 1"))
    find(backend, handle(backend), "ingest")
    assert service.status("a")["stats"]["publication_mode"] == "local"
    assert service.status("a")["stats"]["rebound_files"] == 1
    _assert_full_equivalent(backend, service, service_factory, "ingest")


def test_legacy_endpoint_indexes_migrate_without_losing_unresolved_facts(tmp_path, service_factory):
    project = "p_" + "f" * 32
    backend = backend_for(tmp_path, {project: python_files()})
    directory = tmp_path / "legacy-endpoint-index"
    service = service_factory(backend, data_dir=directory)
    symbol = find(backend, handle(backend, project), "normalize", project)
    baseline = rows(service, "SELECT data FROM ci_relations ORDER BY rowid")
    service.close()
    with sqlite3.connect(service.path) as db:
        for name, column in (
            ("ci_relation_source", "source_symbol_id"),
            ("ci_relation_target", "target_symbol_id"),
        ):
            db.execute(f"DROP INDEX {name}")
            db.execute(f"CREATE INDEX {name} ON ci_relations(project_id,revision,{column})")
    reopened = service_factory(backend, data_dir=directory)
    assert rows(reopened, "SELECT data FROM ci_relations ORDER BY rowid") == baseline
    assert rows(reopened, "PRAGMA integrity_check") == [("ok",)]
    definitions = dict(
        rows(reopened, "SELECT name,sql FROM sqlite_master WHERE name LIKE 'ci_relation_%'")
    )
    assert "WHERE target_symbol_id IS NOT NULL" in definitions["ci_relation_target"]
    assert "WHERE source_symbol_id IS NOT NULL" in definitions["ci_relation_source"]
    assert find(backend, handle(backend, project), "normalize", project) == symbol
    external = query(backend, handle(backend, project), "external_dependencies", project)
    assert external["dependencies"]
    assert rows(reopened, "SELECT count(*) FROM ci_relations WHERE target_symbol_id IS NULL")[0][0]


def test_real_length_project_ids_fit_compact_index_and_failed_publish_reports_storage(
    tmp_path, service_factory
):
    project = "p_" + "1" * 32
    text = "".join(f"def value_{n}(): return missing_{n}()\n" for n in range(350))
    backend = backend_for(tmp_path, {project: {"a.py": text}})
    probe = service_factory(backend)
    find(backend, handle(backend, project), "value_0", project)
    compact_bytes = probe.path.stat().st_size
    probe.close()
    with sqlite3.connect(probe.path) as db:
        db.execute("DROP INDEX ci_relation_target")
        db.execute(
            "CREATE INDEX ci_relation_target ON ci_relations(project_id,revision,target_symbol_id)"
        )
    assert probe.path.stat().st_size > compact_bytes
    service = service_factory(backend, max_bytes=compact_bytes)
    find(backend, handle(backend, project), "value_0", project)
    assert service.path.stat().st_size <= compact_bytes
    assert service.status(project)["limits"]["database_bytes"] == compact_bytes
    (backend.sources[project].root / "a.py").write_text(text + text.replace("value_", "extra_"))
    failed = query(backend, handle(backend, project), "project_architecture", project)
    assert failed["reason"] == "GLOBAL_STORAGE_BUDGET_EXCEEDED"
    publication = failed["storage"]["last_publication"]
    assert publication["project_id_bytes"] == 34 and not publication["complete"]
    assert publication["phase"] in module._TABLES
    assert failed["storage"]["page_count"] <= failed["storage"]["page_ceiling"]
    assert rows(service, "PRAGMA integrity_check") == [("ok",)]


def test_corrupt_parse_cache_reparses_only_the_affected_file(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": python_files(), "b": {"b.py": "def other(): pass\n"}})
    service = service_factory(backend)
    snapshot = handle(backend)
    normalize = find(backend, snapshot, "normalize")
    before = query(backend, snapshot, "find_references", symbol_id=normalize["symbol_id"])
    find(backend, handle(backend, "b"), "other", project="b")
    other = rows(service, "SELECT * FROM li_projects WHERE project_id='b'")
    with service._lock:
        service._db.execute(
            "UPDATE li_parse_cache SET data=? WHERE project_id='a' AND path='Code/pkg/helper.py'",
            (b"CP1:broken",),
        )
        service._db.commit()
    service.invalidate("a")
    after = query(backend, snapshot, "find_references", symbol_id=normalize["symbol_id"])
    assert after["references"] == before["references"]
    stats = service.status("a")["stats"]
    assert stats["parsed_files"] == stats["repaired_parse_files"] == 1
    assert stats["reused_parse_files"] == 3
    assert rows(service, "SELECT * FROM li_projects WHERE project_id='b'") == other
    assert not query(backend, snapshot, "project_architecture").get("index_not_ready")


def test_corrupt_relation_cache_rebuilds_after_restart_without_reusing_damage(
    tmp_path, service_factory
):
    backend = backend_for(tmp_path, {"a": python_files()})
    directory = tmp_path / "repair-index"
    service = service_factory(backend, data_dir=directory)
    snapshot = handle(backend)
    ingest = find(backend, snapshot, "ingest")
    before = query(backend, snapshot, "get_call_graph", symbol_id=ingest["symbol_id"])
    with service._lock:
        service._db.execute(
            "UPDATE ci_relations SET data=? WHERE source_symbol_id=? AND kind='CALL'",
            (b"CL1:broken", ingest["symbol_id"]),
        )
        service._db.commit()
    refused = query(backend, snapshot, "get_call_graph", symbol_id=ingest["symbol_id"])
    assert refused["index_not_ready"] and refused["reason"] == "INDEX_CACHE_INVALID"
    assert "edges" not in refused
    service.close()
    repaired = service_factory(backend, data_dir=directory)
    after = query(backend, snapshot, "get_call_graph", symbol_id=ingest["symbol_id"])
    assert after["edges"] == before["edges"]
    stats = repaired.status("a")["stats"]
    assert stats["parsed_files"] == stats["reused_relation_files"] == 0
    assert stats["reused_parse_files"] == 4


def test_legacy_json_parses_and_relations_remain_readable_and_reusable(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": python_files()})
    directory = tmp_path / "legacy-index"
    service = service_factory(backend, data_dir=directory)
    snapshot = handle(backend)
    ingest = find(backend, snapshot, "ingest")
    before = query(backend, snapshot, "get_call_graph", symbol_id=ingest["symbol_id"])
    with service._lock:
        for rowid, data in rows(service, "SELECT rowid, data FROM li_parse_cache"):
            service._db.execute(
                "UPDATE li_parse_cache SET data=?, parser_version=? WHERE rowid=?",
                (
                    json.dumps(module._parse_cached_file(data).to_dict()),
                    module.PARSER_VERSION,
                    rowid,
                ),
            )
        for rowid, data in rows(service, "SELECT rowid, data FROM ci_relations"):
            service._db.execute(
                "UPDATE ci_relations SET data=? WHERE rowid=?",
                (json.dumps(decode_relation(data)), rowid),
            )
        service._db.commit()
    service.close()
    reopened = service_factory(backend, data_dir=directory)
    assert (
        query(backend, snapshot, "get_call_graph", symbol_id=ingest["symbol_id"])["edges"]
        == before["edges"]
    )
    reopened.invalidate("a")
    after = query(backend, snapshot, "get_call_graph", symbol_id=ingest["symbol_id"])
    assert after["edges"] == before["edges"]
    # Rewritten legacy payloads invalidate the declaration cache's parse digest.
    # Rebuild its read evidence once; subsequent edits can use local publication.
    assert reopened.status("a")["stats"]["reused_relation_files"] == 0
    # JSON-only builds select PARSER_VERSION. They must skip packed CP1 rows
    # on rollback and regenerate their own facts from the original source.
    assert rows(
        reopened,
        "SELECT COUNT(*) FROM li_parse_cache WHERE parser_version=?",
        (module.PARSER_VERSION,),
    ) == [(0,)]
    assert rows(reopened, "SELECT DISTINCT parser_version FROM li_parse_cache") == [
        (module._PARSE_CACHE_VERSION,)
    ]


@pytest.mark.parametrize("change", ["edit", "add", "remove", "revoke", "pending_write"])
def test_query_guards_reject_changes_after_fact_selection(
    tmp_path, service_factory, monkeypatch, change
):
    backend = backend_for(tmp_path, {"a": python_files()})
    service_factory(backend)
    snapshot = handle(backend)
    find(backend, snapshot, "normalize")

    class Guard:
        pending = False

        def guard_read(self, project_id):
            if self.pending:
                raise SourceError("PENDING_WRITE: finish recovery first")

    guard = Guard()
    backend.write_coordinator = guard
    original = module.QUERIES["project_architecture"]

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        root = backend.sources["a"].root
        if change == "edit":
            (root / "Code/pkg/helper.py").write_text("def changed(): return 2\n")
        elif change == "add":
            (root / "new.md").write_text("New inventory entry\n")
        elif change == "remove":
            (root / "Code/pkg/models.py").unlink()
        elif change == "revoke":
            del backend.sources["a"]
        else:
            guard.pending = True
        return result

    monkeypatch.setitem(module.QUERIES, "project_architecture", mutate)
    with pytest.raises(SourceError):
        query(backend, snapshot, "project_architecture")


def test_repeated_edits_replace_current_parse_without_accumulating_versions(
    tmp_path, service_factory
):
    backend = backend_for(tmp_path, {"a": {"a.py": "def value(): return 0\n"}})
    service = service_factory(backend)
    for n in range(15):
        (backend.sources["a"].root / "a.py").write_text(f"def value(): return {n}\n")
        snapshot = handle(backend)
        value = find(backend, snapshot, "value")
        assert (
            f"return {n}"
            in query(backend, snapshot, "read_symbol", symbol_id=value["symbol_id"])["content"]
        )
        assert rows(service, "SELECT COUNT(*) FROM li_parse_cache") == [(1,)]
        assert rows(service, "SELECT DISTINCT revision FROM ci_symbols") == [(1,)]
    assert service.path.stat().st_size < 200_000


def test_index_response_budget_paginated_symbols_and_invalid_arguments(tmp_path, service_factory):
    backend = backend_for(
        tmp_path, {"a": {"a.py": "".join(f"def value_{n}(): return {n}\n" for n in range(80))}}
    )
    service_factory(backend)
    snapshot = handle(backend)
    result = query(backend, snapshot, "symbol_search", query="value", limit=80, max_chars=2000)
    assert result["has_more"] and result["truncated"]
    assert len(json.dumps(result, ensure_ascii=False)) <= 2000
    second = query(
        backend,
        snapshot,
        "symbol_search",
        query="value",
        offset=result["next_offset"],
        limit=80,
        max_chars=2000,
    )
    assert result["symbols"][0]["symbol_id"] != second["symbols"][0]["symbol_id"]
    for operation, parameters in [
        ("unknown", {}),
        ("symbol_search", {"query": ""}),
        ("symbol_search", {"query": "value", "max_chars": 999}),
        ("symbol_search", {"query": "value", "offset": -1}),
        ("file_dependencies", {"path": "../b.py"}),
        ("read_symbol", {"symbol_id": "bad"}),
        (
            "symbol_graph",
            {
                "symbol_id": find(backend, snapshot, "value_0")["symbol_id"],
                "graph": "call",
                "depth": 6,
            },
        ),
    ]:
        with pytest.raises(SourceError):
            query(backend, snapshot, operation, **parameters)


def test_bounded_context_metadata_no_silent_context_replacement(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": {"a.py": "def value(): pass\n"}})
    service = service_factory(backend, max_context_bindings=1)
    first = handle(backend)
    find(backend, first, "value")
    second = handle(backend)
    with pytest.raises(SourceError, match="capacity"):
        query(backend, second, "project_architecture")
    assert find(backend, first, "value")["name"] == "value"
    assert len(service._bindings) == 1
    assert backend.read_file("a", "a.py", second)["content"] == "def value(): pass\n"


def test_context_file_budget_refuses_index_without_breaking_text(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": python_files()})
    backend.contexts = ReadContexts(max_files=1)
    service_factory(backend)
    with pytest.raises(SourceError, match="LIVE_CONTEXT"):
        query(backend, handle(backend), "project_architecture")
    backend.contexts.clear()
    assert "class Base" in backend.read_file("a", "Code/pkg/models.py", handle(backend))["content"]


def test_exclusive_cache_owner_and_close_preserve_files(tmp_path, service_factory):
    backend = backend_for(tmp_path, {"a": {"a.py": "def value(): pass\n"}})
    data_dir = tmp_path / "ownership-data"
    service = service_factory(backend, data_dir=data_dir)
    snapshot = handle(backend)
    find(backend, snapshot, "value")
    with pytest.raises(SourceError, match="INDEX_STORAGE_UNAVAILABLE"):
        LiveIndexService(data_dir)
    service.close()
    service.close()
    assert service.path.exists() and service.status("a")["status"] == "closed"
    with pytest.raises(SourceError, match="INDEX_CLOSED"):
        service.query(backend, "a", snapshot, "project_architecture")
    fresh = service_factory(backend, data_dir=data_dir)
    assert fresh.path == service.path
    find(backend, handle(backend), "value")


def test_index_context_and_parse_reuse_batch_without_holding_index_lock(
    tmp_path, service_factory, monkeypatch
):
    backend = backend_for(tmp_path, {"a": python_files(), "b": {"b.py": "def other(): pass\n"}})
    service = service_factory(backend)
    snapshot = handle(backend)
    original = backend.fingerprint_batch
    scopes = []

    @contextmanager
    def batch(project_id, source=None):
        assert not service._lock._is_owned()
        with original(project_id, source) as current:
            scopes.append(project_id)
            yield current

    monkeypatch.setattr(backend, "fingerprint_batch", batch)
    find(backend, snapshot, "normalize")
    reads = backend.sources["a"].metrics["body_reads"]
    scopes.clear()
    query(backend, snapshot, "project_architecture")
    # Entry validation plus the two complete pre/post fact guards, all outside
    # the index lock; an extra duplicate entry hash pass is unnecessary.
    assert len(scopes) == 3 and set(scopes) == {"a"}
    service.invalidate("a")
    query(backend, snapshot, "project_architecture")
    assert backend.sources["a"].metrics["body_reads"] == reads
    assert backend.sources["b"].metrics["body_reads"] == 0
    assert service.status("a")["stats"]["parsed_files"] == 0
    assert service.status("a")["stats"]["reused_parse_files"] == 4
    assert backend.sources["a"]._fingerprint_batches.stack == []

"""AST retention is selective, bounded and isolated from request graph state."""

import ast
import gc
import json
import threading
import weakref
from pathlib import Path

import pytest

import code_context.dataflow_ast_cache as cache_module
import code_context.live_dataflow as live_flow
from code_context.dataflow_ast_cache import DataflowAstCache
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.project_index import ProjectIndexService
from code_context.source_access import SourceAccess, SourceError

_SOURCE = (
    "def normalize(value, extra=1):\n"
    "    cleaned = value + extra\n"
    "    return cleaned\n\n"
    "def entry(value):\n"
    "    result = normalize(value, extra=2)\n"
    "    return result\n"
)


def parse(
    cache,
    text=_SOURCE,
    *,
    project="sample",
    source="source",
    identity="stable",
    path="flow.py",
    check=lambda: None,
):
    return cache.parse(
        project_id=project,
        source_id=source,
        source_identity=identity,
        path=path,
        text=text,
        check=check,
    )


def retained_cache(**options):
    return DataflowAstCache(min_parse_ms=0, min_source_bytes=0, **options)


def test_default_admission_skips_small_file_even_when_parse_is_slow(monkeypatch):
    clock = iter((0, 1, 2, 3))
    monkeypatch.setattr(cache_module.time, "perf_counter", lambda: next(clock))
    cache = DataflowAstCache()
    first, second = parse(cache), parse(cache)
    assert first is not second
    assert cache.snapshot()["entries"] == cache.snapshot()["hits"] == 0


@pytest.mark.parametrize("parse_ms, admitted", [(4, False), (5, True)])
def test_admission_uses_measured_parse_threshold(monkeypatch, parse_ms, admitted):
    clock = iter((0, parse_ms / 1000))
    monkeypatch.setattr(cache_module.time, "perf_counter", lambda: next(clock))
    cache = DataflowAstCache()
    text = _SOURCE + "# padding\n" * 4000
    first = parse(cache, text)
    assert bool(cache.snapshot()["entries"]) is admitted
    if admitted:
        assert parse(cache, text) is first


def test_same_content_hits_without_retaining_source_text():
    cache = retained_cache()
    first = parse(cache)
    assert parse(cache) is first
    assert cache.snapshot()["hits"] == 1 and cache.snapshot()["entries"] == 1
    entry = next(iter(cache._entries.values()))
    assert set(vars(entry)) == {"tree", "bytes"}
    assert _SOURCE not in str(next(iter(cache._entries.keys())))


@pytest.mark.parametrize("field", ["project", "source", "identity", "path"])
def test_project_source_identity_and_path_do_not_share_trees(field):
    cache = retained_cache()
    original = parse(cache)
    assert parse(cache, **{field: "different"}) is not original
    assert cache.snapshot()["entries"] == 2


def test_changed_source_hash_evicts_old_version_before_reuse():
    cache = retained_cache()
    original = parse(cache)
    different = parse(cache, _SOURCE.replace("extra=2", "extra=3"))
    assert different is not original
    assert cache.snapshot()["entries"] == 1
    assert parse(cache, _SOURCE.replace("extra=2", "extra=3")) is different
    assert parse(cache) is not original


def test_entry_lru_retains_recently_used_tree():
    cache = retained_cache(max_entries=2)
    a, b = parse(cache, path="a.py"), parse(cache, path="b.py")
    assert parse(cache, path="a.py") is a
    parse(cache, path="c.py")
    assert parse(cache, path="a.py") is a
    assert parse(cache, path="b.py") is not b
    assert cache.snapshot()["entries"] == 2


def test_byte_budget_evicts_even_before_entry_limit():
    reference = retained_cache()
    parse(reference)
    size = reference.snapshot()["retained_bytes"]
    cache = retained_cache(max_entries=4, max_bytes=size + 100)
    original = parse(cache, path="a.py")
    parse(cache, path="b.py")
    assert cache.snapshot()["entries"] == 1
    assert cache.snapshot()["retained_bytes"] <= cache.max_bytes
    assert parse(cache, path="a.py") is not original


def test_oversized_retained_ast_still_analyzes_without_caching():
    cache = retained_cache(max_bytes=100)
    tree = parse(cache)
    assert isinstance(tree, ast.Module)
    assert cache.snapshot()["entries"] == cache.snapshot()["retained_bytes"] == 0


def test_large_ast_footprint_accounts_for_node_dictionaries_and_strings():
    cache = retained_cache()
    text = "".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(100))
    tree = parse(cache, text)
    assert cache.snapshot()["retained_bytes"] > len(text.encode()) * 10
    assert cache.snapshot()["retained_bytes"] > tree.__sizeof__()


def test_hit_retains_request_deadline_checks():
    cache = retained_cache()
    tree = parse(cache)
    checks = 0

    def deadline():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise TimeoutError("request expired")

    with pytest.raises(TimeoutError):
        parse(cache, check=deadline)
    assert parse(cache) is tree


def test_failed_parse_is_never_cached_or_replaced_by_old_tree():
    cache = retained_cache()
    parse(cache)
    with pytest.raises(SyntaxError):
        parse(cache, "def broken(:\n")
    assert cache.snapshot()["entries"] == 0


def test_footprint_deadline_propagates_without_admission():
    cache = retained_cache()
    text = "".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(100))
    checks = 0

    def deadline():
        nonlocal checks
        checks += 1
        if checks >= 4:
            raise TimeoutError("request expired")

    with pytest.raises(TimeoutError):
        parse(cache, text, check=deadline)
    assert cache.snapshot()["entries"] == 0


def test_optional_accounting_allocation_failure_preserves_parsed_tree(monkeypatch):
    cache = retained_cache()

    def exhausted(*args):
        raise MemoryError

    monkeypatch.setattr(cache_module, "_footprint", exhausted)
    assert isinstance(parse(cache), ast.Module)
    assert cache.snapshot()["entries"] == 0


def test_admission_respects_ast_occurrence_budget(monkeypatch):
    cache = retained_cache()
    monkeypatch.setattr(cache_module, "MAX_FILE_NODES", 20)
    text = "def f(x):\n    return x + x + x + x + x + x + x + x\n"
    tree = parse(cache, text)
    assert len(list(ast.walk(tree))) > 20
    assert cache.snapshot()["entries"] == 0


@pytest.mark.parametrize("action", ["clear", "close", "evict_project"])
def test_clear_or_close_cannot_be_repopulated_by_inflight_parse(monkeypatch, action):
    cache = retained_cache()
    entered, release = threading.Event(), threading.Event()
    original = cache_module.ast.parse
    returned, failures = [], []

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    def worker():
        try:
            returned.append(parse(cache))
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(cache_module.ast, "parse", paused)
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=3)
    if action == "evict_project":
        cache.evict_project("sample")
    else:
        getattr(cache, action)()
    release.set()
    thread.join(timeout=3)
    assert not thread.is_alive() and not failures and isinstance(returned[0], ast.Module)
    assert cache.snapshot()["entries"] == cache.snapshot()["retained_bytes"] == 0
    assert cache.snapshot()["closed"] is (action == "close")


def test_concurrent_misses_do_not_wait_for_cache_or_duplicate_retention(monkeypatch):
    cache = retained_cache()
    barrier = threading.Barrier(2)
    original = cache_module.ast.parse
    returned, failures = [], []

    def parallel(*args, **kwargs):
        barrier.wait(timeout=3)
        return original(*args, **kwargs)

    def worker():
        try:
            returned.append(parse(cache))
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(cache_module.ast, "parse", parallel)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert not failures and len(returned) == 2
    assert ast.dump(returned[0]) == ast.dump(returned[1])
    assert cache.snapshot()["entries"] == cache.snapshot()["admitted"] == 1


def test_eviction_project_scope_and_clear_release_trees():
    cache = retained_cache()
    first = parse(cache, project="first")
    second = parse(cache, project="second")
    reference = weakref.ref(first)
    del first
    cache.evict_project("first")
    gc.collect()
    assert reference() is None and parse(cache, project="second") is second
    reference = weakref.ref(second)
    del second
    cache.clear()
    gc.collect()
    assert reference() is None and cache.snapshot()["entries"] == 0


@pytest.fixture
def live_parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "flow.py").write_text(_SOURCE)
    backend = LiveQueries({"sample": SourceAccess(root)})
    service = LiveIndexService(tmp_path / "index")
    service.dataflow_ast_cache = retained_cache()
    backend.index_service = service
    handle = backend.resolve_snapshot("sample")[0]
    ready = backend.code_query("sample", handle, "symbol_search", query="entry", exact=True)
    assert not ready.get("index_not_ready")
    yield root, backend, service, handle
    service.close()
    backend.close()


def query(parts, mode="data_impact", **options):
    _, backend, _, handle = parts
    selector = {
        "path": "flow.py",
        "qualname": "entry",
        "value": {"kind": "return"}
        if mode == "data_origin"
        else {"kind": "parameter", "name": "value"},
    }
    target = {"path": "flow.py", "qualname": "entry", "value": {"kind": "return"}}
    return backend.code_query(
        "sample",
        handle,
        "analyze_code",
        mode=mode,
        selector=selector,
        target=target if mode == "data_path" else None,
        max_depth=2,
        **options,
    )


def test_all_modes_cached_results_equal_fresh_without_mutating_ast(live_parts):
    root, _, service, _ = live_parts
    cache = service.dataflow_ast_cache
    golden = {}
    service.dataflow_ast_cache = None
    for mode in ("data_origin", "data_impact", "data_path", "combined"):
        golden[mode] = query(live_parts, mode)
    service.dataflow_ast_cache = cache
    for mode, expected in golden.items():
        assert query(live_parts, mode) == expected
    tree = next(iter(cache._entries.values())).tree
    before = ast.dump(tree, include_attributes=True)
    for mode, expected in golden.items():
        assert query(live_parts, mode) == expected
    assert ast.dump(tree, include_attributes=True) == before
    assert cache.snapshot()["hits"] >= 7
    assert (root / "flow.py").read_text() == _SOURCE


def test_cache_hit_keeps_real_source_reads_and_final_guard(live_parts, monkeypatch):
    _, _, service, _ = live_parts
    query(live_parts)
    reads, guards, parses = [], [], []
    original_read, original_guard, original_parse = SourceAccess.read, service._guard, ast.parse

    def read(source, path):
        reads.append(path)
        return original_read(source, path)

    def guard(*args):
        guards.append(1)
        return original_guard(*args)

    def physical_parse(*args, **kwargs):
        if kwargs.get("filename") == "<CoLink Python data flow>":
            parses.append(1)
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(SourceAccess, "read", read)
    monkeypatch.setattr(service, "_guard", guard)
    monkeypatch.setattr(ast, "parse", physical_parse)
    assert query(live_parts)["analysis_status"] == "ready"
    assert reads == ["flow.py"] and guards == [1, 1] and not parses


def test_source_edit_rejects_old_context_then_rebuilds_changed_flow(live_parts):
    root, backend, service, handle = live_parts
    assert query(live_parts, "data_path")["path_found"]
    cached = next(iter(service.dataflow_ast_cache._entries.values())).tree
    (root / "flow.py").write_text(
        _SOURCE.replace("result = normalize(value, extra=2)", "result = 0")
    )
    with pytest.raises(SourceError, match="CHANGED|INVALID"):
        query(live_parts, "data_path")
    fresh = backend.resolve_snapshot("sample")[0]
    assert fresh != handle
    result = query((root, backend, service, fresh), "data_path")
    assert result["analysis_status"] == "ready" and not result["path_found"]
    assert next(iter(service.dataflow_ast_cache._entries.values())).tree is not cached


@pytest.mark.parametrize("change", ["source", "publication"])
def test_hit_result_never_escapes_changed_source_or_publication(live_parts, monkeypatch, change):
    root, _, service, _ = live_parts
    query(live_parts)
    original = live_flow.analyze_dataflow

    def compute(**options):
        result = original(**options)
        if change == "source":
            (root / "flow.py").write_text("def entry(value):\n    return 0\n")
        else:
            service.invalidate("sample")
        return result

    monkeypatch.setattr(live_flow, "analyze_dataflow", compute)
    with pytest.raises(SourceError, match="CHANGED|INVALID"):
        query(live_parts)


def test_request_result_mutation_does_not_mutate_retained_ast_or_next_result(live_parts):
    first = query(live_parts)
    golden = json.loads(json.dumps(first))
    first["nodes"][0]["name"] = "caller changed returned graph"
    first["edges"].clear()
    assert query(live_parts) == golden


def test_structure_query_never_allocates_dataflow_ast(live_parts):
    _, backend, service, handle = live_parts
    result = backend.code_query(
        "sample",
        handle,
        "analyze_code",
        mode="symbol_context",
        selector={"path": "flow.py", "qualname": "entry"},
    )
    assert result["analysis_status"] == "ready"
    assert service.dataflow_ast_cache.snapshot()["entries"] == 0


def test_no_persistent_cache_files_or_tables_are_added(live_parts):
    _, _, service, _ = live_parts
    before = [tuple(row) for row in service._db.execute("SELECT type,name,sql FROM sqlite_master")]
    files = set(Path(service.path.parent).iterdir())
    query(live_parts)
    query(live_parts)
    assert before == [
        tuple(row) for row in service._db.execute("SELECT type,name,sql FROM sqlite_master")
    ]
    assert files == set(Path(service.path.parent).iterdir())


@pytest.fixture
def pool_parts(tmp_path):
    sources = {}
    for project in ("a", "b"):
        source = tmp_path / project
        source.mkdir()
        (source / "flow.py").write_text(_SOURCE)
        sources[project] = SourceAccess(source)
    backend = LiveQueries(sources)
    pool = ProjectIndexService(tmp_path / "pool", max_open_projects=2)
    pool.dataflow_ast_cache = retained_cache()
    backend.index_service = pool
    yield backend, pool
    pool.close()
    backend.close()


def project_flow(backend, project):
    return backend.code_query(
        project,
        None,
        "analyze_code",
        mode="data_impact",
        selector={
            "path": "flow.py",
            "qualname": "entry",
            "value": {"kind": "parameter", "name": "value"},
        },
        max_depth=2,
    )


def test_workspace_cache_is_shared_and_idle_trim_retains_busy_project(pool_parts):
    backend, pool = pool_parts
    project_flow(backend, "a")
    project_flow(backend, "b")
    assert pool._entries["a"]["service"].dataflow_ast_cache is pool.dataflow_ast_cache
    assert pool._entries["b"]["service"].dataflow_ast_cache is pool.dataflow_ast_cache
    assert pool.dataflow_ast_cache.snapshot()["entries"] == 2
    with pool._lease("a"):
        assert pool.trim_idle() == 1
        assert pool.dataflow_ast_cache.snapshot()["entries"] == 1
        assert all(key[0] == "a" for key in pool.dataflow_ast_cache._entries)
    assert pool.trim_idle() == 1
    assert pool.dataflow_ast_cache.snapshot()["entries"] == 0
    assert pool.dataflow_ast_cache.snapshot()["retained_bytes"] == 0
    assert pool.path_for("a").is_file() and pool.path_for("b").is_file()
    assert not pool.dataflow_ast_cache.snapshot()["closed"]


def test_project_invalidation_evicts_only_requested_project(pool_parts):
    backend, pool = pool_parts
    project_flow(backend, "a")
    project_flow(backend, "b")
    pool.invalidate("a")
    assert pool.dataflow_ast_cache.snapshot()["entries"] == 1
    assert all(key[0] == "b" for key in pool.dataflow_ast_cache._entries)
    assert pool.path_for("a").is_file() and pool.path_for("b").is_file()


def test_lru_project_close_drops_its_ast_without_closing_workspace_cache(pool_parts):
    backend, pool = pool_parts
    pool.max_open_projects = 1
    project_flow(backend, "a")
    project_flow(backend, "b")
    assert list(pool._entries) == ["b"]
    assert pool.dataflow_ast_cache.snapshot()["entries"] == 1
    assert all(key[0] == "b" for key in pool.dataflow_ast_cache._entries)
    assert not pool.dataflow_ast_cache.snapshot()["closed"]
    assert pool.path_for("a").is_file()


def test_workspace_close_disables_cache_and_reopen_has_no_retained_ast(pool_parts):
    backend, pool = pool_parts
    project_flow(backend, "a")
    cache = pool.dataflow_ast_cache
    assert cache.snapshot()["entries"] == 1
    pool.close()
    assert cache.snapshot()["closed"] and cache.snapshot()["entries"] == 0
    reopened = ProjectIndexService(pool.root)
    try:
        backend.index_service = reopened
        assert reopened.dataflow_ast_cache.snapshot()["entries"] == 0
        assert reopened.status("a")["status"] == "cached"
        assert project_flow(backend, "a")["analysis_status"] == "ready"
        assert reopened.dataflow_ast_cache.snapshot()["entries"] == 0  # Small source stays local.
    finally:
        reopened.close()

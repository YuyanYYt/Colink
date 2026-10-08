"""Per-project disk isolation, lazy ownership and bounded connection reuse."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.project_index import ProjectIndexService
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def pool_factory(tmp_path):
    pools = []

    def create(**options):
        directory = options.pop("data_dir", tmp_path / f"index-{len(pools)}")
        pool = ProjectIndexService(directory, **options)
        pools.append(pool)
        return pool

    yield create
    for pool in pools:
        pool.close()


def backend_for(tmp_path, names):
    sources = {}
    for name in names:
        root = tmp_path / name
        root.mkdir()
        (root / "main.py").write_text(f"def value(): return '{name}'\n")
        sources[name] = SourceAccess(root)
    return LiveQueries(sources)


def query(backend, project):
    result = backend.code_query(project, None, "symbol_search", query="value", exact=True)
    assert result.get("total") == 1 and not result.get("index_not_ready"), result
    return result


def test_each_project_owns_current_database_and_unrequested_sources_remain_lazy(
    tmp_path, pool_factory
):
    backend = backend_for(tmp_path, ("a", "b"))
    pool = pool_factory()
    backend.index_service = pool
    assert pool.status("a")["status"] == "not_requested"
    assert not pool.path_for("a").exists()
    query(backend, "a")
    assert not pool.path_for("b").exists()
    assert backend.sources["b"].metrics["body_reads"] == 0
    query(backend, "b")
    assert pool.path_for("a") != pool.path_for("b")
    for project in ("a", "b"):
        with sqlite3.connect(pool.path_for(project)) as db:
            assert db.execute("SELECT project_id FROM li_projects").fetchall() == [(project,)]
            assert db.execute("SELECT DISTINCT revision FROM files").fetchall() == [(1,)]
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        limits = pool.status(project)["limits"]
        assert limits["database_page_ceiling_bytes"] == 500 * 1024**2
        assert limits["open_project_limit"] == 4 and limits["cached_projects"] == 1
        assert limits["storage_layout"] == "one_database_per_project"
    (backend.sources["a"].root / "main.py").write_text("def value(): return 2\n")
    query(backend, "a")
    with sqlite3.connect(pool.path_for("a")) as db:
        assert db.execute("SELECT count(*) FROM li_projects").fetchone() == (1,)


def test_lru_closes_connections_retains_databases_and_reuses_after_restart(tmp_path, pool_factory):
    backend = backend_for(tmp_path, ("a", "b", "c"))
    pool = pool_factory(max_open_projects=2)
    backend.index_service = pool
    for project in ("a", "b", "c"):
        query(backend, project)
    assert list(pool._entries) == ["b", "c"]
    assert pool.path_for("a").exists() and pool.status("a")["status"] == "cached"
    before = backend.sources["a"].metrics["body_reads"]
    query(backend, "a")
    assert backend.sources["a"].metrics["body_reads"] == before
    assert list(pool._entries) == ["c", "a"]
    assert pool.status("a")["stats"]["parsed_files"] == 1  # No rebuild, persisted build stats.
    directory = pool.root
    pool.close()
    reopened = pool_factory(data_dir=directory, max_open_projects=2)
    backend.index_service = reopened
    assert reopened.status("a")["status"] == "cached"
    assert reopened.status("a")["requires_validation"]
    (backend.sources["a"].root / "main.py").write_text("def value(): return 3\n")
    query(backend, "a")
    assert reopened.status("a")["stats"]["parsed_files"] == 1


def test_busy_project_is_pinned_and_capacity_failure_does_not_read_other_source(
    tmp_path, pool_factory, monkeypatch
):
    backend = backend_for(tmp_path, ("a", "b"))
    pool = pool_factory(max_open_projects=1)
    backend.index_service = pool
    started, release = Event(), Event()
    original = LiveIndexService._extract

    def extract(self, *args):
        started.set()
        assert release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(LiveIndexService, "_extract", extract)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(query, backend, "a")
            assert started.wait(3)
            blocked = backend.code_query("b", None, "symbol_search", query="value")
            assert blocked["reason"] == "BUILD_QUEUE_LIMIT"
            assert len(pool._entries) == 1 and not pool.path_for("b").exists()
            assert backend.sources["b"].metrics["body_reads"] == 0
            release.set()
            assert first.result(timeout=5)["total"] == 1
        query(backend, "b")
    finally:
        release.set()


def test_old_shared_database_is_preserved_and_new_project_cache_is_reconstructed(
    tmp_path, pool_factory
):
    backend = backend_for(tmp_path, ("a", "b"))
    directory = tmp_path / "old-shared"
    old = LiveIndexService(directory)
    backend.index_service = old
    query(backend, "a")
    query(backend, "b")
    old.close()
    old_hash = hashlib.sha256(old.path.read_bytes()).hexdigest()
    pool = pool_factory(data_dir=directory)
    backend.index_service = pool
    query(backend, "a")
    assert hashlib.sha256(old.path.read_bytes()).hexdigest() == old_hash
    assert not pool.path_for("b").exists()
    with sqlite3.connect(pool.path_for("a")) as db:
        assert db.execute("SELECT project_id FROM li_projects").fetchall() == [("a",)]


def test_unknown_project_never_creates_cache_and_symlink_is_refused(tmp_path, pool_factory):
    backend = backend_for(tmp_path, ("a",))
    pool = pool_factory()
    backend.index_service = pool
    with pytest.raises(SourceError, match="PROJECT_NOT_AUTHORIZED"):
        backend.code_query("unknown", None, "symbol_search", query="value")
    assert not (pool.root / "projects").exists()
    destination = pool.path_for("a")
    destination.parent.mkdir(parents=True, mode=0o700)
    destination.symlink_to(tmp_path / "outside.sqlite3")
    with pytest.raises(SourceError, match="INDEX_STORAGE_UNAVAILABLE"):
        query(backend, "a")
    assert not (tmp_path / "outside.sqlite3").exists()


def test_pool_exclusive_owner_and_close_are_recoverable(tmp_path, pool_factory):
    pool = pool_factory()
    with pytest.raises(SourceError, match="INDEX_STORAGE_UNAVAILABLE"):
        ProjectIndexService(pool.root)
    pool.close()
    assert pool.root.is_dir()
    with pytest.raises(SourceError, match="INDEX_CLOSED"):
        pool.query(backend_for(tmp_path, ("a",)), "a", None, "symbol_search", query="value")
    assert pool_factory(data_dir=pool.root).status("a")["status"] == "not_requested"

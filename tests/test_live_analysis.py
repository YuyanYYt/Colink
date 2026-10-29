"""Advanced reads share live guards and never hold locks for Python traversal."""

import json
from threading import Thread

import pytest

import code_context.intelligence_analysis as analysis
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "module.py").write_text(
        "def helper(value):\n    return value + 1\n"
        "class Service:\n    def run(self, value):\n        return helper(value)\n"
    )
    backend = LiveQueries({"sample": SourceAccess(root)})
    service = LiveIndexService(tmp_path / "index")
    backend.index_service = service
    handle = backend.resolve_snapshot("sample")[0]
    ready = backend.code_query("sample", handle, "symbol_search", query="helper", exact=True)
    assert not ready.get("index_not_ready"), ready
    yield root, backend, service, handle
    service.close()
    backend.close()


def query(backend, handle, **options):
    return backend.code_query(
        "sample",
        handle,
        "analyze_code",
        mode="file_context",
        selector={"path": "module.py"},
        **options,
    )


def test_composite_uses_one_pre_post_guard_and_detached_selects(parts, monkeypatch):
    _, backend, service, handle = parts
    original_guard, original_analysis = service._guard, analysis.analyze_structure
    guards = []
    acquired = []

    def guard(*args):
        guards.append(1)
        return original_guard(*args)

    def run(*args, **kwargs):
        def check_lock():
            success = service._lock.acquire(timeout=0.5)
            acquired.append(success)
            if success:
                service._lock.release()

        thread = Thread(target=check_lock)
        thread.start()
        thread.join(timeout=1)
        return original_analysis(*args, **kwargs)

    monkeypatch.setattr(service, "_guard", guard)
    monkeypatch.setattr(analysis, "analyze_structure", run)
    result = query(backend, handle, include=["ownership", "calls", "dependencies"])
    assert result["source_mode"] == "live"
    assert result["snapshot"] == handle
    assert result["symbols"]
    assert guards == [1, 1]
    assert acquired == [True]


def test_partial_index_cannot_claim_complete_analysis(parts):
    _, backend, service, handle = parts
    with service._lock:
        stats = json.loads(service._cached("sample")["stats"])
        stats["partial"] = True
        service._db.execute(
            "UPDATE li_projects SET stats=? WHERE project_id=?", (json.dumps(stats), "sample")
        )
        service._db.commit()
    result = query(backend, handle)
    assert result["index_partial"] is True
    assert result["analysis_status"] == "partial"


def test_source_change_after_analysis_cannot_publish_old_view(parts, monkeypatch):
    root, backend, service, handle = parts
    original = analysis.analyze_structure

    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / "module.py").write_text("def changed():\n    return 42\n")
        return result

    monkeypatch.setattr(analysis, "analyze_structure", change)
    with pytest.raises(SourceError, match="CHANGED|INVALID"):
        query(backend, handle)
    assert service.status("sample")["status"] == "dirty"


def test_publication_invalidation_cannot_publish_completed_analysis(parts, monkeypatch):
    _, backend, service, handle = parts
    original = analysis.analyze_structure

    def invalidate(*args, **kwargs):
        result = original(*args, **kwargs)
        service.invalidate("sample")
        return result

    monkeypatch.setattr(analysis, "analyze_structure", invalidate)
    with pytest.raises(SourceError, match="LIVE_INDEX_CHANGED"):
        query(backend, handle)


def test_sql_budget_returns_partial_and_restores_progress_handler(parts, monkeypatch):
    _, backend, service, handle = parts

    def expensive(db, *args, **kwargs):
        db.execute(
            "WITH RECURSIVE numbers(n) AS (SELECT 0 UNION ALL "
            "SELECT n+1 FROM numbers WHERE n<100000000) SELECT sum(n) FROM numbers"
        ).fetchone()
        pytest.fail("SQL must be interrupted by its request deadline")

    monkeypatch.setattr(analysis, "_analyze_structure", expensive)
    result = query(backend, handle, max_ms=100)
    assert result["analysis_status"] == "partial"
    assert result["truncated_reason"] == ["time_budget"]
    assert result["nodes"] == result["edges"] == []
    assert service.status("sample")["status"] == "ready"
    assert service._db.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.parametrize("value", [True, 0, 99, 10001, "3000"])
def test_invalid_analysis_budget_is_safe(parts, value):
    _, backend, _, handle = parts
    with pytest.raises(SourceError, match="INVALID_ANALYSIS_BUDGET"):
        query(backend, handle, max_ms=value)


def test_unknown_project_and_expired_context_remain_denied(parts):
    _, backend, _, handle = parts
    with pytest.raises(SourceError):
        backend.code_query(
            "not-authorized",
            handle,
            "analyze_code",
            mode="file_context",
            selector={"path": "module.py"},
        )
    backend.contexts.invalidate_project("sample")
    with pytest.raises(SourceError):
        query(backend, handle)


def test_response_capacity_is_not_an_invalid_query_or_cache_invalidation(parts, monkeypatch):
    _, backend, service, handle = parts

    def oversized(*args, **kwargs):
        return {"mode": "file_context", "symbol": {"qualname": "private_input" * 200}}

    monkeypatch.setattr(analysis, "analyze_structure", oversized)
    with pytest.raises(SourceError, match="^ANALYSIS_RESPONSE_BUDGET_EXCEEDED:") as failure:
        query(backend, handle, max_chars=1000)
    assert "private_input" not in str(failure.value)
    assert service.status("sample")["status"] == "ready"

"""Search pagination binds source/query/context and reports real resource limits."""

import json
import time
from types import SimpleNamespace

import pytest

import code_context.read_search as module
from code_context.live import LiveQueries
from code_context.read_code import execute_read_code
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def searcher(tmp_path):
    roots = {}
    for project in ("a", "b"):
        root = tmp_path / project
        root.mkdir()
        (root / "hits.txt").write_text("needle " + "x" * 900 + "\n" + "needle second\n")
        roots[project] = SourceAccess(root)
    backend = LiveQueries(roots)
    yield backend
    backend.close()


def search(backend, request=None, snapshot=None, project="a", **options):
    return execute_read_code(
        backend,
        project,
        request or {"mode": "search", "kind": "text", "query": "needle"},
        snapshot,
        display_name="search fixture",
        **options,
    )


def test_text_search_total_json_budget_pages_every_matching_line(searcher):
    root = searcher.source("a").root
    (root / "large.txt").write_text(
        "\n".join("needle " + '\\"' * 700 + f" {i}" for i in range(240)) + "\n"
    )
    request = {
        "mode": "search",
        "kind": "text",
        "query": "needle",
        "path_prefix": "large.txt",
        "limit": 200,
    }
    snapshot, positions = None, []
    while True:
        result = search(searcher, request, snapshot, max_chars=2000)
        assert len(json.dumps(result, ensure_ascii=False)) <= 2000
        assert result["display_name"] == "search fixture"
        positions.extend((item["path"], item["line"]) for item in result["matches"])
        snapshot = result["snapshot"]
        if not result["has_more"]:
            assert result["search_complete"] is True
            break
        request = {**request, "cursor": result["next_cursor"]}
    assert positions == [("large.txt", line) for line in range(1, 241)]


def test_text_context_is_narrow_and_lf_lines_keep_unicode_separator(searcher):
    root = searcher.source("a").root
    (root / "unicode.txt").write_text("before\nvalue='one\u2028needle'\nafter\n")
    result = search(
        searcher,
        {
            "mode": "search",
            "kind": "text",
            "query": "needle",
            "path_prefix": "unicode.txt",
            "context_lines": 1,
        },
    )
    match = result["matches"][0]
    assert match["line"] == 2
    assert match["text"] == "value='one\u2028needle'"
    assert match["context"]["content"] == "before\nvalue='one\u2028needle'\nafter\n"


def test_many_matches_encode_the_accumulated_payload_and_cursor_only_at_return(
    searcher, monkeypatch
):
    (searcher.source("a").root / "many.txt").write_text(
        "".join(f"needle {number}\n" for number in range(300))
    )
    whole_payload_sizes, cursors = [], []
    original_chars, original_cursor = module.json_chars, module.encode_search_cursor

    def measured(value):
        if isinstance(value, dict) and isinstance(value.get("matches"), list):
            whole_payload_sizes.append(len(value["matches"]))
        return original_chars(value)

    def signed(*args, **kwargs):
        cursors.append(1)
        return original_cursor(*args, **kwargs)

    monkeypatch.setattr(module, "json_chars", measured)
    monkeypatch.setattr(module, "encode_search_cursor", signed)
    result = search(
        searcher,
        {
            "mode": "search",
            "kind": "text",
            "query": "needle",
            "path_prefix": "many.txt",
            "limit": 100,
        },
        max_chars=50_000,
    )
    assert [match["line"] for match in result["matches"]] == list(range(1, 101))
    assert result["has_more"] is True
    assert [size for size in whole_payload_sizes if size] == [100]
    assert len(cursors) == 1


@pytest.mark.parametrize("max_chars", [1000, 1600, 3000, 6000])
def test_incremental_budget_keeps_escaped_context_pages_complete(searcher, max_chars):
    (searcher.source("a").root / "escaped.txt").write_text(
        "".join(f'needle 中\\"\t {number} ' + '\\"' * 70 + "\n" for number in range(40))
    )
    request = {
        "mode": "search",
        "kind": "text",
        "query": "needle",
        "path_prefix": "escaped.txt",
        "context_lines": 1,
        "limit": 200,
    }
    snapshot, lines = None, []
    for _ in range(100):
        result = search(searcher, request, snapshot, max_chars=max_chars)
        assert len(json.dumps(result, ensure_ascii=False)) <= max_chars
        lines.extend(match["line"] for match in result["matches"])
        if not result["has_more"]:
            assert result["search_complete"]
            break
        assert result["matches"], "a successful page must advance beyond one matching line"
        snapshot = result["snapshot"]
        request = {**request, "cursor": result["next_cursor"]}
    else:
        pytest.fail("search pages did not finish")
    assert lines == list(range(1, 41))


@pytest.mark.parametrize(
    "change",
    [
        {"query": "second"},
        {"path_prefix": "hits.txt"},
        {"context_lines": 1},
        {"kind": "symbol"},
    ],
)
def test_cursor_rejects_changed_search_fields(searcher, change):
    result = search(searcher, {"mode": "search", "kind": "text", "query": "needle", "limit": 1})
    request = {
        "mode": "search",
        "kind": "text",
        "query": "needle",
        "cursor": result["next_cursor"],
        **change,
    }
    with pytest.raises(SourceError, match="INVALID_READ_CURSOR"):
        search(searcher, request, result["snapshot"])


def test_cursor_rejects_cross_project_new_context_and_tampering(searcher):
    result = search(searcher, {"mode": "search", "kind": "text", "query": "needle", "limit": 1})
    request = {"mode": "search", "kind": "text", "query": "needle", "cursor": result["next_cursor"]}
    with pytest.raises(SourceError, match="READ_CURSOR_CONTEXT_REQUIRED"):
        search(searcher, request)
    fresh = searcher.resolve_snapshot("a")[0]
    with pytest.raises(SourceError, match="INVALID_READ_CURSOR"):
        search(searcher, request, fresh)
    other = searcher.resolve_snapshot("b")[0]
    with pytest.raises(SourceError, match="INVALID_READ_CURSOR"):
        search(searcher, request, other, project="b")
    damaged = request["cursor"][:-1] + ("a" if request["cursor"][-1] != "a" else "b")
    with pytest.raises(SourceError, match="INVALID_READ_CURSOR"):
        search(searcher, {**request, "cursor": damaged}, result["snapshot"])


def test_cursor_rejects_inventory_addition_without_silently_skipping_files(searcher):
    result = search(searcher, {"mode": "search", "kind": "text", "query": "needle", "limit": 1})
    (searcher.source("a").root / "added.txt").write_text("needle new\n")
    with pytest.raises(SourceError, match="READ_SEARCH_CHANGED"):
        search(
            searcher,
            {"mode": "search", "kind": "text", "query": "needle", "cursor": result["next_cursor"]},
            result["snapshot"],
        )


def test_cursor_reuse_is_semantically_stable_and_page_size_can_change(searcher):
    first = search(searcher, {"mode": "search", "kind": "text", "query": "needle", "limit": 1})
    request = {
        "mode": "search",
        "kind": "text",
        "query": "needle",
        "cursor": first["next_cursor"],
        "limit": 200,
    }
    second = search(searcher, request, first["snapshot"])
    retry = search(searcher, request, first["snapshot"])
    assert second["matches"] == retry["matches"]
    assert second["matches"][0]["line"] == 2
    assert second["has_more"] is retry["has_more"] is False


def test_expired_search_cursor_is_rejected_even_before_context_ttl(searcher, monkeypatch):
    first = search(searcher, {"mode": "search", "kind": "text", "query": "needle", "limit": 1})
    future = time.monotonic() + 901
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: future))
    with pytest.raises(SourceError, match="INVALID_READ_CURSOR"):
        search(
            searcher,
            {"mode": "search", "kind": "text", "query": "needle", "cursor": first["next_cursor"]},
            first["snapshot"],
        )


def test_scan_byte_limit_returns_bound_continuation_and_partial_coverage(searcher, monkeypatch):
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 10)
    first = search(searcher)
    assert first["matches"] == []
    assert first["search_partial"] is True
    assert first["has_more"] is True
    assert first["truncated_reason"] == ["max_bytes"]
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 10_000)
    resumed = search(
        searcher,
        {"mode": "search", "kind": "text", "query": "needle", "cursor": first["next_cursor"]},
        first["snapshot"],
    )
    assert len(resumed["matches"]) == 2
    assert resumed["search_complete"] is True


def test_text_growth_between_discovery_and_read_is_rejected_before_publication(
    searcher, monkeypatch
):
    source = searcher.source("a")
    original = source.read

    def changed(path):
        (source.root / path).write_text("needle changed after discovery\n")
        return original(path)

    monkeypatch.setattr(source, "read", changed)
    with pytest.raises(SourceError, match="READ_SEARCH_CHANGED"):
        search(searcher)


def test_cooperative_scan_deadline_returns_partial_without_reading_source(searcher, monkeypatch):
    now = time.monotonic()
    times = iter(now + step * 0.06 for step in range(100))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(times)))
    called = []
    original = searcher.source("a").read

    def tracked(path):
        called.append(path)
        return original(path)

    monkeypatch.setattr(searcher.source("a"), "read", tracked)
    result = search(searcher, max_ms=100)
    assert not called
    assert result["search_partial"] is True
    assert result["search_complete"] is False
    assert result["truncated_reason"] == ["max_ms"]
    assert result["next_cursor"]

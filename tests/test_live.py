import asyncio
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from code_context.live import LiveQueries
from code_context.project_registry import ProjectRegistry
from code_context.read_context import ContextError
from code_context.server import build_mcp
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def backend(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "a.py").write_text("before\n")
    (tmp_path / "b" / "b.py").write_text("other project\n")
    return LiveQueries({"a": SourceAccess(tmp_path / "a"), "b": SourceAccess(tmp_path / "b")})


def test_overview_never_mirrors_bodies_and_read_is_current(backend):
    handle, _ = backend.resolve_snapshot("a")
    result = backend.repo_overview("a", handle)
    assert result["source_mode"] == "live" and not result["history_available"]
    assert backend.sources["a"].metrics["body_reads"] == 0
    assert backend.sources["b"].metrics["body_reads"] == 0
    assert backend.read_file("a", "a.py", handle)["content"] == "before\n"
    file = backend.sources["a"].root / "a.py"
    file.write_text("after\n")
    with pytest.raises(SourceError, match="LIVE_CONTEXT_INVALID"):
        backend.read_file("a", "a.py", handle)
    fresh, _ = backend.resolve_snapshot("a")
    assert backend.read_file("a", "a.py", fresh)["content"] == "after\n"
    assert backend.sources["b"].metrics["body_reads"] == 0


def test_project_context_and_history_never_silently_fall_back(backend):
    handle, _ = backend.resolve_snapshot("a")
    with pytest.raises(SourceError):
        backend.read_file("b", "b.py", handle)
    for selector in ["previous", "snap_" + "0" * 32, "live_unknown"]:
        with pytest.raises(SourceError):
            backend.resolve_snapshot("a", selector)
    with pytest.raises(SourceError, match="LIVE_HISTORY_UNAVAILABLE"):
        backend.get_recent_diff("a", handle)
    with pytest.raises(SourceError, match="PROJECT_NOT_AUTHORIZED"):
        backend.resolve_snapshot("unknown")


def test_search_and_pagination_are_source_bounded(backend):
    source = backend.sources["a"]
    (source.root / "long.py").write_text("x" * 4000 + "\n")
    handle, _ = backend.resolve_snapshot("a")
    result = backend.search_code("a", "before", handle)
    assert result["matches"][0]["path"] == "a.py"
    first = backend.read_file("a", "long.py", handle, max_chars=1000)
    second = backend.read_file(
        "a",
        "long.py",
        handle,
        start_line=first["next_start_line"],
        max_chars=1000,
        char_offset=first["next_char_offset"],
    )
    assert len(first["content"] + second["content"]) == 2000
    assert backend.sources["b"].metrics["body_reads"] == 0


def test_live_read_search_lines_match_precise_edit_lines(backend):
    from code_context.text_edits import apply_text_edit

    original = 'value = "first\u2028second"\nTARGET = 1\n'
    (backend.sources["a"].root / "a.py").write_text(original)
    result = backend.search_code("a", "TARGET")
    assert result["matches"][0]["line"] == 2
    page = backend.read_file("a", "a.py", start_line=2, end_line=2)
    assert page["content"] == "TARGET = 1\n" and page["total_lines"] == 2
    edited = apply_text_edit(
        original,
        {
            "kind": "replace_lines",
            "start_line": 2,
            "end_line": 2,
            "old_text": page["content"],
            "new_text": "TARGET = 2\n",
        },
    )
    assert edited.content == original.replace("TARGET = 1", "TARGET = 2")


def test_live_mcp_keeps_read_annotations_and_explains_contexts(backend):
    async def inspect():
        mcp = build_mcp(
            backend, project_names={"a": "A", "b": "B"}, status_provider=backend.mcp_status
        )
        tools = await mcp.list_tools()
        assert len(tools) == 15
        assert all(tool.annotations.read_only_hint for tool in tools)
        overview = next(tool for tool in tools if tool.name == "repo_overview")
        assert "not an immutable historical" in overview.description
        result = await mcp.call_tool("repo_overview", {"project_id": "a"})
        assert not result.is_error
        assert result.structured_content["source_mode"] == "live"

    asyncio.run(inspect())


def test_live_stdio_reads_new_saved_content_without_source_database(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    file = root / "a.py"
    file.write_text("before\n")
    data = tmp_path / "data"

    async def inspect():
        process = StdioServerParameters(
            command=sys.executable,
            args=[
                "-B",
                "-m",
                "code_context",
                "live",
                "--root",
                str(root),
                "--project",
                "sample",
                "--data-dir",
                str(data),
            ],
        )
        async with stdio_client(process) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=10) as session:
                initialized = await session.initialize()
                assert "not immutable historical" in initialized.instructions
                overview = await session.call_tool("repo_overview", {"project_id": "sample"})
                snapshot = overview.structured_content["snapshot"]
                before = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "a.py", "snapshot": snapshot}
                )
                assert before.structured_content["content"] == "before\n"
                file.write_text("after\n")
                stale = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "a.py", "snapshot": snapshot}
                )
                assert stale.is_error
                fresh = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "a.py"}
                )
                assert fresh.structured_content["content"] == "after\n"

    asyncio.run(inspect())
    assert not data.exists()


def test_backend_sources_share_one_bounded_metadata_cache(backend):
    cache = backend.fingerprint_cache
    a, b = backend.source("a"), backend.source("b")
    assert a._fingerprints.cache is b._fingerprints.cache is cache
    a.fingerprint("a.py")
    b.fingerprint("b.py")
    assert len(cache) == 2 and len(a._fingerprints) == len(b._fingerprints) == 1
    assert cache.stats()["max_entries"] == 50_000
    assert cache.stats()["max_charged_bytes"] == 32 * 1024 * 1024
    backend.clear()
    assert len(cache) == 0 and a.metrics["body_reads"] == b.metrics["body_reads"] == 1
    a.fingerprint("a.py")
    backend.close()
    assert cache.stats()["charged_bytes"] == 0
    assert not a._fingerprints.put("a.py", (1, 2, 3, 4, 5, 6), "0" * 64)
    with pytest.raises(SourceError, match="LIVE_CLOSED"):
        backend.source("a")


def test_closing_one_backend_does_not_clear_a_different_backends_cache(tmp_path):
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        (root / "same.py").write_text(f"value = '{name}'\n")
    one = LiveQueries({"one": SourceAccess(tmp_path / "one")})
    two = LiveQueries({"two": SourceAccess(tmp_path / "two")})
    one.source("one").fingerprint("same.py")
    source = two.source("two")
    expected = source.fingerprint("same.py")
    one.close()
    assert len(two.fingerprint_cache) == 1
    assert source.fingerprint("same.py") == expected and source.metrics["body_reads"] == 1


def test_more_than_4096_participants_validate_twice_without_repeat_body_reads(tmp_path):
    root = tmp_path / "large"
    root.mkdir()
    paths = [f"file_{number:04d}.py" for number in range(4100)]
    for path in paths:
        (root / path).write_text("value = 0\n")
    source = SourceAccess(root)
    backend = LiveQueries({"large": source})
    handle, _ = backend.resolve_snapshot("large")
    for path in paths:
        document = source.read(path)
        backend.contexts.observe("large", source.source_id, handle, path, document.sha256)
    for _ in range(2):
        backend.resolve_snapshot("large", handle)
    assert source.metrics["body_reads"] == 4100
    assert len(source._fingerprints) == len(backend.fingerprint_cache) == 4100
    assert backend.fingerprint_cache.stats()["charged_bytes"] <= 32 * 1024 * 1024
    backend.close()


def registered_backend(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(workspace)
    ids = {}
    for name in ("a", "b"):
        root = workspace / name
        root.mkdir()
        (root / "same.py").write_text(f"value = '{name}'\n")
        ids[name] = registry.register(name, enabled=True)
    return LiveQueries(registry=registry), registry, ids


def test_refresh_revokes_only_disabled_source_and_preserves_other_active_metadata(tmp_path):
    backend, registry, ids = registered_backend(tmp_path)
    a, b = backend.source(ids["a"]), backend.source(ids["b"])
    a.fingerprint("same.py")
    expected = b.fingerprint("same.py")
    registry.set_enabled(ids["a"], False)
    backend.refresh_sources()
    assert len(a._fingerprints) == 0 and len(b._fingerprints) == 1
    assert b.fingerprint("same.py") == expected and b.metrics["body_reads"] == 1
    assert not a._fingerprints.put("late.py", (1, 2, 3, 4, 5, 6), "0" * 64)
    with pytest.raises(SourceError):
        backend.source(ids["a"])
    assert len(b._fingerprints) == 1


def test_dynamic_registry_sources_lazily_attach_same_cache_without_resetting_b(tmp_path):
    backend, registry, ids = registered_backend(tmp_path)
    b = backend.source(ids["b"])
    expected = b.fingerprint("same.py")
    root = registry.workspace / "new"
    root.mkdir()
    (root / "new.py").write_text("value = 3\n")
    new_id = registry.register("new", enabled=True)
    new = backend.source(new_id)  # No refresh call: registry is dynamically authorized.
    assert new._fingerprints.cache is backend.fingerprint_cache
    assert new.fingerprint("new.py")
    assert b.fingerprint("same.py") == expected and b.metrics["body_reads"] == 1
    registry.set_enabled(new_id, False)
    with pytest.raises(SourceError):
        backend.source(new_id)
    assert len(new._fingerprints) == 0 and len(b._fingerprints) == 1


def test_replaced_source_binding_discards_only_old_namespace(backend):
    a, b = backend.source("a"), backend.source("b")
    a.fingerprint("a.py")
    expected_b = b.fingerprint("b.py")
    a.root.rename(a.root.with_name("saved_a"))
    a.root.mkdir()
    (a.root / "a.py").write_text("replacement\n")
    backend.sources["a"] = SourceAccess(a.root)
    replacement = backend.source("a")
    assert replacement.source_id != a.source_id
    assert len(a._fingerprints) == 0
    assert b.fingerprint("b.py") == expected_b and b.metrics["body_reads"] == 1
    assert replacement.fingerprint("a.py")
    assert replacement._fingerprints.cache is b._fingerprints.cache


def test_64_sources_share_one_global_budget_not_64_independent_50000_caches(tmp_path):
    sources = {}
    for number in range(64):
        root = tmp_path / f"p{number}"
        root.mkdir()
        (root / "a.py").write_text(f"value = {number}\n")
        sources[f"p{number}"] = SourceAccess(root)
    backend = LiveQueries(sources)
    for source in sources.values():
        assert source._fingerprints.cache is backend.fingerprint_cache
        source.fingerprint("a.py")
    assert len(backend.fingerprint_cache) == 64
    assert backend.fingerprint_cache.stats()["active_sources"] == 64
    assert backend.fingerprint_cache.stats()["charged_bytes"] <= 32 * 1024 * 1024
    backend.close()


@pytest.mark.parametrize("change", ["disable", "nested_boundary"])
def test_batch_rechecks_registry_authorization_and_scope_after_last_leaf(
    tmp_path, monkeypatch, change
):
    backend, registry, ids = registered_backend(tmp_path)
    a, b = backend.source(ids["a"]), backend.source(ids["b"])
    (a.root / "nested").mkdir()
    snapshot = backend.read_file(ids["a"], "same.py")["snapshot"]
    b_hash = b.fingerprint("same.py")
    original = type(a._fingerprints).get
    changed = False

    def mutate(view, path):
        nonlocal changed
        result = original(view, path)
        if view.source_id == a.source_id and not changed:
            changed = True
            if change == "disable":
                registry.set_enabled(ids["a"], False)
            else:
                registry.register("a/nested", enabled=False)
        return result

    monkeypatch.setattr(type(a._fingerprints), "get", mutate)
    with pytest.raises(SourceError):
        backend.resolve_snapshot(ids["a"], snapshot)
    with pytest.raises(ContextError, match="unknown|invalidated"):
        backend.contexts.get(ids["a"], a.source_id, snapshot)
    assert a._fingerprint_batches.stack == []
    assert b.fingerprint("same.py") == b_hash and b.metrics["body_reads"] == 1


def test_batch_rejects_backend_source_object_rebinding_at_end(backend, monkeypatch):
    a = backend.source("a")
    snapshot = backend.read_file("a", "a.py")["snapshot"]
    replacement = SourceAccess(a.root)
    original = type(a._fingerprints).get

    def rebind(view, path):
        result = original(view, path)
        backend.sources["a"] = replacement
        return result

    monkeypatch.setattr(type(a._fingerprints), "get", rebind)
    with pytest.raises(SourceError, match="SOURCE_REPLACED"):
        backend.resolve_snapshot("a", snapshot)
    assert a._fingerprint_batches.stack == []


def test_context_validate_runs_inside_batch_without_repeated_root_opens(backend, monkeypatch):
    source = backend.source("a")
    for number in range(20):
        path = source.root / f"file_{number}.py"
        path.write_text("value = 1\n")
    snapshot = backend.resolve_snapshot("a")[0]
    for item in source.manifest()["files"]:
        document = source.read(item["path"])
        backend.contexts.observe("a", source.source_id, snapshot, document.path, document.sha256)
    original = source._fingerprints.get
    calls = []

    def in_batch(view, path):
        assert source._fingerprint_batches.stack
        calls.append(path)
        return original(path)

    monkeypatch.setattr(type(source._fingerprints), "get", in_batch)
    reads = source.metrics["body_reads"]
    backend.resolve_snapshot("a", snapshot)
    assert len(calls) == 21 and source.metrics["body_reads"] == reads
    assert source._fingerprint_batches.stack == []

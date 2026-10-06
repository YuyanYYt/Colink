import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from code_context.fingerprint_cache import FingerprintCache
from code_context.source_access import SourceAccess, SourceError


def test_manifest_is_metadata_only_and_read_is_immediately_current(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    file = root / "demo.py"
    file.write_text("old\n", encoding="utf-8")
    access = SourceAccess(root)
    assert access.manifest()["files"][0]["path"] == "demo.py"
    assert access.metrics["body_reads"] == 0
    assert access.read("demo.py").content == "old\n"
    file.write_text("new\n", encoding="utf-8")
    assert access.read("demo.py").content == "new\n"
    assert access.metrics["body_reads"] == 2


@pytest.mark.parametrize("path", ["../other.py", "/tmp/file.py", ".env", ".git/config"])
def test_source_rejects_escape_and_sensitive_paths(tmp_path, path):
    access = SourceAccess(tmp_path)
    with pytest.raises(SourceError):
        access.read(path)


def test_source_rejects_symlink_parent_and_replaced_root(tmp_path):
    root, outside = tmp_path / "source", tmp_path / "other"
    root.mkdir()
    outside.mkdir()
    (outside / "a.py").write_text("outside\n")
    (root / "link").symlink_to(outside, target_is_directory=True)
    access = SourceAccess(root)
    assert not access.manifest()["files"]
    with pytest.raises(SourceError):
        access.read("link/a.py")
    root.rename(tmp_path / "moved")
    root.mkdir()
    with pytest.raises(SourceError, match="SOURCE_REPLACED"):
        access.ensure_available()


def test_metadata_limits_and_root_ignore_are_enforced(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "large.py").write_text("ignored body")
    for name in ["a.py", "b.py", "c.py"]:
        (tmp_path / name).write_text("x\n")
    access = SourceAccess(tmp_path)
    result = access.manifest(max_files=2)
    assert result["partial"] and len(result["files"]) == 2
    assert access.metrics["body_reads"] == 0
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        access.read("ignored/large.py")


def test_read_preserves_crlf_bom_and_permissions(tmp_path):
    path = tmp_path / "a.py"
    path.write_bytes(b"\xef\xbb\xbf# Chinese\r\nx = 1\r\n")
    os.chmod(path, 0o640)
    result = SourceAccess(tmp_path).read("a.py")
    assert result.content == "\ufeff# Chinese\r\nx = 1\r\n"
    assert result.mode == 0o640
    assert result.size == path.stat().st_size


def test_secret_and_binary_are_not_read(tmp_path):
    (tmp_path / "binary.py").write_bytes(b"x\x00y")
    with pytest.raises(SourceError, match="FILE_EXCLUDED"):
        SourceAccess(tmp_path).read("binary.py")


def test_read_detects_changes_during_safe_read(tmp_path, monkeypatch):
    file = tmp_path / "a.py"
    file.write_text("before\n")
    access = SourceAccess(tmp_path)
    original = access.scanner._read_text

    def changing(parent, name, path):
        content, problem = original(parent, name, path)
        if path == "a.py":
            file.write_text("after\n")
        return content, problem

    monkeypatch.setattr(access.scanner, "_read_text", changing)
    with pytest.raises(SourceError, match="SOURCE_CHANGED"):
        access.read("a.py")


def test_fingerprint_validation_reuses_only_unchanged_metadata(tmp_path):
    file = tmp_path / "a.py"
    file.write_text("before\n")
    access = SourceAccess(tmp_path)
    document = access.read("a.py")
    assert access.fingerprint("a.py") == document.sha256
    assert access.metrics["body_reads"] == 1
    file.write_text("after\n")
    assert access.fingerprint("a.py") != document.sha256
    assert access.metrics["body_reads"] == 2


def test_ignore_rules_reuse_metadata_but_changed_policy_takes_effect(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("allowed = True\n")
    ignore = root / ".codecontextignore"
    ignore.write_text("other.py\n")
    source = SourceAccess(root)
    calls = []
    original = source.scanner._load_ignore

    def count(fd):
        calls.append(1)
        return original(fd)

    monkeypatch.setattr(source.scanner, "_load_ignore", count)
    assert source.read("a.py").content
    assert source.fingerprint("a.py")
    source.manifest()
    assert len(calls) == 1
    ignore.write_text("a.py\n")
    assert source.fingerprint("a.py") is None
    assert len(calls) == 2


def test_standalone_default_is_4096_and_backend_attachment_is_metadata_only(tmp_path):
    source = SourceAccess(tmp_path)
    assert source._fingerprints.cache.max_entries == 4096
    for number in range(4097):
        source._fingerprints.put(f"file_{number}.py", (1, 2, 3, 4, 5, 6), "0" * 64)
    assert len(source._fingerprints) == 4096
    assert source._fingerprints.get("file_0.py") is None
    cache = FingerprintCache()
    source.attach_fingerprint_cache(cache)
    assert source._fingerprints.cache is cache
    assert source.metrics["body_reads"] == 0


def test_shared_same_path_hashes_are_source_bound_and_changes_still_read(tmp_path):
    cache = FingerprintCache()
    sources = []
    for name, body in (("a", "value = 1\n"), ("b", "value = 2\n")):
        root = tmp_path / name
        root.mkdir()
        (root / "same.py").write_text(body)
        source = SourceAccess(root)
        source.attach_fingerprint_cache(cache)
        sources.append(source)
    a, b = sources
    before = a.fingerprint("same.py")
    assert b.fingerprint("same.py") != before
    (a.root / "same.py").write_text("value = 3\n")
    assert a.fingerprint("same.py") != before
    assert a.metrics["body_reads"] == 2
    assert b.fingerprint("same.py") and b.metrics["body_reads"] == 1


def test_cached_fingerprint_keeps_second_stat_and_ignore_checks(tmp_path, monkeypatch):
    file = tmp_path / "a.py"
    file.write_text("before\n")
    source = SourceAccess(tmp_path)
    source.attach_fingerprint_cache(FingerprintCache())
    old = source.fingerprint("a.py")
    original = source._fingerprints.get

    def change_between_stats(path):
        value = original(path)
        file.write_text("after\n")
        return value

    monkeypatch.setattr(type(source._fingerprints), "get", lambda self, p: change_between_stats(p))
    assert source.fingerprint("a.py") != old
    assert source.metrics["body_reads"] == 2
    (tmp_path / ".codecontextignore").write_text("a.py\n")
    assert source.fingerprint("a.py") is None
    assert len(source._fingerprints) == 0


def test_shared_cache_never_bypasses_symlink_or_root_identity(tmp_path):
    root, other = tmp_path / "source", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    file = root / "a.py"
    file.write_text("before\n")
    (other / "a.py").write_text("outside\n")
    source = SourceAccess(root)
    source.attach_fingerprint_cache(FingerprintCache())
    source.fingerprint("a.py")
    file.unlink()
    file.symlink_to(other / "a.py")
    assert source.fingerprint("a.py") is None
    assert source.metrics["body_reads"] == 1
    root.rename(tmp_path / "saved")
    root.mkdir()
    with pytest.raises(SourceError, match="SOURCE_REPLACED"):
        source.fingerprint("a.py")


def test_source_io_can_observe_cache_stats_without_inverse_lock(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    (tmp_path / "a.py").write_text("value = 1\n")
    source = SourceAccess(tmp_path)
    cache = FingerprintCache()
    source.attach_fingerprint_cache(cache)
    original = source.scanner._read_text

    def observed(parent, name, path):
        assert not cache._lock.locked()
        cache.stats()
        return original(parent, name, path)

    monkeypatch.setattr(source.scanner, "_read_text", observed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(source.fingerprint, "a.py").result(timeout=2)


def warm_source(root, paths):
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n")
    source = SourceAccess(root)
    hashes = {path: source.fingerprint(path) for path in paths}
    return source, hashes


def track_directory_fds(monkeypatch):
    original_open, original_close = os.open, os.close
    state = {"live": set(), "peak": 0, "opens": 0}

    def opening(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            state["live"].add(fd)
            state["opens"] += 1
            state["peak"] = max(state["peak"], len(state["live"]))
        return fd

    def closing(fd):
        original_close(fd)
        state["live"].discard(fd)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "close", closing)
    return state


def test_batch_reuses_parent_but_keeps_two_leaf_stats_and_closes_fds(tmp_path, monkeypatch):
    paths = [f"pkg/file_{n}.py" for n in range(80)]
    source, hashes = warm_source(tmp_path, paths)
    calls, original = [], source.scanner._stat

    def count(fd, name, path):
        if path in hashes:
            calls.append(path)
        return original(fd, name, path)

    monkeypatch.setattr(source.scanner, "_stat", count)
    tracked = track_directory_fds(monkeypatch)
    with source.fingerprint_batch() as batch:
        assert batch is source
        assert {p: batch.fingerprint(p) for p in paths} == hashes
        assert source.metrics["fingerprint_batch_parent_opens"] == 1
    assert all(calls.count(p) == 2 for p in paths)
    assert source.metrics["body_reads"] == len(paths)
    assert not tracked["live"] and source._fingerprint_batches.stack == []


@pytest.mark.parametrize("depth,count", [(1, 70), (10, 16)])
def test_parent_lru_bounds_actual_ancestor_fds_not_only_parent_items(
    tmp_path, monkeypatch, depth, count
):
    paths = ["/".join([f"p{n}"] + ["d"] * (depth - 1) + ["a.py"]) for n in range(count)]
    source, hashes = warm_source(tmp_path, paths)
    tracked = track_directory_fds(monkeypatch)
    with source.fingerprint_batch():
        for path in paths + [paths[0]]:
            assert source.fingerprint(path) == hashes[path]
        assert source.metrics["fingerprint_batch_parent_evictions"] > 0
        assert source.metrics["fingerprint_batch_peak_parent_items"] <= 64
        assert source.metrics["fingerprint_batch_peak_retained_fds"] <= 128
    assert tracked["peak"] <= 128 and not tracked["live"]
    assert source.metrics["body_reads"] == count


def test_deep_chain_falls_back_to_original_single_path_without_retaining_parents(tmp_path):
    path = "/".join(["d"] * 130 + ["a.py"])
    source, hashes = warm_source(tmp_path, [path])
    with source.fingerprint_batch():
        assert source.fingerprint(path) == hashes[path]
        assert source.metrics["fingerprint_batch_fallbacks"] == 1
        assert not source._fingerprint_batches.stack[-1].parents
    assert source.metrics["body_reads"] == 1


def test_batch_nested_pools_are_independent_and_thread_state_does_not_leak(tmp_path, monkeypatch):
    source, hashes = warm_source(tmp_path, ["pkg/a.py", "other/b.py"])
    tracked = track_directory_fds(monkeypatch)
    with source.fingerprint_batch():
        assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
        outer = source._fingerprint_batches.stack[-1]
        outer_fd = outer.parents["pkg"][1]
        with source.fingerprint_batch():
            assert source._fingerprint_batches.stack[-1] is not outer
            assert source.fingerprint("other/b.py") == hashes["other/b.py"]
        assert os.fstat(outer_fd)
        assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
    assert tracked["peak"] <= 128 and not tracked["live"]

    def validate(_):
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
        assert source._fingerprint_batches.stack == []

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(validate, range(6)))
    assert not tracked["live"] and source.metrics["body_reads"] == 2


def test_batch_body_exception_still_closes_every_fd(tmp_path, monkeypatch):
    source, _ = warm_source(tmp_path, ["pkg/a.py"])
    tracked = track_directory_fds(monkeypatch)
    with pytest.raises(RuntimeError, match="caller aborted"):
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py")
            raise RuntimeError("caller aborted")
    assert not tracked["live"] and source._fingerprint_batches.stack == []


@pytest.mark.parametrize("change", ["symlink", "replace", "rename_restore"])
def test_parent_link_change_rejects_whole_batch_even_for_cached_leaf(tmp_path, monkeypatch, change):
    source, hashes = warm_source(tmp_path, ["pkg/a.py", "other/b.py"])
    tracked = track_directory_fds(monkeypatch)
    with pytest.raises(SourceError, match="SOURCE_CHANGED") as error:
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
            (tmp_path / "pkg").rename(tmp_path / "saved")
            if change == "symlink":
                (tmp_path / "pkg").symlink_to(tmp_path / "saved", target_is_directory=True)
            elif change == "replace":
                (tmp_path / "pkg").mkdir()
                (tmp_path / "pkg/a.py").write_text("value = 9\n")
            else:
                (tmp_path / "saved").rename(tmp_path / "pkg")
            assert source.fingerprint("other/b.py") == hashes["other/b.py"]
    assert str(tmp_path) not in str(error.value)
    assert not tracked["live"] and source.metrics["body_reads"] == 2


@pytest.mark.parametrize(
    "change", ["ignore", "scope", "identity", "root", "ancestor", "ancestor_restore"]
)
def test_batch_postcheck_rejects_policy_binding_root_and_ancestor_changes(tmp_path, change):
    root = tmp_path / "container" / "source"
    root.mkdir(parents=True)
    (root / ".gitignore").write_text("other.py\n")
    source, hashes = warm_source(root, ["pkg/a.py"])
    with pytest.raises(SourceError):
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
            if change == "ignore":
                (root / ".gitignore").write_text("pkg/\n")
            elif change == "scope":
                source.scanner._excluded.add("pkg")
            elif change == "identity":
                source.source_id = "f" * 64
            elif change == "root":
                root.rename(root.with_name("saved"))
                root.mkdir()
            else:
                root.parent.rename(tmp_path / "saved_container")
                if change == "ancestor_restore":
                    (tmp_path / "saved_container").rename(root.parent)
                else:
                    root.mkdir(parents=True)
    assert source._fingerprint_batches.stack == []


def test_batch_keeps_second_leaf_stat_changed_hash_and_missing_file_checks(tmp_path, monkeypatch):
    source, hashes = warm_source(tmp_path, ["pkg/a.py"])
    original = type(source._fingerprints).get

    def changed(view, path):
        value = original(view, path)
        (tmp_path / path).write_text("value = 2\n")
        return value

    with monkeypatch.context() as patch:
        patch.setattr(type(source._fingerprints), "get", changed)
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py") != hashes["pkg/a.py"]
    assert source.metrics["body_reads"] == 2
    (tmp_path / "pkg/a.py").unlink()
    with pytest.raises(SourceError, match="batch scope changed"):
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py") is None


def test_parent_lru_eviction_validates_link_before_discarding_it(tmp_path):
    paths = [f"p{n}/a.py" for n in range(65)]
    source, _ = warm_source(tmp_path, paths)
    with pytest.raises(SourceError, match="SOURCE_CHANGED"):
        with source.fingerprint_batch():
            for path in paths[:64]:
                assert source.fingerprint(path)
            (tmp_path / "p0").rename(tmp_path / "saved")
            (tmp_path / "p0").mkdir()
            source.fingerprint(paths[-1])  # Eviction cannot hide the changed old link.
    assert source._fingerprint_batches.stack == []


@pytest.fixture
def deep_root(tmp_path):
    root = tmp_path.joinpath(*(["d"] * 70))
    root.mkdir(parents=True)
    assert len(root.parts) * 2 > 128
    return root


def test_deep_absolute_root_batch_keeps_direct_and_live_reads_compatible(deep_root, monkeypatch):
    from code_context.live import LiveQueries

    source, hashes = warm_source(deep_root, ["pkg/a.py"])
    assert source.read("pkg/a.py").content == "value = 1\n"
    tracked = track_directory_fds(monkeypatch)
    with source.fingerprint_batch() as batch:
        assert batch is source
        assert not tracked["live"] and source._fingerprint_batches.stack == []
        assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
        assert source.read("pkg/a.py").content == "value = 1\n"
        assert not tracked["live"]
    assert not tracked["live"]
    assert source.metrics["fingerprint_batch_parent_opens"] == 0
    assert source.metrics["fingerprint_batch_peak_retained_fds"] == 0

    backend = LiveQueries({"deep": source})
    try:
        handle, _ = backend.resolve_snapshot("deep")
        assert backend.read_file("deep", "pkg/a.py", handle)["content"] == "value = 1\n"
        assert backend.resolve_snapshot("deep", handle)[0] == handle
        (deep_root / "pkg/a.py").write_text("value = 2\n")
        with pytest.raises(SourceError, match="LIVE_CONTEXT_INVALID"):
            backend.resolve_snapshot("deep", handle)
    finally:
        backend.close()
    assert not tracked["live"]


@pytest.mark.parametrize(
    "change", ["root", "symlink", "ancestor_restore", "gitignore", "codecontextignore", "scope"]
)
def test_deep_absolute_root_fallback_rejects_metadata_changes(deep_root, monkeypatch, change):
    (deep_root / ".gitignore").write_text("other.py\n")
    (deep_root / ".codecontextignore").write_text("other.py\n")
    source, hashes = warm_source(deep_root, ["pkg/a.py"])
    tracked = track_directory_fds(monkeypatch)
    with pytest.raises(SourceError) as error:
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
            if change in ("root", "symlink"):
                saved = deep_root.with_name("saved")
                deep_root.rename(saved)
                if change == "root":
                    deep_root.mkdir()
                else:
                    deep_root.symlink_to(saved, target_is_directory=True)
            elif change == "ancestor_restore":
                ancestor = deep_root.parent
                saved = ancestor.with_name("saved")
                ancestor.rename(saved)
                saved.rename(ancestor)
            elif change == "scope":
                source.scanner._excluded.add("pkg")
            else:
                (deep_root / f".{change}").write_text("pkg/\n")
    assert str(deep_root) not in str(error.value)
    assert not tracked["live"] and source._fingerprint_batches.stack == []
    assert not source._fingerprint_batches.unpooled
    assert source.metrics["body_reads"] == 1


@pytest.mark.parametrize("change", ["disabled_before", "disabled_after", "child_boundary"])
def test_deep_absolute_root_rechecks_registered_authority_and_boundaries(
    tmp_path, deep_root, change
):
    from code_context.project_registry import ProjectRegistry, RegistryError

    (deep_root / "pkg").mkdir()
    (deep_root / "pkg/a.py").write_text("value = 1\n")
    registry = ProjectRegistry(tmp_path)
    relative = deep_root.relative_to(tmp_path).as_posix()
    project = registry.register(relative, enabled=True)
    source = registry.source(project)
    expected = source.fingerprint("pkg/a.py")
    if change == "disabled_before":
        registry.set_enabled(project, False)
    with pytest.raises(RegistryError if change.startswith("disabled") else SourceError):
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py") == expected
            if change == "child_boundary":
                registry.register(f"{relative}/pkg")
            else:
                registry.set_enabled(project, False)
    assert source._fingerprint_batches.stack == []
    assert not getattr(source._fingerprint_batches, "unpooled", False)


def test_deep_absolute_root_fallback_keeps_second_leaf_stat(deep_root, monkeypatch):
    source, hashes = warm_source(deep_root, ["pkg/a.py"])
    original = type(source._fingerprints).get

    def changed(view, path):
        cached = original(view, path)
        (deep_root / path).write_text("value = 2\n")
        return cached

    monkeypatch.setattr(type(source._fingerprints), "get", changed)
    with source.fingerprint_batch():
        assert source.fingerprint("pkg/a.py") != hashes["pkg/a.py"]
    assert source.metrics["body_reads"] == 2


def test_deep_absolute_root_nested_rejection_and_caller_failure_leave_no_state(
    deep_root, monkeypatch
):
    source, hashes = warm_source(deep_root, ["pkg/a.py"])
    tracked = track_directory_fds(monkeypatch)
    with pytest.raises(RuntimeError, match="caller aborted"):
        with source.fingerprint_batch():
            with pytest.raises(SourceError, match="nested validation FD budget exceeded"):
                with source.fingerprint_batch():
                    pytest.fail("nested fallback must not open another root stack")
            assert not tracked["live"]
            assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
            raise RuntimeError("caller aborted")
    assert not tracked["live"] and not source._fingerprint_batches.unpooled

    def validate(_):
        with source.fingerprint_batch():
            assert source.fingerprint("pkg/a.py") == hashes["pkg/a.py"]
        assert source._fingerprint_batches.stack == []
        assert not source._fingerprint_batches.unpooled

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(validate, range(4)))
    assert not tracked["live"] and source.metrics["body_reads"] == 1

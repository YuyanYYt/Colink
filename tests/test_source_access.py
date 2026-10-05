import os

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

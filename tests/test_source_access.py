import os

import pytest

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

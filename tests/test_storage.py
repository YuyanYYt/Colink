import pytest
from pydantic import ValidationError

from code_context.models import FileChange, SyncBatch, content_hash
from code_context.storage import MirrorError, MirrorStore, RevisionConflict


def upsert(path, content):
    return FileChange(op="upsert", path=path, content=content, sha256=content_hash(content))


def batch(request_id, base_revision, changes, mode="delta"):
    return SyncBatch(request_id=request_id, base_revision=base_revision, mode=mode, changes=changes)


def test_snapshot_isolation_delete_rename_search_and_diff(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    store.apply("sample", batch("first", 0, [upsert("旧.py", "value = 1\n")], "full"))
    store.apply(
        "sample",
        batch(
            "second",
            1,
            [
                FileChange(op="delete", path="旧.py"),
                upsert("new.py", "value = 2\n"),
            ],
        ),
    )
    assert store.read_file("sample", "旧.py", revision=1)["content"] == "value = 1\n"
    with pytest.raises(MirrorError):
        store.read_file("sample", "旧.py", revision=2)
    assert store.read_file("sample", "new.py", revision=2)["content"] == "value = 2\n"
    assert store.search_code("sample", "value", revision=1)["matches"][0]["path"] == "旧.py"
    changes = store.get_diff("sample", 1, 2)["changes"]
    assert {c["op"] for c in changes} == {"add", "delete"}
    assert store.manifest("sample")["revision"] == 2


def test_replayed_request_and_revision_conflict_are_atomic(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    first = batch("first", 0, [upsert("a.py", "a=1\n")], "full")
    assert store.apply("sample", first)["revision"] == 1
    assert store.apply("sample", first) == {"project_id": "sample", "revision": 1, "replayed": True}
    with pytest.raises(RevisionConflict):
        store.apply("sample", batch("stale", 0, [upsert("b.py", "b=2\n")]))
    with pytest.raises(MirrorError, match="different payload"):
        store.apply("sample", batch("first", 0, [upsert("a.py", "a=9\n")], "full"))
    assert store.manifest("sample")["file_count"] == 1
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../b", "a//b", "a\\b", "a\x00b"])
def test_rejects_path_traversal(path):
    with pytest.raises(ValidationError):
        upsert(path, "safe")


def test_rejects_hash_mismatch_secrets_and_duplicate_changes():
    with pytest.raises(ValidationError, match="sha256"):
        FileChange(op="upsert", path="a.py", content="a", sha256="0" * 64)
    with pytest.raises(ValidationError):
        upsert("config.py", "token='sk-" + "a" * 30 + "'\n")
    with pytest.raises(ValidationError):
        upsert(".env", "placeholder")
    with pytest.raises(ValidationError, match="duplicate"):
        batch("same-path", 0, [upsert("a.py", "a"), upsert("a.py", "b")], "full")


def test_bounded_lines_and_search_results(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    store.apply("sample", batch("first", 0, [upsert("a.py", "match\n" * 205)], "full"))
    result = store.read_file("sample", "a.py", start_line=200, end_line=202)
    assert result["content"] == "match\n" * 3
    assert result["total_lines"] == 205 and result["has_more"]
    result = store.search_code("sample", "match", limit=5)
    assert len(result["matches"]) == 5 and result["has_more"]
    with pytest.raises(MirrorError):
        store.read_file("sample", "a.py", start_line=0)


def test_snapshot_size_limit_rolls_back(tmp_path, monkeypatch):
    import code_context.storage as module

    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 4)
    store = MirrorStore(tmp_path / "mirror.db")
    with pytest.raises(MirrorError, match="8 MiB"):
        store.apply("sample", batch("big", 0, [upsert("a.py", "12345")], "full"))
    assert store.list_projects()["projects"] == []
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0

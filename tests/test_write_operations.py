"""Source writes stay inside fresh independent test directories, never real projects."""

import hashlib
import json
import os

import pytest

import code_context.write_operations as operations
from code_context.recovery_store import RecoveryError, RecoveryStore
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator, WriteError


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")
    (root / "b.py").write_bytes(b"unchanged\n")
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    c = WriteCoordinator(
        store,
        lambda project: source if project == "a" else SourceAccess(tmp_path),
        control_alive=lambda: True,
    )
    yield source, store, c
    c.close()
    store.close()


def begin(c, **kwargs):
    c.enable(["a"])
    return c.begin_write_task("a", c.status()["next_task_request_id"], **kwargs)["task_id"]


def edit(source, c, task, request="edit_0001", path="a.py", old="first", new="updated"):
    before = source.read(path)
    return c.apply_edit(
        "a",
        task,
        request,
        path,
        before.sha256,
        {"kind": "replace_fragment", "old_text": old, "new_text": new},
    )


def test_edit_retains_origin_once_preserves_mode_and_raw_encoding(parts):
    source, store, c = parts
    os.chmod(source.root / "a.py", 0o640)
    origin = source.read("a.py")
    task = begin(c)
    result = edit(source, c, task)
    assert result["readback_verified"]
    assert (source.root / "a.py").read_bytes() == b"\xef\xbb\xbfupdated\r\nsecond\r\n"
    assert source.read("a.py").mode == 0o640
    rows = store.query("SELECT * FROM files")
    assert rows[0]["origin_hash"] == origin.sha256
    assert rows[0]["last_hash"] == result["sha256"]
    assert store.read_blob(origin.sha256) == origin.content.encode()
    assert store.usage()["object_count"] == 1
    assert not list(source.root.glob(".colink-write-*"))
    edit(source, c, task, "edit_0002", old="updated", new="again")
    assert store.query("SELECT * FROM files")[0]["origin_hash"] == origin.sha256
    assert store.usage()["object_count"] == 1
    assert not list(source.root.glob(".colink-write-*"))


def test_same_request_returns_durable_result_without_second_install(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    before = source.read("a.py")
    params = (
        "a",
        task,
        "edit_0001",
        "a.py",
        before.sha256,
        {"kind": "replace_fragment", "old_text": "first", "new_text": "updated"},
    )
    first = c.apply_edit(*params)
    monkeypatch.setattr(operations, "commit_file", lambda *args: pytest.fail("no second install"))
    assert c.apply_edit(*params) == first
    conflicting = (
        *params[:-1],
        {"kind": "replace_fragment", "old_text": "first", "new_text": "different"},
    )
    with pytest.raises(WriteError, match="REQUEST_ID_CONFLICT"):
        c.apply_edit(*conflicting)
    assert len(store.query("SELECT * FROM operations")) == 1


def test_restart_defaults_off_but_replay_is_persistent(parts):
    source, store, c = parts
    task = begin(c)
    before = source.read("a.py")
    params = (
        "a",
        task,
        "edit_0001",
        "a.py",
        before.sha256,
        {"kind": "replace_fragment", "old_text": "first", "new_text": "updated"},
    )
    result = c.apply_edit(*params)
    c.close()
    other = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    with pytest.raises(WriteError, match="WRITE_DISABLED"):
        other.apply_edit(*params)
    other.enable(["a"])
    assert other.apply_edit(*params) == result
    other.close()


@pytest.mark.parametrize("kind", ["hash", "range", "ambiguous", "scope", "external", "late"])
def test_precondition_failures_do_not_journal_or_write(parts, kind):
    source, store, c = parts
    task = begin(c, paths=["a.py"] if kind == "scope" else None)
    path = "b.py" if kind in {"scope", "late"} else "a.py"
    before = source.read(path)
    sha, change = (
        before.sha256,
        {"kind": "replace_fragment", "old_text": "first", "new_text": "updated"},
    )
    if kind == "hash":
        sha = "0" * 64
    elif kind == "range":
        change = {
            "kind": "replace_lines",
            "start_line": 500,
            "end_line": 500,
            "old_text": "x",
            "new_text": "y",
        }
    elif kind == "ambiguous":
        change["old_text"] = "s"
    elif kind in {"external", "late"}:
        (source.root / path).write_text("external change\n")
        if kind == "late":
            sha = source.read(path).sha256
    actual = (source.root / path).read_bytes()
    with pytest.raises(SourceError):
        c.apply_edit("a", task, "edit_0001", path, sha, change)
    assert (source.root / path).read_bytes() == actual
    assert store.query("SELECT * FROM operations") == []
    assert store.query("SELECT * FROM objects") == []


def test_backup_failure_never_changes_source_and_protects_pending(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    raw = (source.root / "a.py").read_bytes()

    def fail(*args):
        raise RecoveryError("RECOVERY_SAVE_FAILED: injected")

    monkeypatch.setattr(store, "put_blob", fail)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        edit(source, c, task)
    assert (source.root / "a.py").read_bytes() == raw
    assert not list(source.root.glob(".colink-write-*"))
    assert c.status()["recovery_required"] and not c.status()["write_enabled"]
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.guard_read("a")
    c.guard_read("unrelated")
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.enable(["a"])


def test_intent_and_backup_are_durable_before_preparing_source(parts, monkeypatch):
    source, store, c = parts
    original = source.read("a.py")
    task = begin(c)
    actual = operations.prepare_file

    def prepare(*args, **kwargs):
        row = store.query("SELECT * FROM operations")[0]
        assert row["state"] == "prepared"
        assert json.loads(row["metadata"])["phase"] == "preparing"
        assert store.read_blob(original.sha256) == original.content.encode()
        assert len(store.query("SELECT * FROM object_refs")) == 3
        return actual(*args, **kwargs)

    monkeypatch.setattr(operations, "prepare_file", prepare)
    edit(source, c, task)


def test_close_between_prepare_and_commit_refuses_source_change(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    raw = (source.root / "a.py").read_bytes()
    actual = operations.prepare_file

    def prepare(*args, **kwargs):
        prepared = actual(*args, **kwargs)
        c.disable()
        return prepared

    monkeypatch.setattr(operations, "prepare_file", prepare)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        edit(source, c, task)
    assert (source.root / "a.py").read_bytes() == raw
    assert len(list(source.root.glob(".colink-write-*"))) == 1
    assert store.usage()["object_count"] == 2


def test_created_file_ownership_replay_no_overwrite_and_final_link_count(parts):
    source, store, c = parts
    task = begin(c, paths=["new.py", "a.py"])
    result = c.create_file("a", task, "create_001", "new.py", "created\r\n")
    assert c.create_file("a", task, "create_001", "new.py", "created\r\n") == result
    assert (source.root / "new.py").read_bytes() == b"created\r\n"
    assert os.stat(source.root / "new.py").st_nlink == 1
    row = store.query("SELECT * FROM files")[0]
    assert row["kind"] == "created" and row["origin_hash"] is None
    assert tuple(json.loads(row["last_version"])) == source.read("new.py").version
    assert store.usage()["object_count"] == 0
    with pytest.raises(WriteError, match="WRITE_TARGET_EXISTS"):
        c.create_file("a", task, "create_002", "new.py", "must not overwrite")
    edit(source, c, task, "edit_0002", path="new.py", old="created", new="later")
    assert store.query("SELECT * FROM files")[0]["kind"] == "created"
    assert store.usage()["object_count"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "/escape.py",
        ".env",
        "node_modules/new.py",
        ".colink-write-" + "a" * 32 + ".tmp",
    ],
)
def test_unsafe_new_paths_refused(parts, path):
    source, store, c = parts
    task = begin(c)
    with pytest.raises(SourceError):
        c.create_file("a", task, "create_001", path, "text")
    assert store.query("SELECT * FROM operations") == []


def test_repeated_small_writes_do_not_keep_intermediate_full_bodies(parts):
    source, store, c = parts
    original = "x" * (100 * 1024) + "\nMARKER_0\n"
    (source.root / "a.py").write_text(original)
    task = begin(c, paths=["a.py"])
    for index in range(20):
        edit(source, c, task, f"edit_{index:04d}", old=f"MARKER_{index}", new=f"MARKER_{index + 1}")
        assert store.usage()["object_count"] == 1
    assert store.usage()["resident_bytes"] < 1024 * 1024
    assert store.read_blob(hashlib.sha256(original.encode()).hexdigest()) == original.encode()
    assert len(store.query("SELECT * FROM operations")) == 20
    assert not list(source.root.glob(".colink-write-*"))


def test_quota_rejection_leaves_source_and_pending_state_unchanged(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    original = source.read("a.py")
    actual = store.reserve

    def reserve(**kwargs):
        if kwargs.get("object_bytes", 0):
            raise RecoveryError("RECOVERY_CAPACITY: injected")
        return actual(**kwargs)

    monkeypatch.setattr(store, "reserve", reserve)
    with pytest.raises(RecoveryError, match="RECOVERY_CAPACITY"):
        edit(source, c, task)
    assert source.read("a.py") == original
    assert store.query("SELECT * FROM operations") == []
    assert not c.status()["recovery_required"]


def test_unicode_result_near_metadata_cap_is_rejected_before_source_edit(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    original = "😀" * 4000
    (root / "a.py").write_text(original)
    source = SourceAccess(root)
    with RecoveryStore(
        tmp_path / "recovery",
        max_bytes=8 * 1024 * 1024,
        max_peak_bytes=16 * 1024 * 1024,
        max_metadata_bytes=1024 * 1024,
        min_free_bytes=0,
    ) as store:
        c = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
        try:
            task = begin(c, paths=["a.py"])
            # Synthetic SQL pressure reproduces admission with ~68 KiB left.
            target = store.max_metadata_bytes - 17 * 4096
            length = target - store.usage()["database_page_bytes"] - 4096
            for _ in range(8):
                with store.transaction() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES('test_padding',?)", ("q" * length,)
                    )
                actual = store.usage()["database_page_bytes"]
                if actual == target:
                    break
                length += target - actual
            assert actual == target
            before = source.read("a.py")
            with pytest.raises(RecoveryError, match="RECOVERY_METADATA_CAPACITY"):
                c.apply_edit(
                    "a",
                    task,
                    "edit_0001",
                    "a.py",
                    before.sha256,
                    {"kind": "replace_fragment", "old_text": original, "new_text": "😃" * 4000},
                )
            assert source.read("a.py") == before
            assert store.query("SELECT * FROM operations") == []
            assert store.query("SELECT * FROM objects") == []
            assert not c.status()["recovery_required"]
        finally:
            c.close()


def test_growth_admission_protects_latest_bodies_for_mkdir_and_source_disk(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    c.create_file("a", task, "create_001", "new.py", "latest\n")
    actual = store.reserve
    calls = []

    def reserve(**kwargs):
        calls.append(kwargs)
        return actual(**kwargs)

    monkeypatch.setattr(store, "reserve", reserve)
    c.create_directory("a", task, "mkdir_0001", "directory")
    assert any(
        call.get("object_bytes", 0) >= 4096 and call.get("metadata_bytes", 0) >= 256 * 1024
        for call in calls
    )
    monkeypatch.setattr(
        "code_context.write_coordinator.shutil.disk_usage",
        lambda _: type("Space", (), {"free": store.min_free_bytes})(),
    )
    with pytest.raises(WriteError, match="WRITE_DISK_SPACE"):
        c.create_directory("a", task, "mkdir_0002", "other")
    assert not (source.root / "other").exists()
    assert not c.status()["recovery_required"]

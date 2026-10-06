"""Only synthetic files under a fresh pytest fixture can be deleted here."""

import os

import pytest

import code_context.file_removal as removal
import code_context.write_deletion as deletion
import code_context.write_rollback as rollback
from code_context.file_attributes import _set_xattr
from code_context.file_mutation import read_file_attributes
from code_context.recovery_store import RecoveryError, RecoveryStore
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator, WriteError


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")
    (root / "b.py").write_text("unchanged\n")
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    coordinator = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    yield source, store, coordinator
    coordinator.close()
    store.close()


def begin(c, **kwargs):
    c.enable(["a"])
    return c.begin_write_task("a", c.status()["next_task_request_id"], **kwargs)["task_id"]


def delete(source, c, task, path="a.py", request="delete_001"):
    return c.delete_file("a", task, request, path, source.read(path).sha256)


def edit(source, c, task):
    return c.apply_edit(
        "a",
        task,
        "edit_0001",
        "a.py",
        source.read("a.py").sha256,
        {"kind": "replace_fragment", "old_text": "first", "new_text": "updated"},
    )


def test_delete_diff_finish_and_undo_restore_exact_origin_and_attributes(parts):
    source, store, c = parts
    raw = (source.root / "a.py").read_bytes()
    os.chmod(source.root / "a.py", 0o640)
    fd = os.open(source.root / "a.py", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        _set_xattr(fd, "user.colink.synthetic", b"synthetic attribute")
    finally:
        os.close(fd)
    origin_attributes = read_file_attributes(source, source.read("a.py"))
    task = begin(c, paths=["a.py", "b.py"])
    saved = delete(source, c, task)
    assert saved["state"] == "deleted" and saved["readback_verified"]
    assert not (source.root / "a.py").exists()
    assert store.usage()["object_count"] == 1
    diff = c.get_diff("a", path="a.py", detail="patch")
    assert diff["summary"]["deleted"] == 1
    assert diff["changes"][0]["op"] == "delete"
    assert "-second" in diff["changes"][0]["patch"]
    c.finish_write_task("a", task, "finish_001")
    result = c.rollback_write_task("a", task, "rollback_001")
    assert result["files_restored"] == 1
    assert (source.root / "a.py").read_bytes() == raw
    assert source.read("a.py").mode == 0o640
    assert read_file_attributes(source, source.read("a.py")) == origin_attributes
    assert c.get_diff("a")["summary"]["files_changed"] == 0
    assert not list(source.root.glob(".colink-write-*"))


def test_retry_never_removes_a_recreated_external_file(parts):
    source, store, c = parts
    task = begin(c)
    sha = source.read("a.py").sha256
    args = ("a", task, "delete_001", "a.py", sha)
    first = c.delete_file(*args)
    (source.root / "a.py").write_text("external replacement\n")
    assert c.delete_file(*args) == first
    assert (source.root / "a.py").read_text() == "external replacement\n"
    with pytest.raises(WriteError, match="REQUEST_ID_CONFLICT"):
        c.delete_file("a", task, "delete_001", "b.py", source.read("b.py").sha256)
    with pytest.raises(SourceError):
        c.get_diff("a")
    with pytest.raises(WriteError, match="WRITE_ROLLBACK_CONFLICT"):
        c.rollback_write_task("a", task, "rollback_001")
    assert store.query("SELECT count(*) AS n FROM operations")[0]["n"] == 1


@pytest.mark.parametrize(
    "kind", ["disabled", "hash", "scope", "directory", "symlink", "hardlink", "late"]
)
def test_rejections_never_delete_or_create_journal(parts, kind):
    source, store, c = parts
    task = begin(c, paths=["b.py"] if kind == "scope" else None)
    sha = source.read("a.py").sha256
    if kind == "disabled":
        c.disable()
    elif kind == "hash":
        sha = "0" * 64
    elif kind == "directory":
        (source.root / "a.py").unlink()
        (source.root / "a.py").mkdir()
    elif kind == "symlink":
        (source.root / "a.py").unlink()
        (source.root / "a.py").symlink_to("b.py")
    elif kind == "hardlink":
        os.link(source.root / "a.py", source.root / "second.py")
    elif kind == "late":
        (source.root / "a.py").write_text("late external content\n")
        sha = source.read("a.py").sha256
    with pytest.raises(SourceError):
        c.delete_file("a", task, "delete_001", "a.py", sha)
    assert (source.root / "a.py").exists()
    assert store.query("SELECT * FROM operations") == []


@pytest.mark.parametrize(
    "sequence", ["edit-delete", "delete-recreate", "create-delete", "create-delete-recreate"]
)
def test_lifecycle_keeps_one_task_origin_and_whole_undo(parts, sequence):
    source, store, c = parts
    raw = (source.root / "a.py").read_bytes()
    task = begin(c, paths=["a.py", "new.py"])
    path = "a.py"
    if sequence.startswith("create"):
        path = "new.py"
        c.create_file("a", task, "create_001", path, "created\n")
    elif sequence == "edit-delete":
        edit(source, c, task)
    delete(source, c, task, path=path)
    if sequence.endswith("recreate"):
        c.create_file("a", task, "create_002", path, "recreated\n")
    assert c.get_diff("a")["summary"]["files_changed"] == (0 if sequence == "create-delete" else 1)
    c.finish_write_task("a", task, "finish_001")
    c.rollback_write_task("a", task, "rollback_001")
    assert (source.root / "a.py").read_bytes() == raw
    assert not (source.root / "new.py").exists()
    assert c.get_diff("a")["summary"]["files_changed"] == 0
    assert store.usage()["object_count"] <= 2


def test_created_directory_with_created_then_deleted_file_rolls_back(parts):
    source, _, c = parts
    task = begin(c, paths=["new", "new/a.py"])
    c.create_directory("a", task, "mkdir_001", "new")
    c.create_file("a", task, "create_001", "new/a.py", "created\n")
    delete(source, c, task, path="new/a.py")
    c.rollback_write_task("a", task, "rollback_001")
    assert not (source.root / "new").exists()
    assert c.get_diff("a")["summary"]["files_changed"] == 0


def test_multi_file_preflight_refuses_foreign_reappearance_without_partial_restore(parts):
    source, _, c = parts
    task = begin(c)
    edit(source, c, task)
    delete(source, c, task, path="b.py")
    (source.root / "b.py").write_text("external\n")
    with pytest.raises(WriteError, match="WRITE_ROLLBACK_CONFLICT"):
        c.rollback_write_task("a", task, "rollback_001")
    assert "updated" in source.read("a.py").content
    assert (source.root / "b.py").read_text() == "external\n"


def test_capacity_failure_happens_before_intent_or_source_mutation(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)

    def refuse(**_):
        raise RecoveryError("limit")

    monkeypatch.setattr(store, "reserve", refuse)
    with pytest.raises(RecoveryError):
        delete(source, c, task)
    assert (source.root / "a.py").exists()
    assert store.query("SELECT * FROM operations") == []


def test_backup_failure_aborts_without_deleting(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = store.put_blob

    def refuse(*_):
        raise RecoveryError("stop")

    monkeypatch.setattr(store, "put_blob", refuse)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        delete(source, c, task)
    assert (source.root / "a.py").exists()
    monkeypatch.setattr(store, "put_blob", actual)
    assert c.recover("a")["outcomes"][0]["state"] == "aborted"
    assert not c.status()["write_enabled"]
    assert store.usage()["object_count"] == 0


@pytest.mark.parametrize("phase", ["before-move", "isolated", "unlinked"])
def test_interrupted_delete_recovers_without_repeating_source_mutation(parts, monkeypatch, phase):
    source, store, c = parts
    raw = (source.root / "a.py").read_bytes()
    task = begin(c)
    sha = source.read("a.py").sha256
    actual = deletion.remove_created_file

    def interrupted(source, expected, temporary, moved, attrs):
        if phase == "before-move":
            raise RuntimeError("before native mutation")
        if phase == "isolated":

            def stopped(receipt):
                moved(receipt)
                raise RuntimeError("after durable isolation proof")

            return actual(source, expected, temporary, stopped, attrs)
        actual(source, expected, temporary, moved, attrs)
        raise RuntimeError("after verified unlink")

    monkeypatch.setattr(deletion, "remove_created_file", interrupted)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.delete_file("a", task, "delete_001", "a.py", sha)
    c.close()
    other = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    assert other.status()["recovery_required"] and not other.status()["write_enabled"]
    assert other.recover("a")["outcomes"][0]["state"] == (
        "aborted" if phase == "before-move" else "deleted"
    )
    assert not other.status()["write_enabled"]
    if phase != "before-move":
        other.enable(["a"])
        assert other.delete_file("a", task, "delete_001", "a.py", sha)["state"] == "deleted"
        other.rollback_write_task("a", task, "rollback_001")
    assert (source.root / "a.py").read_bytes() == raw
    assert not list(source.root.glob(".colink-write-*"))
    other.close()


def test_foreign_delete_without_registered_isolation_is_not_adopted(parts, monkeypatch):
    source, _, c = parts
    task = begin(c)

    def external(source, expected, *_):
        (source.root / expected.path).unlink()
        raise RuntimeError("foreign deletion before our native mutation")

    monkeypatch.setattr(deletion, "remove_created_file", external)
    with pytest.raises(WriteError):
        delete(source, c, task)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert c.status()["recovery_required"]


def test_move_before_callback_is_verified_locally_without_second_move(parts, monkeypatch):
    source, _, c = parts
    task = begin(c)
    actual = removal._rename_excl

    def interrupted(*args):
        actual(*args)
        raise RuntimeError("native move succeeded before callback")

    monkeypatch.setattr(removal, "_rename_excl", interrupted)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        delete(source, c, task)
    assert not (source.root / "a.py").exists()
    assert len(list(source.root.glob(".colink-write-*"))) == 1
    assert c.recover("a")["outcomes"][0]["state"] == "deleted"
    assert not list(source.root.glob(".colink-write-*"))
    assert not c.status()["write_enabled"]


def test_foreign_object_swapped_before_move_is_preserved_not_unlinked(parts, monkeypatch):
    source, _, c = parts
    task = begin(c)
    actual = removal._rename_excl

    def replace_then_move(parent, target, temporary):
        os.unlink(target, dir_fd=parent)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent)
        try:
            os.write(fd, b"foreign replacement\n")
        finally:
            os.close(fd)
        return actual(parent, target, temporary)

    monkeypatch.setattr(removal, "_rename_excl", replace_then_move)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        delete(source, c, task)
    temporaries = list(source.root.glob(".colink-write-*"))
    assert len(temporaries) == 1 and temporaries[0].read_bytes() == b"foreign replacement\n"
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert temporaries[0].read_bytes() == b"foreign replacement\n"


def test_deleted_parent_replacement_cannot_be_adopted(parts):
    source, _, c = parts
    folder = source.root / "folder"
    folder.mkdir()
    (folder / "a.py").write_text("origin\n")
    task = begin(c, paths=["folder/a.py"])
    delete(source, c, task, path="folder/a.py")
    folder.rename(source.root / "saved-parent")
    folder.mkdir()
    for operation in (
        lambda: c.create_file("a", task, "create_002", "folder/a.py", "not adopted\n"),
        lambda: c.get_diff("a"),
        lambda: c.rollback_write_task("a", task, "rollback_001"),
    ):
        with pytest.raises(SourceError):
            operation()
    assert not (folder / "a.py").exists()


@pytest.mark.parametrize("phase", ["prepared", "installed", "cleaned"])
def test_interrupted_deleted_restore_resumes_without_overwrite(parts, monkeypatch, phase):
    source, store, c = parts
    raw = (source.root / "a.py").read_bytes()
    task = begin(c)
    delete(source, c, task)
    method = {
        "prepared": "prepare_file",
        "installed": "commit_file",
        "cleaned": "discard_prepared",
    }[phase]
    actual = getattr(rollback, method)

    def interrupted(*args, **kwargs):
        actual(*args, **kwargs)
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(rollback, method, interrupted)
    with pytest.raises(WriteError, match="WRITE_ROLLBACK_RECOVERY_REQUIRED"):
        c.rollback_write_task("a", task, "rollback_001")
    monkeypatch.setattr(rollback, method, actual)
    c.close()
    other = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    assert other.recover("a")["state"] == "rolled_back"
    assert (source.root / "a.py").read_bytes() == raw
    assert other.get_diff("a")["summary"]["files_changed"] == 0
    assert not list(source.root.glob(".colink-write-*"))
    other.close()

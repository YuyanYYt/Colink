import json
import os

import pytest

import code_context.write_operations as operations
from code_context.recovery_store import RecoveryError, RecoveryStore
from code_context.source_access import SourceAccess
from code_context.write_coordinator import WriteCoordinator, WriteError


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\n")
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    c = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    yield source, store, c
    c.close()
    store.close()


def begin(c):
    c.enable(["a"])
    return c.begin_write_task("a", c.status()["next_task_request_id"])["task_id"]


def edit(source, c, task):
    return c.apply_edit(
        "a",
        task,
        "edit_0001",
        "a.py",
        source.read("a.py").sha256,
        {"kind": "replace_fragment", "old_text": "one", "new_text": "updated"},
    )


def interrupt_after_prepare(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = operations.prepare_file

    def prepare(*args, **kwargs):
        value = actual(*args, **kwargs)
        c.disable()
        return value

    monkeypatch.setattr(operations, "prepare_file", prepare)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        edit(source, c, task)
    return task


def test_precommit_registered_file_is_aborted_no_repeat_no_grant(parts, monkeypatch):
    source, store, c = parts
    task = interrupt_after_prepare(parts, monkeypatch)
    result = c.recover("a")
    assert result["outcomes"][0]["state"] == "aborted"
    assert not c.status()["write_enabled"] and not c.status()["recovery_required"]
    assert (source.root / "a.py").read_text() == "one\ntwo\n"
    assert not list(source.root.glob(".colink-write-*"))
    assert store.usage()["object_count"] == 0
    c.enable(["a"])
    with pytest.raises(WriteError, match="WRITE_OPERATION_ABORTED"):
        edit(source, c, task)


def test_backup_failure_before_any_object_aborts_locally(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = store.put_blob

    def fail(*args):
        raise RecoveryError("RECOVERY_SAVE_FAILED: injected")

    monkeypatch.setattr(store, "put_blob", fail)
    with pytest.raises(WriteError):
        edit(source, c, task)
    monkeypatch.setattr(store, "put_blob", actual)
    assert c.recover("a")["outcomes"][0]["state"] == "aborted"
    assert (source.root / "a.py").read_text() == "one\ntwo\n"


@pytest.mark.parametrize("kind", ["edit", "create"])
def test_crash_after_native_install_recovers_without_second_mutation(parts, monkeypatch, kind):
    source, store, c = parts
    task = begin(c)
    actual = operations.commit_file

    def commit(*args):
        actual(*args)
        raise RuntimeError("simulated process stop after native call")

    monkeypatch.setattr(operations, "commit_file", commit)
    with pytest.raises(WriteError):
        if kind == "edit":
            edit(source, c, task)
        else:
            c.create_file("a", task, "create_001", "new.py", "created\n")
    path = "a.py" if kind == "edit" else "new.py"
    inode = os.stat(source.root / path).st_ino
    c.close()
    fresh = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    assert fresh.status()["recovery_required"] and not fresh.status()["write_enabled"]
    result = fresh.recover("a")
    assert result["outcomes"][0]["state"] == "saved"
    assert os.stat(source.root / path).st_ino == inode
    assert not list(source.root.glob(".colink-write-*"))
    assert os.stat(source.root / path).st_nlink == 1
    assert fresh.get_diff("a")["summary"]["files_changed"] == 1
    fresh.enable(["a"])
    if kind == "create":
        assert fresh.create_file("a", task, "create_001", "new.py", "created\n")["state"] == "saved"
    fresh.close()


def test_crash_after_temp_cleanup_can_confirm_persisted_verified_install(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = operations.discard_prepared

    def discard(*args):
        actual(*args)
        raise RuntimeError("simulated process stop after cleanup")

    monkeypatch.setattr(operations, "discard_prepared", discard)
    with pytest.raises(WriteError):
        edit(source, c, task)
    assert not list(source.root.glob(".colink-write-*"))
    assert c.recover("a")["outcomes"][0]["state"] == "saved"
    assert c.get_diff("a")["summary"]["files_changed"] == 1


@pytest.mark.parametrize("kind", ["unknown", "altered", "permissions", "outside_change"])
def test_unknown_or_changed_material_is_preserved(parts, monkeypatch, kind):
    source, store, c = parts
    if kind == "unknown":
        task = begin(c)

        def prepare(source, path, raw, mode, name, on_created, attributes=None, on_attributes=None):
            (source.root / name).write_text("unknown bytes")
            raise RuntimeError("creation identity never recorded")

        monkeypatch.setattr(operations, "prepare_file", prepare)
        with pytest.raises(WriteError):
            edit(source, c, task)
    else:
        task = interrupt_after_prepare(parts, monkeypatch)
    temp = next(source.root.glob(".colink-write-*"))
    if kind == "altered":
        temp.write_text("externally altered")
    elif kind == "permissions":
        temp.chmod(0o666)
    elif kind == "outside_change":
        (source.root / "a.py").write_text("external\n")
    before = temp.read_bytes()
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert temp.read_bytes() == before and c.status()["recovery_required"]
    assert not c.status()["write_enabled"]


@pytest.mark.parametrize("phase", ["prepared", "installed"])
def test_directory_interruption_reconciles_known_identity(parts, monkeypatch, phase):
    source, store, c = parts
    task = begin(c)
    if phase == "prepared":
        actual = operations.prepare_directory

        def prepare(*args):
            value = actual(*args)
            c.disable()
            return value

        monkeypatch.setattr(operations, "prepare_directory", prepare)
    else:
        actual = operations.commit_directory

        def commit(*args):
            actual(*args)
            raise RuntimeError("simulated stop after directory install")

        monkeypatch.setattr(operations, "commit_directory", commit)
    with pytest.raises(WriteError):
        c.create_directory("a", task, "mkdir_001", "new")
    result = c.recover("a")
    assert result["outcomes"][0]["state"] == ("aborted" if phase == "prepared" else "created")
    assert (source.root / "new").exists() == (phase == "installed")
    assert not list(source.root.glob(".colink-write-*"))


def test_directory_foreign_contents_after_install_not_adopted_as_task_files(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = operations.commit_directory

    def commit(*args):
        actual(*args)
        (source.root / "new" / "external.py").write_text("external\n")
        raise RuntimeError("simulated stop")

    monkeypatch.setattr(operations, "commit_directory", commit)
    with pytest.raises(WriteError):
        c.create_directory("a", task, "mkdir_001", "new")
    c.recover("a")
    assert (source.root / "new" / "external.py").read_text() == "external\n"
    assert [row["path"] for row in store.query("SELECT * FROM files")] == ["new"]


def test_recovery_requires_live_local_control_and_original_source(parts, monkeypatch):
    source, store, c = parts
    interrupt_after_prepare(parts, monkeypatch)
    c.control_alive = lambda: False
    with pytest.raises(WriteError, match="LOCAL_CONTROL_UNAVAILABLE"):
        c.recover("a")
    assert c.status()["recovery_required"]
    c.control_alive = lambda: True
    source.root.rename(source.root.parent / "original")
    source.root.mkdir()
    c.source_provider = lambda _: SourceAccess(source.root)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_SCOPE"):
        c.recover("a")


def test_corrupt_scope_binding_does_not_clean_other_paths(parts, monkeypatch):
    source, store, c = parts
    task = interrupt_after_prepare(parts, monkeypatch)
    row = store.query("SELECT * FROM operations")[0]
    metadata = json.loads(row["metadata"])
    metadata["prepared"]["path"] = "b.py"
    with store.transaction() as db:
        db.execute("UPDATE operations SET metadata=? WHERE task_id=?", (json.dumps(metadata), task))
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert len(list(source.root.glob(".colink-write-*"))) == 1


def test_unverified_captured_swap_cannot_be_promoted_to_verified_installed(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = operations.commit_file

    def commit(*args):
        actual(*args)
        temp = source.root / args[1].temp_name
        temp.write_text("unconfirmed displaced external bytes\n")
        raise RuntimeError("simulated stop before verified install record")

    monkeypatch.setattr(operations, "commit_file", commit)
    with pytest.raises(WriteError):
        edit(source, c, task)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    metadata = json.loads(store.query("SELECT * FROM operations")[0]["metadata"])
    assert metadata["phase"] == "installing"
    assert store.query("SELECT * FROM files") == []
    # Absence cannot later become proof that the unknown exchanged body matched.
    next(source.root.glob(".colink-write-*")).unlink()
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")


def test_complete_pending_content_object_can_be_confirmed_by_hash(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = store.put_blob

    def put(*args):
        sha = actual(*args)
        with store.transaction() as db:
            db.execute("UPDATE objects SET state='pending' WHERE sha256=?", (sha,))
        raise RuntimeError("simulated stop before ready state was durable")

    monkeypatch.setattr(store, "put_blob", put)
    with pytest.raises(WriteError):
        edit(source, c, task)
    assert c.recover("a")["outcomes"][0]["state"] == "aborted"
    assert store.usage()["object_count"] == 0


def test_missing_pending_object_intent_can_abort_without_deleting_unknown_bytes(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)

    def put(raw, owner):
        import hashlib

        sha = hashlib.sha256(raw).hexdigest()
        with store.transaction() as db:
            db.execute("INSERT INTO objects VALUES(?,?,?,NULL)", (sha, len(raw), "pending"))
            db.execute("INSERT INTO object_refs VALUES(?,?)", (owner, sha))
        raise RuntimeError("simulated stop before creating object")

    monkeypatch.setattr(store, "put_blob", put)
    with pytest.raises(WriteError):
        edit(source, c, task)
    assert c.recover("a")["outcomes"][0]["state"] == "aborted"
    assert store.usage()["object_count"] == 0


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_recreate_disappeared_task_owned_path_rejects_before_journal(parts, kind):
    source, store, c = parts
    task = begin(c)
    if kind == "file":
        c.create_file("a", task, "create_001", "new.py", "created\n")
        (source.root / "new.py").unlink()

        def action():
            return c.create_file("a", task, "create_002", "new.py", "replacement\n")
    else:
        c.create_directory("a", task, "mkdir_001", "new")
        (source.root / "new").rmdir()

        def action():
            return c.create_directory("a", task, "mkdir_002", "new")

    count = len(store.query("SELECT * FROM operations"))
    with pytest.raises(WriteError, match="WRITE_FILE_CONFLICT"):
        action()
    assert len(store.query("SELECT * FROM operations")) == count
    assert not list(source.root.glob(".colink-write-*"))


def test_enable_failure_never_leaves_an_unacknowledged_write_grant(parts, monkeypatch):
    source, store, c = parts
    c.enable(["a"])

    def fail():
        raise RecoveryError("RECOVERY_OBJECT_CHANGED: injected retirement failure")

    monkeypatch.setattr(c, "_retire_expired", fail)
    with pytest.raises(RecoveryError):
        c.enable(["a"])
    assert not c.status()["write_enabled"] and c.grants == {}


def test_hardlinked_existing_file_refuses_before_backup_or_pending_intent(parts):
    source, store, c = parts
    os.link(source.root / "a.py", source.root / "linked.py")
    task = begin(c)
    with pytest.raises(WriteError, match="WRITE_UNSAFE_FILE"):
        edit(source, c, task)
    assert store.query("SELECT * FROM operations") == []
    assert store.query("SELECT * FROM objects") == []

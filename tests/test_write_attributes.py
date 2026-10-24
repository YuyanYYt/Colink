import json
import os
import sys
from dataclasses import replace

import pytest

import code_context.file_attributes as attributes
import code_context.file_mutation as mutation
from code_context.file_attributes import _set_xattr
from code_context.file_mutation import read_file_attributes
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator, WriteError


def test_replacement_attributes_only_use_new_darwin_provenance():
    original = attributes.FileAttributes(
        "darwin",
        501,
        20,
        0o640,
        xattrs=(("com.apple.provenance", b"original"), ("user.keep", b"saved")),
    )
    creation = replace(
        original,
        mode=0o600,
        xattrs=(("com.apple.provenance", b"new-inode"), ("user.unwanted", b"drop")),
    )
    wanted = attributes.replacement_file_attributes(original, creation)
    assert dict(wanted.xattrs) == {"com.apple.provenance": b"new-inode", "user.keep": b"saved"}
    assert replace(wanted, xattrs=original.xattrs) == original
    assert attributes.FileAttributes.from_record(original.to_record()) == original
    linux = replace(original, platform="linux", xattrs=(("user.keep", b"saved"),))
    assert attributes.replacement_file_attributes(linux, replace(linux, xattrs=())) == linux


def different_original_provenance(source, monkeypatch):
    inode = (source.root / "a.py").stat().st_ino
    actual_get = attributes._get_xattrs
    actual_set = attributes._set_xattr

    def get(fd):
        values = dict(actual_get(fd))
        if os.fstat(fd).st_ino == inode:
            values["com.apple.provenance"] = b"recorded-original-inode"
        return tuple(sorted(values.items()))

    def set_attribute(fd, name, value):
        if name == "com.apple.provenance":
            pytest.fail("must retain the kernel's own new-inode provenance")
        return actual_set(fd, name, value)

    monkeypatch.setattr(attributes, "_get_xattrs", get)
    monkeypatch.setattr(attributes, "_set_xattr", set_attribute)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin per-inode provenance")
def test_edit_journals_new_inode_provenance_and_preserves_original_record(parts, monkeypatch):
    source, store, c = parts
    different_original_provenance(source, monkeypatch)
    set_test_attribute(source.root / "a.py", "user.colink.keep", b"original-metadata")
    before = read_file_attributes(source, source.read("a.py"))
    task = begin(c)
    assert edit(source, c, task)["readback_verified"]
    actual = read_file_attributes(source, source.read("a.py"))
    saved = store.query("SELECT origin_record,last_record FROM file_attributes")[0]
    assert json.loads(saved["origin_record"]) == before.to_record()
    assert json.loads(saved["last_record"]) == actual.to_record()
    assert dict(actual.xattrs)["user.colink.keep"] == b"original-metadata"
    assert actual == attributes.replacement_file_attributes(before, actual)
    assert actual != before
    assert not c.status()["recovery_required"]
    edit(source, c, task, "edit_0002", "2", "3")
    assert c.finish_write_task("a", task, "finish_001")["state"] == "completed"


@pytest.mark.skipif(sys.platform != "darwin", reason="Build 33 Darwin recovery")
@pytest.mark.parametrize(
    "conflict", [None, "temp_xattr", "body", "original_xattr", "identity", "phase"]
)
def test_legacy_preparation_aborts_only_provenance_difference(parts, monkeypatch, conflict):
    import code_context.write_operations as operations

    source, store, c = parts
    different_original_provenance(source, monkeypatch)
    before = source.read("a.py")
    original_attrs = read_file_attributes(source, before)
    task = begin(c)
    prepare = operations.prepare_file

    def interrupted(*args, **kwargs):
        prepare(*args, **kwargs)
        row = store.query("SELECT metadata FROM operations")[0]
        metadata = json.loads(row["metadata"])
        metadata["prepared"]["attribute_sha256"] = original_attrs.sha256
        with store.transaction() as db:
            db.execute("UPDATE operation_attributes SET after_record=before_record")
            db.execute("UPDATE operations SET metadata=?", (json.dumps(metadata),))
        raise RuntimeError("legacy process stopped after mismatched attribute preparation")

    monkeypatch.setattr(operations, "prepare_file", interrupted)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        edit(source, c, task)
    metadata = json.loads(store.query("SELECT metadata FROM operations")[0]["metadata"])
    temp = source.root / metadata["path"]
    temp = temp.parent / metadata["temp_name"]
    if conflict == "temp_xattr":
        set_test_attribute(temp, "user.colink.external", b"changed")
    elif conflict == "body":
        temp.write_text("external body\n")
    elif conflict == "original_xattr":
        set_test_attribute(source.root / "a.py", "user.colink.external", b"changed")
    elif conflict == "identity":
        metadata["prepared"]["temp_identity"][1] += 1
    elif conflict == "phase":
        metadata["phase"] = "installing"
    with store.transaction() as db:
        db.execute("UPDATE operations SET metadata=?", (json.dumps(metadata),))
    if conflict:
        with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
            c.recover("a")
        assert temp.exists()
        assert c.status()["recovery_required"]
    else:
        result = c.recover("a")
        assert result["outcomes"] == [{"state": "aborted", "source_mutation_repeated": False}]
        assert not temp.exists()
        assert not c.status()["recovery_required"]
        assert read_file_attributes(source, source.read("a.py")) == original_attrs
    assert source.read("a.py").content == before.content
    assert not c.status()["write_enabled"]


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("VALUE = 1\n")
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    c = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    yield source, store, c
    c.close()
    store.close()


def begin(c):
    c.enable(["a"])
    return c.begin_write_task("a", c.status()["next_task_request_id"])["task_id"]


def edit(source, c, task, request="edit_0001", old="1", new="2"):
    return c.apply_edit(
        "a",
        task,
        request,
        "a.py",
        source.read("a.py").sha256,
        {"kind": "replace_fragment", "old_text": old, "new_text": new},
    )


def set_test_attribute(path, name, value):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        _set_xattr(fd, name, value)
    finally:
        os.close(fd)


def test_original_mode_gid_xattrs_persist_without_attribute_copy_per_edit(parts):
    source, store, c = parts
    path = source.root / "a.py"
    path.chmod(0o640)
    set_test_attribute(path, "user.colink.synthetic", "metadata文字".encode())
    # A real alternate supplementary group tests non-parent-GID preservation.
    alternate = next((group for group in os.getgroups() if group != path.stat().st_gid), None)
    if alternate is not None:
        os.chown(path, -1, alternate)
    original = read_file_attributes(source, source.read("a.py"))
    task = begin(c)
    edit(source, c, task)
    edit(source, c, task, "edit_0002", "2", "3")
    assert read_file_attributes(source, source.read("a.py")) == original
    records = store.query("SELECT * FROM file_attributes")
    assert len(records) == 1
    assert json.loads(records[0]["origin_record"]) == json.loads(records[0]["last_record"])
    assert store.query("SELECT * FROM operation_attributes") == []
    assert c.get_diff("a")["summary"]["files_changed"] == 1
    assert c.finish_write_task("a", task, "finish_001")["state"] == "completed"


def test_new_file_retains_actual_platform_attributes_and_saved_identity(parts):
    source, store, c = parts
    task = begin(c)
    c.create_file("a", task, "create_001", "new.py", "new\n")
    actual = read_file_attributes(source, source.read("new.py"))
    saved = store.query("SELECT * FROM file_attributes WHERE path='new.py'")[0]
    assert json.loads(saved["last_record"]) == actual.to_record()
    assert saved["origin_record"] is None
    assert store.query("SELECT * FROM operation_attributes") == []


def test_late_external_attribute_change_is_pending_not_labeled_verified(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = mutation._exchange

    def exchange(parent, temporary, target):
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            _set_xattr(fd, "user.colink.external", b"outside")
        finally:
            os.close(fd)
        return actual(parent, temporary, target)

    monkeypatch.setattr(mutation, "_exchange", exchange)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        edit(source, c, task)
    assert (source.root / "a.py").read_text() == "VALUE = 2\n"
    assert len(list(source.root.glob(".colink-write-*"))) == 1
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert (
        json.loads(store.query("SELECT metadata FROM operations")[0]["metadata"])["phase"]
        == "installing"
    )
    assert not c.status()["write_enabled"] and c.status()["recovery_required"]


def test_created_directory_attribute_change_refuses_finish_and_diff(parts):
    source, store, c = parts
    task = begin(c)
    c.create_directory("a", task, "mkdir_0001", "directory")
    set_test_attribute(source.root / "directory", "user.colink.external", b"outside")
    for query in (lambda: c.get_diff("a"), lambda: c.finish_write_task("a", task, "finish_001")):
        with pytest.raises(WriteError, match="WRITE_ATTRIBUTE_CONFLICT"):
            query()
    assert store.query("SELECT state FROM tasks")[0]["state"] == "active"
    assert (source.root / "directory").is_dir()
    with pytest.raises(WriteError, match="WRITE_ATTRIBUTE_CONFLICT"):
        c.create_file(
            "a", task, "create_001", "directory/new.py", "no extension into changed ACL\n"
        )
    with pytest.raises(WriteError, match="WRITE_ATTRIBUTE_CONFLICT"):
        c.create_directory("a", task, "mkdir_0002", "directory/nested")
    assert not (source.root / "directory/new.py").exists()
    assert not (source.root / "directory/nested").exists()


def test_unsupported_attribute_budget_refuses_before_intent_or_source_change(parts, monkeypatch):
    source, store, c = parts
    original = source.read("a.py")
    # Linux filesystems may reject a 64 KiB xattr before our capture runs. Test
    # the same native admission path with a small test-only bound; default
    # 64 KiB/128 KiB boundaries remain covered in test_file_attributes.py.
    monkeypatch.setattr(attributes, "MAX_XATTR_BYTES", 128)
    set_test_attribute(source.root / "a.py", "user.colink.synthetic", b"x" * 129)
    task = begin(c)
    with pytest.raises(SourceError, match="ATTRIBUTE_BUDGET"):
        edit(source, c, task)
    assert (source.root / "a.py").read_text() == original.content
    assert store.query("SELECT * FROM operations") == []
    assert not c.status()["recovery_required"]


def test_whole_rollback_proves_native_attributes_and_zero_diff(parts):
    source, store, c = parts
    set_test_attribute(source.root / "a.py", "user.colink.synthetic", b"saved-original")
    original = read_file_attributes(source, source.read("a.py"))
    task = begin(c)
    edit(source, c, task)
    c.create_directory("a", task, "mkdir_0001", "created")
    c.create_file("a", task, "create_001", "created/b.py", "B = 2\n")
    assert c.get_diff("a")["summary"]["files_changed"] == 2
    result = c.rollback.rollback("a", task, "rollback_001")  # Old journal compatibility.
    assert result["readback_verified"] and result["state"] == "rolled_back"
    assert read_file_attributes(source, source.read("a.py")) == original
    assert not (source.root / "created").exists()
    assert c.get_diff("a")["summary"]["files_changed"] == 0
    assert c.rollback.rollback("a", task, "rollback_001") == result
    # Zero must not hide a new external change after a verified rollback.
    (source.root / "a.py").write_text("external now\n")
    with pytest.raises(WriteError, match="WRITE_ROLLBACK_CONFLICT"):
        c.get_diff("a")


def test_attribute_conflict_in_late_participant_causes_zero_rollback_changes(parts):
    source, store, c = parts
    task = begin(c)
    edit(source, c, task)
    c.create_file("a", task, "create_001", "z.py", "last participant\n")
    set_test_attribute(source.root / "z.py", "user.colink.external", b"outside")
    with pytest.raises(WriteError, match="WRITE_ROLLBACK_CONFLICT"):
        c.rollback.rollback("a", task, "rollback_001")
    assert (source.root / "a.py").read_text() == "VALUE = 2\n"
    assert (source.root / "z.py").exists()
    assert not store.query("SELECT * FROM rollback_items")

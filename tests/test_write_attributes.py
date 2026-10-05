import json
import os

import pytest

import code_context.file_mutation as mutation
from code_context.file_attributes import _set_xattr
from code_context.file_mutation import read_file_attributes
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator, WriteError


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
    set_test_attribute(path, "com.colink.synthetic", "metadata文字".encode())
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
            _set_xattr(fd, "com.colink.external", b"outside")
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
    set_test_attribute(source.root / "directory", "com.colink.external", b"outside")
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


def test_unsupported_attribute_budget_refuses_before_intent_or_source_change(parts):
    source, store, c = parts
    original = source.read("a.py")
    set_test_attribute(source.root / "a.py", "com.colink.synthetic", b"x" * (64 * 1024 + 1))
    task = begin(c)
    with pytest.raises(SourceError, match="ATTRIBUTE_BUDGET"):
        edit(source, c, task)
    assert (source.root / "a.py").read_text() == original.content
    assert store.query("SELECT * FROM operations") == []
    assert not c.status()["recovery_required"]

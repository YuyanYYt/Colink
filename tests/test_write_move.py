"""Real same-volume no-overwrite moves, durable interruptions and task origins."""

import hashlib
import json
import os
import stat

import pytest

from code_context import write_move
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator, WriteError

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
)


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_bytes(b"first\r\n")
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    coordinator = WriteCoordinator(store, lambda _project: source, control_alive=lambda: True)
    yield source, store, coordinator
    coordinator.close()
    store.close()


def begin(parts, *, paths=None):
    _, _, c = parts
    c.enable(["a"])
    return c.begin_write_task("a", c.status()["next_task_request_id"], paths=paths)["task_id"]


def edit(parts, task, request, path, old, new):
    source, _, c = parts
    return c.apply_edit(
        "a",
        task,
        request,
        path,
        source.read(path).sha256,
        {"kind": "replace_fragment", "old_text": old, "new_text": new},
    )


def tree(source):
    (source.root / "src/nested").mkdir(parents=True)
    (source.root / "src/app.py").write_bytes(b"app\n")
    (source.root / "src/nested/logo.png").write_bytes(PNG)


def pending(store):
    row = store.query("SELECT * FROM operations WHERE state IN ('prepared','committing')")[0]
    return row, json.loads(row["metadata"])


def test_file_move_preserves_inode_bytes_mode_time_and_replays(parts):
    source, store, c = parts
    path = source.root / "a.py"
    path.chmod(0o640)
    before = path.stat()
    task = begin(parts, paths=["a.py", "renamed.py"])
    expected = source.read("a.py").sha256
    result = c.move_path("a", task, "move_0001", "a.py", "renamed.py", expected)
    moved = source.root / "renamed.py"
    after = moved.stat()
    assert not path.exists() and moved.read_bytes() == b"first\r\n"
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert stat.S_IMODE(after.st_mode) == 0o640 and before.st_mtime_ns == after.st_mtime_ns
    assert c.move_path("a", task, "move_0001", "a.py", "renamed.py", expected) == result
    assert store.query("SELECT count(*) AS n FROM operations")[0]["n"] == 1
    assert c.get_diff("a", task_id=task)["changes"] == [
        {
            "path": "renamed.py",
            "op": "move",
            "kind": "file",
            "from_path": "a.py",
            "to_path": "renamed.py",
        }
    ]
    c.finish_write_task("a", task, "finish_0001")


def test_edit_move_edit_uses_original_body_and_labels(parts):
    _, store, c = parts
    task = begin(parts, paths=["a.py", "b.py", "final.py"])
    edit(parts, task, "edit_0001", "a.py", "first", "middle")
    c.move_path("a", task, "move_0001", "a.py", "b.py")
    edit(parts, task, "edit_0002", "b.py", "middle", "last")
    c.move_path("a", task, "move_0002", "b.py", "final.py")
    diff = c.get_diff("a", task_id=task, detail="patch")["changes"][0]
    assert diff["from_path"] == "a.py" and diff["to_path"] == "final.py"
    assert "--- a/a.py" in diff["patch"] and "+++ b/final.py" in diff["patch"]
    assert "-first\r\n" in diff["patch"] and "+last\r\n" in diff["patch"]
    row = store.query("SELECT * FROM files")[0]
    assert store.read_blob(row["origin_hash"]) == b"first\r\n"
    assert c.get_diff("a", task_id=task, path="a.py")["changes"][0]["path"] == "final.py"


def test_move_back_to_origin_has_no_false_change(parts):
    _, _, c = parts
    task = begin(parts, paths=["a.py", "b.py"])
    c.move_path("a", task, "move_0001", "a.py", "b.py")
    c.move_path("a", task, "move_0002", "b.py", "a.py")
    assert c.get_diff("a", task_id=task)["changes"] == []


def test_task_created_file_stays_added_after_move(parts):
    _, _, c = parts
    task = begin(parts, paths=["new.py", "later.py"])
    c.create_file("a", task, "create_0001", "new.py", "new\n")
    c.move_path("a", task, "move_0001", "new.py", "later.py")
    change = c.get_diff("a", task_id=task)["changes"][0]
    assert change == {"path": "later.py", "op": "add", "kind": "file"}


def test_directory_and_static_binary_move_bounded_digest_and_followup_edit(parts):
    source, store, c = parts
    tree(source)
    before = (source.root / "src/nested/logo.png").stat()
    task = begin(parts, paths=["src", "dest"])
    status = c.move_path_status("a", "src")
    assert status["kind"] == "directory" and status["entries"] == 4
    assert status["sha256"] == status["tree_digest"]
    result = c.move_path("a", task, "move_0001", "src", "dest", status["tree_digest"])
    assert result["tree_digest"] == c.move_path_status("a", "dest")["tree_digest"]
    after = (source.root / "dest/nested/logo.png").stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert (source.root / "dest/nested/logo.png").read_bytes() == PNG
    edit(parts, task, "edit_0001", "dest/app.py", "app", "changed")
    changes = c.get_diff("a", task_id=task, detail="patch")["changes"]
    binary = next(item for item in changes if item["path"].endswith("logo.png"))
    assert binary["from_path"] == "src/nested/logo.png"
    assert binary["patch_unavailable_reason"] == "BINARY_CONTENT"
    assert len(store.query("SELECT * FROM files")) == 2  # Original dirs are not additions.
    c.finish_write_task("a", task, "finish_0001")


def test_directory_move_carries_prior_deletion(parts):
    source, _, c = parts
    tree(source)
    task = begin(parts, paths=["src", "src/app.py", "dest"])
    c.delete_file("a", task, "delete_0001", "src/app.py", source.read("src/app.py").sha256)
    c.move_path("a", task, "move_0001", "src", "dest")
    changes = c.get_diff("a", task_id=task)["changes"]
    deletion = next(item for item in changes if item["op"] == "delete")
    assert deletion["path"] == "dest/app.py" and deletion["from_path"] == "src/app.py"
    assert not (source.root / "dest/app.py").exists()


def test_child_move_then_parent_move_keeps_absence_and_original_names(parts):
    source, _, c = parts
    tree(source)
    task = begin(parts, paths=["src", "src/app.py", "src/main.py", "dest"])
    c.move_path("a", task, "move_0001", "src/app.py", "src/main.py")
    c.move_path("a", task, "move_0002", "src", "dest")
    changes = c.get_diff("a", task_id=task)["changes"]
    change = next(item for item in changes if item["path"] == "dest/main.py")
    assert change["from_path"] == "src/app.py" and change["to_path"] == "dest/main.py"


@pytest.mark.parametrize(
    "old,new",
    [("", "b.py"), (".", "b.py"), ("a.py", "../b.py"), ("a.py", "a.py"), ("a.py", "a.py/nested")],
)
def test_invalid_root_and_nested_moves_have_no_intent(parts, old, new):
    _, store, c = parts
    task = begin(parts)
    with pytest.raises(WriteError, match="INVALID_WRITE_PATH|WRITE_MOVE_NESTED"):
        c.move_path("a", task, "move_0001", old, new)
    assert store.query("SELECT * FROM operations") == []


def test_requires_both_declared_roots_and_expected_hash(parts):
    _, store, c = parts
    task = begin(parts, paths=["a.py", "b.py"])
    with pytest.raises(WriteError, match="WRITE_TASK_PATH_SCOPE"):
        c.move_path("a", task, "move_0001", "a.py", "c.py")
    with pytest.raises(WriteError, match="WRITE_MOVE_HASH_CONFLICT"):
        c.move_path("a", task, "move_0002", "a.py", "b.py", "0" * 64)
    assert store.query("SELECT * FROM operations") == []


def test_no_overwrite_and_unknown_removed_target_origin(parts):
    source, store, c = parts
    target = source.root / "b.py"
    target.write_bytes(b"external\n")
    task = begin(parts)
    with pytest.raises(WriteError, match="WRITE_MOVE_TARGET_HISTORY"):
        c.move_path("a", task, "move_0001", "a.py", "b.py")
    target.unlink()
    with pytest.raises(WriteError, match="WRITE_MOVE_TARGET_HISTORY"):
        c.move_path("a", task, "move_0002", "a.py", "b.py")
    assert store.query("SELECT * FROM operations") == []


@pytest.mark.parametrize(
    "problem", ["symlink", "hardlink", "credential", "git", "unknown", "fifo", "ignore"]
)
def test_unsafe_directory_contents_refused_without_mutation(parts, problem):
    source, store, c = parts
    tree(source)
    task = begin(parts)
    original = source.root / "src"
    if problem == "symlink":
        (original / "link.py").symlink_to(source.root / "a.py")
    elif problem == "hardlink":
        os.link(original / "app.py", original / "copy.py")
    elif problem == "credential":
        (original / "token.txt").write_text("sk-proj-" + "a" * 24)
    elif problem == "git":
        (original / ".git").mkdir()
    elif problem == "unknown":
        (original / "data.bin").write_bytes(b"\x00\xffdata")
    elif problem == "fifo":
        os.mkfifo(original / "pipe")
    else:
        (source.root / ".gitignore").write_text("src/nested/\n")
    with pytest.raises(SourceError):
        c.move_path("a", task, "move_0001", "src", "dest")
    assert original.is_dir() and not (source.root / "dest").exists()
    assert store.query("SELECT * FROM operations") == []


def test_registered_nested_source_refused(parts):
    source, store, c = parts
    tree(source)
    task = begin(parts)
    source.scanner._excluded = frozenset({"src/nested"})
    with pytest.raises(WriteError, match="WRITE_MOVE_TREE_EXCLUDED"):
        c.move_path("a", task, "move_0001", "src", "dest")
    assert store.query("SELECT * FROM operations") == []


def test_replaced_destination_parent_is_not_adopted(parts):
    source, store, c = parts
    (source.root / "output").mkdir()
    task = begin(parts, paths=["a.py", "output/a.py"])
    (source.root / "output").rename(source.root / "old-output")
    (source.root / "output").mkdir()
    with pytest.raises(WriteError, match="WRITE_MOVE_PARENT_CONFLICT"):
        c.move_path("a", task, "move_0001", "a.py", "output/a.py")
    assert store.query("SELECT * FROM operations") == []


@pytest.mark.parametrize("change", ["new", "remove", "edit"])
def test_external_tree_changes_are_not_claimed_as_task_origin(parts, change):
    source, store, c = parts
    tree(source)
    task = begin(parts, paths=["src", "dest"])
    if change == "new":
        (source.root / "src/new.py").write_text("late\n")
    elif change == "remove":
        (source.root / "src/app.py").unlink()
    else:
        (source.root / "src/app.py").write_text("external\n")
    with pytest.raises(WriteError, match="WRITE_MOVE_ORIGIN_CONFLICT"):
        c.move_path("a", task, "move_0001", "src", "dest")
    assert store.query("SELECT * FROM operations") == []


def test_native_target_race_preserves_both_objects_and_requires_inspection(parts, monkeypatch):
    source, store, c = parts
    task = begin(parts, paths=["a.py", "b.py"])
    original = write_move._rename_no_replace

    def raced(parent, old, target_parent, new):
        fd = os.open(new, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=target_parent)
        os.write(fd, b"external\n")
        os.close(fd)
        return original(parent, old, target_parent, new)

    monkeypatch.setattr(write_move, "_rename_no_replace", raced)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.move_path("a", task, "move_0001", "a.py", "b.py")
    assert (source.root / "a.py").read_bytes() == b"first\r\n"
    assert (source.root / "b.py").read_bytes() == b"external\n"
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert pending(store)[0]["state"] == "committing"


def test_abort_before_rename_does_not_repeat_source_mutation(parts, monkeypatch):
    source, store, c = parts
    task = begin(parts, paths=["a.py", "b.py"])

    def refuse(*_args):
        raise OSError("test before native rename")

    monkeypatch.setattr(write_move, "_rename_no_replace", refuse)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.move_path("a", task, "move_0001", "a.py", "b.py")
    assert c.recover("a")["outcomes"] == [{"state": "aborted", "source_mutation_repeated": False}]
    assert (source.root / "a.py").read_bytes() == b"first\r\n"
    assert not (source.root / "b.py").exists()
    assert store.query("SELECT * FROM object_refs") == []
    c.enable(["a"])
    with pytest.raises(WriteError, match="WRITE_OPERATION_ABORTED"):
        c.move_path("a", task, "move_0001", "a.py", "b.py")


@pytest.mark.parametrize("case_only,fail_after", [(False, 1), (True, 1), (True, 2)])
def test_crash_after_each_native_stage_is_provably_recovered(
    parts, monkeypatch, case_only, fail_after
):
    source, store, c = parts
    target = "A.py" if case_only else "b.py"
    task = begin(parts, paths=["a.py", target])
    original = write_move._rename_no_replace
    calls = 0

    def crashed(*args):
        nonlocal calls
        calls += 1
        original(*args)
        if calls == fail_after:
            raise OSError("test after native rename")

    monkeypatch.setattr(write_move, "_rename_no_replace", crashed)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.move_path("a", task, "move_0001", "a.py", target)
    _, metadata = pending(store)
    assert metadata["phase"] == (
        "moving_to_temp" if case_only and fail_after == 1 else "moving_to_target"
    )
    monkeypatch.setattr(write_move, "_rename_no_replace", original)
    result = c.recover("a")
    assert result["outcomes"] == [{"state": "confirmed_move", "source_mutation_repeated": False}]
    assert (source.root / target).read_bytes() == b"first\r\n"
    assert "a.py" not in os.listdir(source.root)
    assert not any(name.startswith(".colink-write-") for name in os.listdir(source.root))
    assert not c.status()["write_enabled"]
    assert c.get_diff("a", task_id=task)["changes"][0]["from_path"] == "a.py"


def test_case_only_journaled_intermediate_is_protected_on_content_conflict(parts, monkeypatch):
    source, store, c = parts
    task = begin(parts, paths=["a.py", "A.py"])
    original = write_move._rename_no_replace

    def crashed(*args):
        original(*args)
        raise OSError("test intermediate")

    monkeypatch.setattr(write_move, "_rename_no_replace", crashed)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.move_path("a", task, "move_0001", "a.py", "A.py")
    _, metadata = pending(store)
    intermediate = source.root / metadata["temp_name"]
    intermediate.write_bytes(b"changed externally\n")
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert intermediate.read_bytes() == b"changed externally\n"
    assert not (source.root / "A.py").exists()


def test_renamed_target_tampering_is_preserved(parts, monkeypatch):
    source, _, c = parts
    task = begin(parts, paths=["a.py", "b.py"])
    original = c.movement._complete
    monkeypatch.setattr(
        c.movement,
        "_complete",
        lambda *_args: (_ for _ in ()).throw(OSError("bookkeeping interruption")),
    )
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.move_path("a", task, "move_0001", "a.py", "b.py")
    monkeypatch.setattr(c.movement, "_complete", original)
    (source.root / "b.py").write_bytes(b"external\n")
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert (source.root / "b.py").read_bytes() == b"external\n"


def test_lost_grant_after_case_first_step_requires_local_recovery(parts, monkeypatch):
    source, _, c = parts
    task = begin(parts, paths=["a.py", "A.py"])
    original = write_move._rename_no_replace

    def disabled(*args):
        original(*args)
        c.stop_requested.set()

    monkeypatch.setattr(write_move, "_rename_no_replace", disabled)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.move_path("a", task, "move_0001", "a.py", "A.py")
    assert "a.py" not in os.listdir(source.root) and "A.py" not in os.listdir(source.root)
    monkeypatch.setattr(write_move, "_rename_no_replace", original)
    assert c.recover("a")["outcomes"][0]["state"] == "confirmed_move"
    assert not c.status()["write_enabled"]


def test_binary_file_hash_is_readable_and_unknown_format_refused(parts):
    source, store, c = parts
    (source.root / "logo.png").write_bytes(PNG)
    task = begin(parts, paths=["logo.png", "other.png", "other.bin"])
    sha = hashlib.sha256(PNG).hexdigest()
    assert c.move_path_status("a", "logo.png")["sha256"] == sha
    with pytest.raises(WriteError, match="WRITE_MOVE_UNKNOWN_BINARY"):
        c.move_path("a", task, "move_0001", "logo.png", "other.bin", sha)
    assert store.query("SELECT * FROM operations") == []
    c.move_path("a", task, "move_0002", "logo.png", "other.png", sha)
    assert (
        c.get_diff("a", task_id=task, detail="patch")["changes"][0]["patch_unavailable_reason"]
        == "BINARY_CONTENT"
    )


def test_move_tree_entry_and_byte_budgets_are_enforced(parts, monkeypatch):
    source, store, c = parts
    tree(source)
    task = begin(parts)
    monkeypatch.setattr(write_move, "MAX_MOVE_ENTRIES", 3)
    with pytest.raises(WriteError, match="WRITE_MOVE_ENTRY_LIMIT"):
        c.move_path("a", task, "move_0001", "src", "dest")
    monkeypatch.setattr(write_move, "MAX_MOVE_ENTRIES", 256)
    monkeypatch.setattr(write_move, "MAX_MOVE_BYTES", 1)
    with pytest.raises(WriteError, match="WRITE_MOVE_BYTE_LIMIT"):
        c.move_path("a", task, "move_0002", "src", "dest")
    assert store.query("SELECT * FROM operations") == []


def test_schema_one_extension_keeps_existing_task_and_objects(parts):
    source, store, c = parts
    task = begin(parts)
    edit(parts, task, "edit_0001", "a.py", "first", "saved")
    origin = store.query("SELECT origin_hash FROM files")[0]["origin_hash"]
    c.close()
    # Simulate the original schema-1 DB layout, without clearing any user data.
    with store.transaction() as db:
        db.execute("DROP TABLE move_mappings")
        db.execute("DROP TABLE move_absences")
        db.execute("DROP TABLE move_baselines")
    store.close()
    with RecoveryStore(store.root) as reopened:
        assert (
            reopened.query("SELECT value FROM settings WHERE key='schema_version'")[0]["value"]
            == "1"
        )
        assert reopened.query("SELECT task_id FROM tasks")[0]["task_id"] == task
        assert reopened.read_blob(origin) == b"first\r\n"
        restored = WriteCoordinator(reopened, lambda _project: source, control_alive=lambda: True)
        try:
            assert restored.get_diff("a", task_id=task)["changes"][0]["op"] == "modify"
            restored.enable(["a"])
            restored.finish_write_task("a", task, "finish_0001")
        finally:
            restored.close()


def test_directory_case_only_crash_finishes_registered_second_step(parts, monkeypatch):
    source, _, c = parts
    tree(source)
    task = begin(parts, paths=["src", "SRC"])
    original = write_move._rename_no_replace

    def interrupted(*args):
        original(*args)
        raise OSError("case-only directory interruption")

    monkeypatch.setattr(write_move, "_rename_no_replace", interrupted)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.move_path("a", task, "move_0001", "src", "SRC")
    monkeypatch.setattr(write_move, "_rename_no_replace", original)
    assert c.recover("a")["outcomes"][0]["state"] == "confirmed_move"
    assert (source.root / "SRC/nested/logo.png").read_bytes() == PNG
    assert "src" not in os.listdir(source.root)
    c.get_diff("a", task_id=task)


def test_created_directory_move_keeps_created_parent_bindings(parts):
    source, _, c = parts
    task = begin(parts, paths=["new", "new/file.py", "later", "later/extra.py"])
    c.create_directory("a", task, "create_0001", "new")
    c.create_file("a", task, "create_0002", "new/file.py", "new\n")
    c.move_path("a", task, "move_0001", "new", "later")
    c.create_file("a", task, "create_0003", "later/extra.py", "extra\n")
    assert (source.root / "later/file.py").read_bytes() == b"new\n"
    changes = c.get_diff("a", task_id=task)["changes"]
    assert {item["op"] for item in changes} == {"add"}
    c.finish_write_task("a", task, "finish_0001")


@pytest.mark.parametrize("corruption", ["origin", "previous", "missing_backup"])
def test_installed_move_requires_original_metadata_and_complete_backup(
    parts, monkeypatch, corruption
):
    source, store, c = parts
    task = begin(parts, paths=["a.py", "b.py"])
    original = c.movement._complete
    monkeypatch.setattr(
        c.movement, "_complete", lambda *_args: (_ for _ in ()).throw(OSError("interruption"))
    )
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.move_path("a", task, "move_0001", "a.py", "b.py")
    monkeypatch.setattr(c.movement, "_complete", original)
    row, metadata = pending(store)
    with store.transaction() as db:
        if corruption == "origin":
            metadata["participants"][0]["origin_path"] = "unrelated.py"
        elif corruption == "previous":
            metadata["participants"][0]["first_touch"] = False
        else:
            db.execute("DELETE FROM object_refs WHERE owner=?", (f"{task}:op:move_0001:before:",))
        db.execute(
            "UPDATE operations SET metadata=? WHERE task_id=? AND request_id=?",
            (json.dumps(metadata), task, row["request_id"]),
        )
    with pytest.raises(WriteError, match="WRITE_RECOVERY_CONFLICT"):
        c.recover("a")
    assert (source.root / "b.py").read_bytes() == b"first\r\n"
    assert store.query("SELECT * FROM files") == []


def test_followup_edit_recovery_accepts_authorized_moved_child(parts, monkeypatch):
    source, _, c = parts
    tree(source)
    task = begin(parts, paths=["src", "dest"])
    c.move_path("a", task, "move_0001", "src", "dest")
    from code_context import write_operations

    original = write_operations.commit_file

    def interrupted(*_args, **_kwargs):
        raise OSError("followup edit interruption")

    monkeypatch.setattr(write_operations, "commit_file", interrupted)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        edit(parts, task, "edit_0001", "dest/app.py", "app", "changed")
    monkeypatch.setattr(write_operations, "commit_file", original)
    assert c.recover("a")["outcomes"][0]["state"] == "aborted"
    assert (source.root / "dest/app.py").read_bytes() == b"app\n"
    c.get_diff("a", task_id=task)


def test_returned_source_name_is_not_silently_accepted(parts):
    source, _, c = parts
    task = begin(parts, paths=["a.py", "b.py"])
    c.move_path("a", task, "move_0001", "a.py", "b.py")
    (source.root / "a.py").write_bytes(b"external replacement\n")
    with pytest.raises(WriteError, match="WRITE_DIFF_CONFLICT"):
        c.get_diff("a", task_id=task)


def test_cross_parent_real_move_keeps_content_and_parent_identities(parts):
    source, store, c = parts
    (source.root / "output").mkdir()
    task = begin(parts, paths=["a.py", "output/a.py"])
    c.move_path("a", task, "move_0001", "a.py", "output/a.py")
    metadata = json.loads(store.query("SELECT metadata FROM operations")[0]["metadata"])
    assert metadata["source_parent"] != metadata["target_parent"]
    assert (source.root / "output/a.py").read_bytes() == b"first\r\n"
    c.get_diff("a", task_id=task)

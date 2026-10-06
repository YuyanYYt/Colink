"""Small synthetic, local-only rollback evidence; no Runtime/MCP wiring claims."""

import hashlib
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import code_context.directory_mutation as directories
import code_context.file_removal as removal
import code_context.write_rollback as rollback_module
from code_context.recovery_store import RecoveryError, RecoveryStore
from code_context.source_access import SourceAccess
from code_context.write_coordinator import RETENTION_SECONDS, WriteCoordinator, WriteError
from code_context.write_rollback import RollbackConflict, WriteRollback


@pytest.fixture
def h(tmp_path):
    root, other = tmp_path / "a", tmp_path / "b"
    root.mkdir()
    other.mkdir()
    for name in ("a", "b", "c", "untouched"):
        (root / f"{name}.py").write_bytes(f"{name} original\r\nsecond\r\n".encode())
    (root / "a.py").chmod(0o744)
    (other / "private.py").write_text("other project must not be read\n")
    state = SimpleNamespace(root=root, data=tmp_path / "recovery", alive=True, now=1000.0)
    state.sources = {"a": SourceAccess(root), "b": SourceAccess(other)}
    state.source = state.sources["a"]
    state.store = RecoveryStore(state.data)
    state.c = coordinator(state)
    state.rb = WriteRollback(state.c)
    yield state
    state.c.close()
    state.store.close()


def coordinator(h):
    return WriteCoordinator(
        h.store, h.sources.__getitem__, control_alive=lambda: h.alive, clock=lambda: h.now
    )


def begin(h, *, paths=None):
    h.c.enable(["a"])
    return h.c.begin_write_task("a", h.c.status()["next_task_request_id"], paths=paths)["task_id"]


def edit(h, task, path="a.py", old="original", new="changed", request="edit_0001"):
    return h.c.apply_edit(
        "a",
        task,
        request,
        path,
        h.source.read(path).sha256,
        {"kind": "replace_fragment", "old_text": old, "new_text": new},
    )


def records(h, task):
    current = h.store.query("SELECT * FROM tasks WHERE task_id=?", (task,))[0]
    operation = h.store.query(
        "SELECT * FROM operations WHERE task_id=? AND request_id='undo_0001'", (task,)
    )[0]
    return current, operation


def resume(h, task):
    return h.rb.resume(h.source, *records(h, task))


def restart(h):
    h.c.close()
    h.store.close()
    h.store = RecoveryStore(h.data)
    h.sources["a"] = h.source = SourceAccess(h.root)
    h.c = coordinator(h)
    h.rb = WriteRollback(h.c)


def assert_pending(h, task):
    current, operation = records(h, task)
    assert current["state"] == "rolling_back"
    assert operation["state"] == "rollback_prepared"
    assert h.c.status()["recovery_required"] and not h.c.status()["write_enabled"]
    assert h.c.inflight_project is None


def fail():
    raise RuntimeError("synthetic interruption; never echo this exception")


def test_multiround_restores_origins_not_intermediate_and_records_real_versions(h):
    original = {p.name: p.read_bytes() for p in h.root.iterdir()}
    untouched_version = h.source.read("untouched.py").version
    task = begin(h)
    edit(h, task)
    edit(h, task, "b.py", request="edit_0002")
    edit(h, task, "c.py", request="edit_0003")
    edit(h, task, old="changed", new="third", request="edit_0004")
    h.c.create_file("a", task, "create_001", "new.py", "created\n")
    result = h.rb.rollback("a", task, "undo_0001")
    assert result["state"] == "rolled_back" and result["readback_verified"]
    assert result["files_restored"] == 3 and result["files_removed"] == 1
    assert result["attribute_support"] == "verified_necessary_attributes"
    assert {p.name: p.read_bytes() for p in h.root.iterdir()} == original
    assert (h.root / "a.py").stat().st_mode & 0o777 == 0o744
    assert h.source.read("untouched.py").version == untouched_version
    assert h.sources["b"].metrics["body_reads"] == 0
    files = h.store.query("SELECT * FROM files WHERE task_id=?", (task,))
    for row in files:
        if row["kind"] == "modified":
            current = h.source.read(row["path"])
            assert row["last_hash"] == row["origin_hash"] == current.sha256
            assert tuple(json.loads(row["last_version"])) == current.version
        else:
            assert row["last_hash"] is None and row["last_version"] is None
    items = h.store.query("SELECT * FROM rollback_items WHERE task_id=?", (task,))
    assert all(row["state"] == "done" for row in items)
    for row in items:
        assert "original" not in row["metadata"] and "third" not in row["metadata"]
        assert len(row["metadata"].encode()) <= 16 * 1024
        data = json.loads(row["metadata"])
        assert data["backup_owner"] == f"{task}:rollback:undo_0001:{row['path']}"
        raw = h.store.read_blob(data["latest_hash"])
        assert hashlib.sha256(raw).hexdigest() == data["latest_hash"]
    assert h.rb.rollback("a", task, "undo_0001") == result


def test_files_then_deepest_owned_empty_directories_never_existing_directory(h, monkeypatch):
    (h.root / "existing").mkdir()
    task = begin(h)
    h.c.create_directory("a", task, "mkdir_001", "new")
    h.c.create_directory("a", task, "mkdir_002", "new/deep")
    h.c.create_file("a", task, "create_001", "new/deep/leaf.py", "leaf\n")
    h.c.create_file("a", task, "create_002", "existing/leaf.py", "leaf\n")
    order = []
    remove_file, remove_dir = (
        rollback_module.remove_created_file,
        rollback_module.remove_created_directory,
    )

    def file(*args, **kwargs):
        order.append(("file", args[1].path))
        return remove_file(*args, **kwargs)

    def directory(*args, **kwargs):
        order.append(("directory", args[1]))
        return remove_dir(*args, **kwargs)

    monkeypatch.setattr(rollback_module, "remove_created_file", file)
    monkeypatch.setattr(rollback_module, "remove_created_directory", directory)
    result = h.rb.rollback("a", task, "undo_0001")
    assert result["files_removed"] == 2 and result["directories_removed"] == 2
    assert order[-2:] == [("directory", "new/deep"), ("directory", "new")]
    assert all(kind == "file" for kind, _ in order[:-2])
    assert not (h.root / "new").exists() and (h.root / "existing").is_dir()


@pytest.mark.parametrize("foreign", ["external.py", ".hidden", "node_modules", "link.py"])
def test_precheck_enumerates_real_foreign_children_not_filtered_manifest(h, foreign):
    task = begin(h)
    edit(h, task)
    h.c.create_directory("a", task, "mkdir_001", "new")
    entry = h.root / "new" / foreign
    if foreign == "node_modules":
        entry.mkdir()
    elif foreign == "link.py":
        entry.symlink_to(h.sources["b"].root / "private.py")
    else:
        entry.write_text("foreign\n")
    latest = (h.root / "a.py").read_bytes()
    with pytest.raises(RollbackConflict) as error:
        h.rb.rollback("a", task, "undo_0001")
    assert error.value.conflict_count == 1
    assert error.value.conflict_paths == ("new",)
    assert (h.root / "a.py").read_bytes() == latest and entry.lstat()
    assert not h.store.query("SELECT * FROM rollback_items")
    assert h.store.query("SELECT state FROM tasks")[0]["state"] == "active"
    assert h.sources["b"].metrics["body_reads"] == 0
    assert str(h.root) not in str(error.value) and foreign not in str(error.value)


def test_all_known_conflicts_are_counted_with_zero_target_changes(h):
    task = begin(h)
    edit(h, task)
    edit(h, task, "b.py", request="edit_0002")
    h.c.create_directory("a", task, "mkdir_001", "new")
    (h.root / "a.py").write_text("foreign a\n")
    (h.root / "b.py").write_text("foreign b\n")
    (h.root / "new" / ".hidden").write_text("foreign child\n")
    with pytest.raises(RollbackConflict) as error:
        h.rb.rollback("a", task, "undo_0001")
    assert error.value.conflict_count == 3
    assert error.value.conflict_paths == ("a.py", "b.py", "new")
    assert (h.root / "a.py").read_text() == "foreign a\n"
    assert (h.root / "b.py").read_text() == "foreign b\n"
    assert not h.store.query("SELECT * FROM rollback_items")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "replacement", "ignore", "parent"])
def test_participant_changes_reject_before_any_restore(h, kind):
    (h.root / "existing").mkdir()
    (h.root / "existing" / "d.py").write_text("original\n")
    task = begin(h)
    edit(h, task)
    edit(h, task, "existing/d.py", request="edit_0002")
    path = h.root / "existing" / "d.py"
    if kind == "symlink":
        path.rename(h.root / "held.py")
        path.symlink_to(h.sources["b"].root / "private.py")
    elif kind == "hardlink":
        os.link(path, h.root / "alias.py")
    elif kind == "replacement":
        path.rename(h.root / "held.py")
        path.write_text("changed\n")
    elif kind == "ignore":
        (h.root / ".codecontextignore").write_text("existing/d.py\n")
    else:
        (h.root / "existing").rename(h.root / "held")
        (h.root / "existing").mkdir()
        path.write_text("changed\n")
    latest = (h.root / "a.py").read_bytes()
    with pytest.raises(RollbackConflict):
        h.rb.rollback("a", task, "undo_0001")
    assert (h.root / "a.py").read_bytes() == latest
    assert not h.store.query("SELECT * FROM rollback_items")


def test_latest_completed_and_seven_day_ttl_replay(h):
    task = begin(h)
    edit(h, task)
    h.c.finish_write_task("a", task, "finish_001")
    h.now += RETENTION_SECONDS
    result = h.rb.rollback("a", task, "undo_0001")
    assert result["state"] == "rolled_back"
    h.now += RETENTION_SECONDS + 1
    with pytest.raises(WriteError, match="WRITE_TASK_EXPIRED"):
        h.rb.rollback("a", task, "undo_0001")


def test_later_active_task_blocks_previous_completed(h):
    first = begin(h)
    edit(h, first)
    h.c.finish_write_task("a", first, "finish_001")
    second = begin(h)
    with pytest.raises(WriteError, match="TASK_ORDER"):
        h.rb.rollback("a", first, "undo_0001")
    assert not h.store.query("SELECT * FROM rollback_items")
    assert h.rb.rollback("a", second, "undo_0002")["files_restored"] == 0
    assert not h.store.query("SELECT * FROM tasks WHERE task_id=?", (first,))
    with pytest.raises(WriteError, match="WRITE_TASK_EXPIRED"):
        h.rb.rollback("a", first, "undo_0001")


def test_scope_auth_unknown_task_and_request_conflict(h):
    task = begin(h)
    edit(h, task)
    h.c.enable(["a", "b"])
    with pytest.raises(WriteError, match="WRITE_TASK_SCOPE"):
        h.rb.rollback("b", task, "undo_0001")
    with pytest.raises(WriteError, match="WRITE_TASK_EXPIRED"):
        h.rb.rollback("a", "wt_" + "0" * 32, "undo_0001")
    with pytest.raises(WriteError, match="REQUEST_ID_CONFLICT"):
        h.rb.rollback("a", task, "edit_0001")
    h.c.disable()
    with pytest.raises(WriteError, match="WRITE_DISABLED"):
        h.rb.rollback("a", task, "undo_0001")
    assert not h.store.query("SELECT * FROM rollback_items")


def test_rollback_bypasses_ordinary_1024_cap_without_repeating_filesystem_ops(h):
    task = begin(h)
    edit(h, task)
    # SQL-seeded synthetic count only: not evidence for 1024 native mutations.
    with h.store.transaction() as db:
        db.executemany(
            "INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
            [(task, f"seed_{n:04d}", "0" * 64, "done", "{}", "{}", n + 1) for n in range(1, 1024)],
        )
    with pytest.raises(WriteError, match="WRITE_OPERATION_LIMIT"):
        h.c.create_file("a", task, "create_001", "new.py", "new\n")
    assert h.rb.rollback("a", task, "undo_0001")["state"] == "rolled_back"
    assert len(h.store.query("SELECT * FROM operations")) == 1025


def test_unified_reservation_and_all_backups_precede_first_native_call(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    edit(h, task, "b.py", request="edit_0002")
    actual_reserve, actual_commit = h.c.reserve_growth, rollback_module.commit_file
    calls = []

    def reserve_growth(task_id, **kwargs):
        assert not h.store.query("SELECT * FROM rollback_items")
        assert task_id == task and kwargs["future_body_bytes"] == 0
        assert kwargs["source"] is h.source and kwargs["source_temp_bytes"] == 4096
        calls.append(kwargs)
        return actual_reserve(task_id, **kwargs)

    def commit(*args, **kwargs):
        items = h.store.query("SELECT metadata FROM rollback_items")
        assert len(items) == 2
        for row in items:
            data = json.loads(row["metadata"])
            assert data["backup_verified"] and data["origin_verified"]
            assert h.store.read_blob(data["latest_hash"])
            assert h.store.read_blob(data["origin_hash"])
            assert h.store.query("SELECT * FROM object_refs WHERE owner=?", (data["backup_owner"],))
        return actual_commit(*args, **kwargs)

    monkeypatch.setattr(h.c, "reserve_growth", reserve_growth)
    monkeypatch.setattr(rollback_module, "commit_file", commit)
    h.rb.rollback("a", task, "undo_0001")
    assert len(calls) == 1 and calls[0]["metadata_bytes"] >= 65536


def test_reservation_failure_does_not_record_intent_or_modify_source(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    latest = (h.root / "a.py").read_bytes()

    def refuse(*args, **kwargs):
        raise RecoveryError("RECOVERY_LIMIT: synthetic capacity")

    monkeypatch.setattr(h.c, "reserve_growth", refuse)
    with pytest.raises(RecoveryError, match="RECOVERY_LIMIT"):
        h.rb.rollback("a", task, "undo_0001")
    assert (h.root / "a.py").read_bytes() == latest
    assert not h.store.query("SELECT * FROM rollback_items")
    assert h.store.query("SELECT state FROM tasks")[0]["state"] == "active"


def test_backup_failure_has_zero_target_changes_and_resumes_after_reopen(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    edit(h, task, "b.py", request="edit_0002")
    latest = {p.name: p.read_bytes() for p in h.root.iterdir()}
    actual = h.store.put_blob
    count = 0

    def backup(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            fail()
        return actual(*args, **kwargs)

    monkeypatch.setattr(h.store, "put_blob", backup)
    with pytest.raises(WriteError, match="ROLLBACK_RECOVERY_REQUIRED"):
        h.rb.rollback("a", task, "undo_0001")
    assert {p.name: p.read_bytes() for p in h.root.iterdir()} == latest
    assert_pending(h, task)
    restart(h)
    assert resume(h, task)["files_restored"] == 2
    assert not h.c.status()["write_enabled"] and not h.c.status()["recovery_required"]


@pytest.mark.parametrize("phase", ["prepare", "commit", "discard", "done_log"])
def test_modified_file_interruption_restart_no_duplicate_swap(h, monkeypatch, phase):
    task = begin(h)
    edit(h, task)
    if phase == "done_log":
        actual = h.rb._save_item

        def save(task, item, state):
            if state == "done":
                fail()
            return actual(task, item, state)

        monkeypatch.setattr(h.rb, "_save_item", save)
    else:
        name = {"prepare": "prepare_file", "commit": "commit_file", "discard": "discard_prepared"}[
            phase
        ]
        actual = getattr(rollback_module, name)

        def interrupted(*args, **kwargs):
            actual(*args, **kwargs)
            fail()

        monkeypatch.setattr(rollback_module, name, interrupted)
    with pytest.raises(WriteError, match="ROLLBACK_RECOVERY_REQUIRED"):
        h.rb.rollback("a", task, "undo_0001")
    assert_pending(h, task)
    inode = (h.root / "a.py").stat().st_ino
    monkeypatch.undo()
    restart(h)
    result = resume(h, task)
    assert result["files_restored"] == 1
    if phase != "prepare":
        assert (h.root / "a.py").stat().st_ino == inode
    assert (h.root / "a.py").read_bytes() == b"a original\r\nsecond\r\n"
    assert not list(h.root.glob(".colink-write-*"))
    assert resume(h, task) == result and not h.c.status()["write_enabled"]


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("phase", ["native_move", "isolation_log", "native_remove", "done_log"])
def test_created_removal_windows_survive_reopen(h, monkeypatch, kind, phase):
    task = begin(h)
    if kind == "file":
        h.c.create_file("a", task, "create_001", "new.py", "created\n")
        target = h.root / "new.py"
        helper = removal
    else:
        h.c.create_directory("a", task, "mkdir_001", "new")
        target = h.root / "new"
        helper = directories
    if phase == "native_move":
        actual = helper._rename_excl

        def moved(*args, **kwargs):
            actual(*args, **kwargs)
            fail()

        monkeypatch.setattr(helper, "_rename_excl", moved)
    elif phase == "native_remove":
        name = "remove_created_file" if kind == "file" else "remove_created_directory"
        actual = getattr(rollback_module, name)

        def removed(*args, **kwargs):
            actual(*args, **kwargs)
            fail()

        monkeypatch.setattr(rollback_module, name, removed)
    else:
        actual = h.rb._save_item

        def save(task, item, state):
            actual(task, item, state)
            if state == ("isolated" if phase == "isolation_log" else "done"):
                fail()

        monkeypatch.setattr(h.rb, "_save_item", save)
    with pytest.raises(WriteError, match="ROLLBACK_RECOVERY_REQUIRED"):
        h.rb.rollback("a", task, "undo_0001")
    assert not target.exists()
    assert_pending(h, task)
    monkeypatch.undo()
    restart(h)
    assert resume(h, task)["state"] == "rolled_back"
    assert not target.exists() and not list(h.root.glob(".colink-write-*"))


def prepared_interrupt(h, monkeypatch, *, after_swap=False):
    task = begin(h)
    edit(h, task)
    name = "commit_file" if after_swap else "prepare_file"
    actual = getattr(rollback_module, name)

    def interrupted(*args, **kwargs):
        actual(*args, **kwargs)
        fail()

    monkeypatch.setattr(rollback_module, name, interrupted)
    with pytest.raises(WriteError, match="ROLLBACK_RECOVERY_REQUIRED"):
        h.rb.rollback("a", task, "undo_0001")
    monkeypatch.undo()
    return task


@pytest.mark.parametrize("kind", ["body", "mode", "symlink", "missing", "receipt", "target"])
def test_unknown_or_changed_preparation_never_recreated_or_overwritten(h, monkeypatch, kind):
    task = prepared_interrupt(h, monkeypatch)
    temp = next(h.root.glob(".colink-write-*"))
    if kind == "body":
        temp.write_text("unknown preparation\n")
    elif kind == "mode":
        temp.chmod(0o666)
    elif kind == "symlink":
        temp.rename(h.root / "held_temp.py")
        temp.symlink_to(h.sources["b"].root / "private.py")
    elif kind == "missing":
        temp.rename(h.root / "held_temp.py")
    elif kind == "receipt":
        row = h.store.query("SELECT * FROM rollback_items")[0]
        data = json.loads(row["metadata"])
        data["prepared"]["path"] = "b.py"
        with h.store.transaction() as db:
            db.execute("UPDATE rollback_items SET metadata=?", (json.dumps(data),))
    else:
        (h.root / "a.py").write_text("external target\n")
    before = (h.root / "a.py").read_bytes()
    inode = temp.lstat().st_ino if temp.exists() or temp.is_symlink() else None
    restart(h)
    with pytest.raises(WriteError, match="ROLLBACK_RECOVERY_REQUIRED") as error:
        resume(h, task)
    assert (h.root / "a.py").read_bytes() == before
    if inode is not None:
        assert temp.lstat().st_ino == inode
    else:
        assert not temp.exists()
    assert_pending(h, task)
    assert str(h.root) not in str(error.value) and "external target" not in str(error.value)
    assert h.sources["b"].metrics["body_reads"] == 0


def test_registered_partial_preparation_preserved_not_rewritten(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    actual = rollback_module.prepare_file

    def partial(source, path, raw, mode, name, callback, attributes):
        def created(receipt):
            callback(receipt)
            fail()  # Saved intended receipt, before the helper writes body/mode.

        return actual(source, path, raw, mode, name, created, attributes)

    monkeypatch.setattr(rollback_module, "prepare_file", partial)
    with pytest.raises(WriteError):
        h.rb.rollback("a", task, "undo_0001")
    temp = next(h.root.glob(".colink-write-*"))
    assert temp.read_bytes() == b""
    inode = temp.stat().st_ino
    monkeypatch.undo()
    restart(h)
    with pytest.raises(WriteError):
        resume(h, task)
    assert temp.read_bytes() == b"" and temp.stat().st_ino == inode
    assert b"changed" in (h.root / "a.py").read_bytes()
    assert_pending(h, task)


@pytest.mark.parametrize("kind", ["captured_change", "capture_gone", "target_change"])
def test_unconfirmed_swap_preserves_external_material(h, monkeypatch, kind):
    task = prepared_interrupt(h, monkeypatch, after_swap=True)
    temp = next(h.root.glob(".colink-write-*"))
    if kind == "captured_change":
        temp.write_text("foreign captured\n")
    elif kind == "capture_gone":
        temp.rename(h.root / "held_latest.py")
    else:
        (h.root / "a.py").write_text("external restored\n")
    before = (h.root / "a.py").read_bytes()
    restart(h)
    with pytest.raises(WriteError):
        resume(h, task)
    assert (h.root / "a.py").read_bytes() == before
    if kind != "capture_gone":
        assert temp.exists()
    assert_pending(h, task)


@pytest.mark.parametrize("kind", ["ignore", "provider", "root", "connection"])
def test_resume_rechecks_scope_policy_original_root_and_local_connection(h, monkeypatch, kind):
    task = prepared_interrupt(h, monkeypatch)
    if kind == "ignore":
        (h.root / ".codecontextignore").write_text("b.py\n")
    elif kind == "provider":
        h.sources["a"] = h.sources["b"]
    elif kind == "root":
        h.root.rename(h.root.with_name("held_source"))
        h.root.mkdir()
        (h.root / "a.py").write_text("unknown root\n")
    else:
        h.alive = False
    before = (h.root / "a.py").read_bytes()
    with pytest.raises(WriteError):
        resume(h, task)
    assert (h.root / "a.py").read_bytes() == before
    assert_pending(h, task)


def test_completed_item_conflict_preflight_blocks_all_remaining_restores(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    edit(h, task, "b.py", request="edit_0002")
    edit(h, task, "c.py", request="edit_0003")
    actual = h.rb._save_item

    def interrupted(task, item, state):
        actual(task, item, state)
        if state == "done" and item["path"] == "a.py":
            fail()

    monkeypatch.setattr(h.rb, "_save_item", interrupted)
    with pytest.raises(WriteError):
        h.rb.rollback("a", task, "undo_0001")
    (h.root / "a.py").write_text("external after restore\n")
    latest_b, latest_c = (h.root / "b.py").read_bytes(), (h.root / "c.py").read_bytes()
    monkeypatch.undo()
    restart(h)
    with pytest.raises(WriteError):
        resume(h, task)
    assert (h.root / "b.py").read_bytes() == latest_b
    assert (h.root / "c.py").read_bytes() == latest_c
    assert_pending(h, task)


def test_corrupt_origin_is_refused_before_intent_and_first_source_change(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    edit(h, task, "b.py", request="edit_0002")
    sha = h.store.query("SELECT origin_hash FROM files WHERE path='b.py'")[0]["origin_hash"]
    actual = h.store.read_blob

    def corrupt(value, **kwargs):
        if value == sha:
            raise RecoveryError("RECOVERY_OBJECT_CHANGED: synthetic corruption")
        return actual(value, **kwargs)

    monkeypatch.setattr(h.store, "read_blob", corrupt)
    with pytest.raises(RollbackConflict) as error:
        h.rb.rollback("a", task, "undo_0001")
    assert error.value.conflict_count == 1
    assert error.value.conflict_paths == ("b.py",)
    assert b"changed" in (h.root / "a.py").read_bytes()
    assert not h.store.query("SELECT * FROM rollback_items")


def test_final_sql_failure_preserves_item_done_and_resumes_without_native_calls(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    transaction = h.store.transaction

    @contextmanager
    def fail_final():
        with transaction() as db:

            class Checked:
                def execute(self, sql, *args):
                    if "UPDATE tasks SET state='rolled_back'" in sql:
                        raise RecoveryError("RECOVERY_METADATA_LIMIT: synthetic final SQL failure")
                    return db.execute(sql, *args)

            yield Checked()

    monkeypatch.setattr(h.store, "transaction", fail_final)
    with pytest.raises(WriteError):
        h.rb.rollback("a", task, "undo_0001")
    assert h.store.query("SELECT state FROM rollback_items")[0]["state"] == "done"
    assert_pending(h, task)
    inode = (h.root / "a.py").stat().st_ino
    monkeypatch.undo()
    restart(h)
    monkeypatch.setattr(rollback_module, "commit_file", lambda *a, **kw: fail())
    assert resume(h, task)["state"] == "rolled_back"
    assert (h.root / "a.py").stat().st_ino == inode


def test_authority_stop_after_prepare_keeps_pending_then_local_resume(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    actual = rollback_module.prepare_file

    def stopped(*args, **kwargs):
        receipt = actual(*args, **kwargs)
        h.c.disable()
        return receipt

    monkeypatch.setattr(rollback_module, "prepare_file", stopped)
    with pytest.raises(WriteError):
        h.rb.rollback("a", task, "undo_0001")
    assert b"changed" in (h.root / "a.py").read_bytes()
    assert_pending(h, task)
    monkeypatch.undo()
    assert resume(h, task)["state"] == "rolled_back"
    assert not h.c.status()["write_enabled"]


def test_invalid_journal_layout_and_bounded_item_metadata_fail_closed(h):
    # A separate empty synthetic store, not an application database migration.
    alternate = RecoveryStore(h.data.with_name("bad_schema"))
    try:
        with alternate.transaction() as db:
            db.execute("CREATE TABLE rollback_items(unknown TEXT)")
        with pytest.raises(WriteError, match="WRITE_ROLLBACK_SCHEMA"):
            WriteRollback(SimpleNamespace(store=alternate))
    finally:
        alternate.close()
    with pytest.raises(WriteError, match="METADATA_LIMIT"):
        rollback_module._encode_item({"metadata_only": "x" * (16 * 1024)})


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_revoked_authority_after_move_preserves_isolated_object_for_local_resume(
    h, monkeypatch, kind
):
    task = begin(h)
    if kind == "file":
        h.c.create_file("a", task, "create_001", "new.py", "created\n")
        helper, path = removal, "new.py"
    else:
        h.c.create_directory("a", task, "mkdir_001", "new")
        helper, path = directories, "new"
    actual = helper._rename_excl

    def revoke(*args, **kwargs):
        actual(*args, **kwargs)
        h.c.disable()

    monkeypatch.setattr(helper, "_rename_excl", revoke)
    with pytest.raises(WriteError):
        h.rb.rollback("a", task, "undo_0001")
    assert not (h.root / path).exists()
    assert next(h.root.glob(".colink-write-*")).lstat()
    assert_pending(h, task)
    monkeypatch.undo()
    restart(h)
    assert resume(h, task)["state"] == "rolled_back"
    assert not list(h.root.glob(".colink-write-*"))


def test_late_foreign_child_never_recursively_removed(h, monkeypatch):
    task = begin(h)
    edit(h, task)
    h.c.create_directory("a", task, "mkdir_001", "new")
    actual = rollback_module.remove_created_directory

    def externally_filled(*args, **kwargs):
        (h.root / "new" / ".external").write_text("preserve\n")
        return actual(*args, **kwargs)

    monkeypatch.setattr(rollback_module, "remove_created_directory", externally_filled)
    with pytest.raises(WriteError):
        h.rb.rollback("a", task, "undo_0001")
    assert b"original" in (h.root / "a.py").read_bytes()
    assert (h.root / "new" / ".external").read_text() == "preserve\n"
    assert_pending(h, task)
    monkeypatch.undo()
    restart(h)
    with pytest.raises(WriteError):
        resume(h, task)
    assert (h.root / "new" / ".external").read_text() == "preserve\n"


def test_replaced_created_directory_conflicts_before_restore(h):
    task = begin(h)
    edit(h, task)
    h.c.create_directory("a", task, "mkdir_001", "new")
    (h.root / "new").rename(h.root / "held_directory")
    (h.root / "new").mkdir()
    latest = (h.root / "a.py").read_bytes()
    with pytest.raises(RollbackConflict):
        h.rb.rollback("a", task, "undo_0001")
    assert (h.root / "a.py").read_bytes() == latest
    assert (h.root / "new").is_dir() and (h.root / "held_directory").is_dir()
    assert not h.store.query("SELECT * FROM rollback_items")


def test_origin_bom_crlf_and_empty_content_preserved_exactly(h):
    (h.root / "bom.py").write_bytes(b"\xef\xbb\xbfline\r\n")
    (h.root / "empty.py").write_bytes(b"")
    task = begin(h)
    edit(h, task, "bom.py", "line", "changed")
    h.c.apply_edit(
        "a",
        task,
        "edit_0002",
        "empty.py",
        h.source.read("empty.py").sha256,
        {
            "kind": "insert_lines",
            "line": 1,
            "position": "before",
            "expected_context": "",
            "text": "filled\n",
        },
    )
    assert h.rb.rollback("a", task, "undo_0001")["files_restored"] == 2
    assert (h.root / "bom.py").read_bytes() == b"\xef\xbb\xbfline\r\n"
    assert (h.root / "empty.py").read_bytes() == b""


def test_expired_completed_first_request_cannot_create_rollback_intent(h):
    task = begin(h)
    edit(h, task)
    h.c.finish_write_task("a", task, "finish_001")
    h.now += RETENTION_SECONDS + 1
    with pytest.raises(WriteError, match="WRITE_TASK_EXPIRED"):
        h.rb.rollback("a", task, "undo_0001")
    assert not h.store.query("SELECT * FROM rollback_items")
    assert b"changed" in (h.root / "a.py").read_bytes()


def test_resume_mismatched_task_operation_does_not_relabel_durable_state(h, monkeypatch):
    task = prepared_interrupt(h, monkeypatch)
    current, operation = records(h, task)
    operation["task_id"] = "wt_" + "0" * 32
    with pytest.raises(WriteError, match="WRITE_ROLLBACK_SCOPE"):
        h.rb.resume(h.source, current, operation)
    assert_pending(h, task)
    assert resume(h, task)["state"] == "rolled_back"


@pytest.mark.parametrize("complete", [True, False])
def test_pending_recovery_object_complete_only_can_resume_after_durable_verification(
    h, monkeypatch, complete
):
    task = begin(h)
    edit(h, task)
    actual = h.store.put_blob
    saved = []

    def interrupted(raw, owner):
        sha = actual(raw, owner)
        saved.append(sha)
        with h.store.transaction() as db:
            db.execute("UPDATE objects SET state='pending' WHERE sha256=?", (sha,))
        if not complete:
            (h.store.objects.root / sha).write_bytes(b"")
        fail()

    monkeypatch.setattr(h.store, "put_blob", interrupted)
    with pytest.raises(WriteError):
        h.rb.rollback("a", task, "undo_0001")
    assert_pending(h, task)
    monkeypatch.undo()
    restart(h)
    if complete:
        assert resume(h, task)["state"] == "rolled_back"
        assert (
            h.store.query("SELECT state FROM objects WHERE sha256=?", (saved[0],))[0]["state"]
            == "ready"
        )
    else:
        with pytest.raises(WriteError):
            resume(h, task)
        assert b"changed" in (h.root / "a.py").read_bytes()
        assert (h.store.objects.root / saved[0]).read_bytes() == b""
        assert_pending(h, task)

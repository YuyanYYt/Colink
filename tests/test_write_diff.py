import hashlib
import threading

import pytest

import code_context.write_diff as diff
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess
from code_context.write_coordinator import WriteCoordinator, WriteError


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("one\ntwo\n")
    (root / "b.py").write_text("other\n")
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    c = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    yield source, store, c
    c.close()
    store.close()


def begin(c):
    c.enable(["a"])
    return c.begin_write_task("a", c.status()["next_task_request_id"])["task_id"]


def edit(source, c, task, request, path, old, new):
    return c.apply_edit(
        "a",
        task,
        request,
        path,
        source.read(path).sha256,
        {"kind": "replace_fragment", "old_text": old, "new_text": new},
    )


def test_no_task_is_unavailable_not_no_changes_and_empty_is_explicit(parts):
    source, store, c = parts
    result = c.get_diff("a")
    assert not result["changes_available"] and result["reason"] == "NO_TASK_BASELINE"
    empty = c.get_diff("a", baseline="empty", path="a.py", detail="patch")
    assert empty["changes_available"] and "+one" in empty["changes"][0]["patch"]
    assert empty["changes"][0]["op"] == "add"
    assert store.query("SELECT * FROM objects") == []


def test_multiple_rounds_compare_task_start_not_last_operation(parts):
    source, store, c = parts
    task = begin(c)
    edit(source, c, task, "edit_0001", "a.py", "one", "first change")
    edit(source, c, task, "edit_0002", "a.py", "first change", "final change")
    edit(source, c, task, "edit_0003", "b.py", "other", "updated")
    c.create_file("a", task, "create_001", "c.py", "new file\n")
    result = c.get_diff("a", detail="patch")
    assert result["baseline"] == "task_origin"
    assert result["summary"]["files_changed"] == 3
    by_path = {row["path"]: row for row in result["changes"]}
    assert "-one" in by_path["a.py"]["patch"] and "+final change" in by_path["a.py"]["patch"]
    assert "first change" not in by_path["a.py"]["patch"]
    assert by_path["c.py"]["op"] == "add"
    assert "+new file" in by_path["c.py"]["patch"]
    assert store.usage()["object_count"] == 2


def test_diff_remains_readable_after_disabling_writes(parts):
    source, store, c = parts
    task = begin(c)
    edit(source, c, task, "edit_0001", "a.py", "one", "updated")
    c.disable()
    assert c.get_diff("a")["summary"]["files_changed"] == 1


def test_summary_never_loads_untouched_or_origin_bodies_when_hashes_warm(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    edit(source, c, task, "edit_0001", "a.py", "one", "updated")
    monkeypatch.setattr(store, "read_blob", lambda *args: pytest.fail("no summary body"))
    reads = source.metrics["body_reads"]
    assert c.get_diff("a")["changes"][0]["path"] == "a.py"
    assert source.metrics["body_reads"] == reads


@pytest.mark.parametrize("mode", ["body", "identity", "permissions", "deleted"])
def test_external_change_is_conflict_even_if_requested_path_is_other_file(parts, mode):
    source, store, c = parts
    task = begin(c)
    edit(source, c, task, "edit_0001", "a.py", "one", "updated")
    target = source.root / "a.py"
    if mode == "body":
        target.write_text("external\n")
    elif mode == "identity":
        raw = target.read_bytes()
        target.rename(source.root / "external-original.py")
        target.write_bytes(raw)
    elif mode == "permissions":
        target.chmod(0o600)
    else:
        target.unlink()
    with pytest.raises(WriteError, match="WRITE_DIFF_CONFLICT"):
        c.get_diff("a", path="b.py")


def test_edit_returning_to_origin_reports_zero_changes_not_last_round_patch(parts):
    source, store, c = parts
    task = begin(c)
    edit(source, c, task, "edit_0001", "a.py", "one", "updated")
    edit(source, c, task, "edit_0002", "a.py", "updated", "one")
    result = c.get_diff("a", detail="patch")
    assert result["changes_available"] and result["changes"] == []
    assert result["summary"]["files_changed"] == 0


def test_paginated_changed_files_and_patch_response_budget(parts):
    source, store, c = parts
    task = begin(c)
    for index in range(5):
        c.create_file("a", task, f"create_{index:03d}", f"new_{index}.py", "x" * 2000)
    first = c.get_diff("a", limit=2)
    assert len(first["changes"]) == 2 and first["next_offset"] == 2
    second = c.get_diff("a", offset=2, limit=2)
    assert second["changes"][0]["path"] == "new_2.py"
    patch = c.get_diff("a", detail="patch", path="new_0.py", max_chars=1000)
    assert patch["truncated"] and len(patch["changes"][0]["patch"]) < 1000


def test_line_product_budget_avoids_potentially_quadratic_algorithm(monkeypatch):
    monkeypatch.setattr(
        diff.difflib, "unified_diff", lambda *args, **kwargs: pytest.fail("algorithm must not run")
    )
    patch, truncated, reason = diff.bounded_patch("a.py", "x\n" * 3000, "y\n" * 3000, 50000)
    assert patch == "" and truncated and reason == "DIFF_COMPUTATION_LIMIT"


@pytest.mark.parametrize(
    "parameters",
    [
        {"offset": -1},
        {"offset": False},
        {"limit": 101},
        {"max_chars": 999},
        {"baseline": "random"},
        {"detail": "random"},
        {"path": "../outside.py"},
    ],
)
def test_invalid_diff_parameters(parts, parameters):
    with pytest.raises(WriteError):
        parts[2].get_diff("a", **parameters)


def test_completed_task_retains_diff_and_finish_idempotent(parts):
    source, store, c = parts
    task = begin(c)
    edit(source, c, task, "edit_0001", "a.py", "one", "updated")
    result = c.finish_write_task("a", task, "finish_001")
    assert c.finish_write_task("a", task, "finish_001") == result
    assert c.get_diff("a")["summary"]["files_changed"] == 1
    with pytest.raises(WriteError, match="WRITE_TASK_NOT_ACTIVE"):
        edit(source, c, task, "edit_0002", "a.py", "updated", "late")


def test_new_completed_task_retires_old_restoration_point_and_request_ids(parts):
    source, store, c = parts
    first = begin(c)
    edit(source, c, first, "edit_0001", "a.py", "one", "first updated")
    c.finish_write_task("a", first, "finish_001")
    original_sha = hashlib.sha256(b"one\ntwo\n").hexdigest()
    second = begin(c)
    assert len(store.query("SELECT * FROM tasks")) == 2
    edit(source, c, second, "edit_0001", "b.py", "other", "second updated")
    c.finish_write_task("a", second, "finish_001")
    assert len(store.query("SELECT * FROM tasks")) == 1
    assert not store.query("SELECT * FROM objects WHERE sha256=?", (original_sha,))
    with pytest.raises(WriteError, match="WRITE_TASK_EXPIRED"):
        c.finish_write_task("a", first, "finish_001")


def test_finish_possible_at_operation_cap_and_conflicts_do_not_finish(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    monkeypatch.setattr("code_context.write_operations.MAX_OPERATIONS", 1)
    edit(source, c, task, "edit_0001", "a.py", "one", "updated")
    with pytest.raises(WriteError, match="WRITE_OPERATION_LIMIT"):
        edit(source, c, task, "edit_0002", "a.py", "updated", "again")
    (source.root / "a.py").write_text("external\n")
    with pytest.raises(WriteError, match="WRITE_DIFF_CONFLICT"):
        c.finish_write_task("a", task, "finish_001")
    assert store.query("SELECT * FROM tasks")[0]["state"] == "active"


def test_finish_at_cap_without_conflict(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    monkeypatch.setattr("code_context.write_operations.MAX_OPERATIONS", 1)
    edit(source, c, task, "edit_0001", "a.py", "one", "updated")
    assert c.finish_write_task("a", task, "finish_001")["state"] == "completed"


def test_expired_retained_task_returns_no_baseline(parts):
    source, store, c = parts
    task = begin(c)
    c.finish_write_task("a", task, "finish_001")
    c.clock = lambda: 1e20
    assert c.get_diff("a")["reason"] == "NO_TASK_BASELINE"


def test_read_guard_avoids_source_coordinator_lock_inversion(parts):
    source, store, c = parts
    started, done = threading.Event(), threading.Event()

    def guard():
        started.set()
        c.guard_read("other")
        done.set()

    with c.lock:
        worker = threading.Thread(target=guard, daemon=True)
        worker.start()
        assert started.wait(1) and done.wait(1)
    worker.join(1)
    assert not worker.is_alive()


def test_restart_retention_finishes_interrupted_old_point_retirement(parts):
    source, store, c = parts
    first = begin(c)
    c.finish_write_task("a", first, "finish_001")
    second = begin(c)
    # A durable completion followed by a process stop before old-point retirement.
    with store.transaction() as db:
        db.execute(
            "UPDATE tasks SET state='completed',completed=? WHERE task_id=?",
            (c.clock(), second),
        )
    assert len(store.query("SELECT * FROM tasks")) == 2
    c.close()
    replacement = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    replacement.enable(["a"])
    assert [row["task_id"] for row in store.query("SELECT * FROM tasks")] == [second]
    replacement.close()

"""Local-only task/origin candidates; no source-writing tools are attached."""

import json

import pytest

from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator, WriteError


@pytest.fixture
def parts(tmp_path):
    roots = {}
    for project in ("a", "b"):
        root = tmp_path / project
        root.mkdir()
        (root / "a.py").write_text("first\r\n")
        (root / "b.py").write_text("second\n")
        roots[project] = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    coordinator = WriteCoordinator(store, roots.__getitem__, control_alive=lambda: True)
    yield roots, store, coordinator
    coordinator.close()
    store.close()


def begin(coordinator, project="a", **kwargs):
    coordinator.enable([project])
    return coordinator.begin_write_task(
        project, coordinator.status()["next_task_request_id"], **kwargs
    )


def test_default_off_local_scope_and_control_lost(parts):
    roots, store, c = parts
    ticket = c.status()["next_task_request_id"]
    with pytest.raises(WriteError, match="WRITE_DISABLED"):
        c.begin_write_task("a", ticket)
    c.enable(["a"])
    with pytest.raises(WriteError, match="WRITE_NOT_AUTHORIZED"):
        c.begin_write_task("b", ticket)
    c.control_alive = lambda: False
    with pytest.raises(WriteError, match="WRITE_DISABLED"):
        c.begin_write_task("a", ticket)
    assert store.query("SELECT * FROM tasks") == []
    assert not c.status()["write_enabled"]


def test_start_captures_hashes_not_full_content_and_replays_once(parts):
    roots, store, c = parts
    c.enable(["a"])
    request = c.status()["next_task_request_id"]
    result = c.begin_write_task("a", request, title="test")
    assert result["baseline_files"] == 2
    assert c.begin_write_task("a", request, title="test") == result
    rows = store.query("SELECT * FROM manifest")
    assert len(rows) == 2 and "first" not in json.dumps(rows)
    assert store.query("SELECT * FROM objects") == []
    with pytest.raises(WriteError, match="REQUEST_ID_CONFLICT"):
        c.begin_write_task("a", request, title="different")
    with pytest.raises(WriteError, match="WRITE_TASK_ACTIVE"):
        c.begin_write_task("a", c.status()["next_task_request_id"])


def test_status_preserves_task_goal_after_completion_and_restart(parts):
    roots, store, c = parts
    title = "超市系统登录与角色权限"
    task = begin(c, title=title)["task_id"]
    active = c.status()["active_task"]
    assert active["title"] == title and active["task_id"] == task
    assert "metadata" not in active and "scope" not in active and "begin_digest" not in active
    c.finish_write_task("a", task, "finish_title_01")
    assert c.status()["active_task"] is None
    assert c.status()["recent_task"]["title"] == title
    c.close()
    store.close()
    with RecoveryStore(store.root) as restarted_store:
        restarted = WriteCoordinator(restarted_store, roots.__getitem__, control_alive=lambda: True)
        try:
            assert restarted.status()["recent_task"]["title"] == title
            assert not restarted.status()["write_enabled"]
        finally:
            restarted.close()


@pytest.mark.parametrize("metadata", ["{}", '{"title":null}', '{"title":123}', "[]", "invalid"])
def test_status_uses_empty_goal_for_legacy_or_malformed_labels(parts, metadata):
    _, store, c = parts
    task = begin(c)["task_id"]
    with store.transaction() as db:
        db.execute("UPDATE tasks SET metadata=? WHERE task_id=?", (metadata, task))
    assert c.status()["active_task"]["title"] == ""
    assert c.status()["active_task"]["state"] == "active"


def test_late_join_external_change_not_misrepresented_as_origin(parts):
    roots, store, c = parts
    task = begin(c)["task_id"]
    c.check_first_touch("a", task, "a.py", roots["a"].read("a.py"))
    (roots["a"].root / "b.py").write_text("externally changed\n")
    with pytest.raises(WriteError, match="WRITE_ORIGIN_CONFLICT"):
        c.check_first_touch("a", task, "b.py", roots["a"].read("b.py"))
    c.enable(["a", "b"])
    with pytest.raises(WriteError, match="WRITE_TASK_SCOPE"):
        c.check_first_touch("b", task, "a.py", roots["b"].read("a.py"))


def test_explicit_task_scope_allows_only_declared_existing_and_future_paths(parts):
    roots, store, c = parts
    task = begin(c, paths=["future.py", "a.py"])["task_id"]
    assert len(store.query("SELECT * FROM manifest")) == 1
    c.check_first_touch("a", task, "future.py", None)
    with pytest.raises(WriteError, match="WRITE_TASK_PATH_SCOPE"):
        c.check_first_touch("a", task, "b.py", roots["a"].read("b.py"))


def test_future_nested_paths_require_explicit_new_parent_names(parts):
    roots, store, c = parts
    c.enable(["a"])
    ticket = c.status()["next_task_request_id"]
    with pytest.raises(WriteError, match="INVALID_TASK_SCOPE"):
        c.begin_write_task("a", ticket, paths=["new/future.py"])
    result = c.begin_write_task("a", ticket, paths=["new", "new/future.py"])
    assert result["baseline_files"] == 0
    assert not (roots["a"].root / "new").exists()


@pytest.mark.parametrize("scope", [None, ["original"]])
@pytest.mark.parametrize("kind", ["file", "directory"])
def test_removed_origin_directory_cannot_be_claimed_as_task_created(parts, scope, kind):
    roots, store, c = parts
    origin = roots["a"].root / "original"
    origin.mkdir()
    task = begin(c, paths=scope)["task_id"]
    assert store.query("SELECT path FROM baseline_directories WHERE task_id=?", (task,)) == [
        {"path": "original"}
    ]
    origin.rmdir()  # Simulated external removal after task start.
    with pytest.raises(WriteError, match="WRITE_ORIGIN_CONFLICT"):
        if kind == "file":
            c.create_file("a", task, "create_0001", "original", "not the origin\n")
        else:
            c.create_directory("a", task, "create_0001", "original")
    assert not origin.exists()
    assert store.query("SELECT * FROM operations") == []


def test_directory_kind_changed_to_file_is_not_a_new_origin(parts):
    roots, store, c = parts
    path = roots["a"].root / "original"
    path.mkdir()
    task = begin(c)["task_id"]
    path.rmdir()
    path.write_text("external replacement\n")
    with pytest.raises(WriteError, match="WRITE_ORIGIN_CONFLICT"):
        c.check_first_touch("a", task, "original", roots["a"].read("original"))


def test_completed_request_replay_expires_in_same_enable_session(parts):
    roots, store, c = parts
    task = begin(c)["task_id"]
    c.create_file("a", task, "create_0001", "new.py", "created\n")
    c.finish_write_task("a", task, "finish_0001")
    completed = store.query("SELECT completed FROM tasks WHERE task_id=?", (task,))[0]["completed"]
    c.clock = lambda: completed + 7 * 24 * 3600 + 1
    for request in (
        lambda: c.create_file("a", task, "create_0001", "new.py", "created\n"),
        lambda: c.finish_write_task("a", task, "finish_0001"),
    ):
        with pytest.raises(WriteError, match="WRITE_TASK_EXPIRED"):
            request()
    assert (roots["a"].root / "new.py").read_text() == "created\n"


def test_declared_future_parent_cannot_be_symlink(parts):
    roots, store, c = parts
    (roots["a"].root / "linked").symlink_to(roots["b"].root, target_is_directory=True)
    c.enable(["a"])
    with pytest.raises(WriteError, match="WRITE_BASELINE_UNSAFE"):
        c.begin_write_task(
            "a", c.status()["next_task_request_id"], paths=["linked", "linked/future.py"]
        )


@pytest.mark.parametrize("scope", [[], ["../outside.py"], ["a.py", "a.py"], [False], "."])
def test_invalid_scope_refuses_without_state(parts, scope):
    roots, store, c = parts
    c.enable(["a"])
    with pytest.raises(WriteError, match="INVALID_TASK_SCOPE"):
        c.begin_write_task("a", c.status()["next_task_request_id"], paths=scope)
    assert store.query("SELECT * FROM tasks") == []


def test_ignored_path_and_source_replacement_rejected(parts):
    roots, store, c = parts
    c.enable(["a"])
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        c.begin_write_task("a", c.status()["next_task_request_id"], paths=[".env"])
    root = roots["a"].root
    root.rename(root.parent / "original")
    root.mkdir()
    with pytest.raises(SourceError, match="SOURCE_REPLACED"):
        c.begin_write_task("a", c.status()["next_task_request_id"])


def test_restart_off_but_same_ticket_and_origin_remain(parts):
    roots, store, c = parts
    ticket = c.status()["next_task_request_id"]
    result = begin(c)
    c.close()
    replacement = WriteCoordinator(store, roots.__getitem__, control_alive=lambda: True)
    assert not replacement.status()["write_enabled"]
    replacement.enable(["a"])
    assert replacement.begin_write_task("a", ticket) == result
    replacement.close()


def test_byte_budget_refuses_without_task_or_ticket_consumption(parts):
    roots, store, c = parts
    c.baseline_bytes = 1
    c.enable(["a"])
    ticket = c.status()["next_task_request_id"]
    with pytest.raises(WriteError, match="WRITE_BASELINE_BYTE_LIMIT"):
        c.begin_write_task("a", ticket)
    assert c.status()["next_task_request_id"] == ticket
    assert store.query("SELECT * FROM tasks") == []


def test_retired_begin_request_never_reappears_as_new_task(parts):
    roots, store, c = parts
    ticket = c.status()["next_task_request_id"]
    result = begin(c)
    with store.transaction() as db:
        db.execute(
            "UPDATE tasks SET state='completed',completed=0 WHERE task_id=?", (result["task_id"],)
        )
    c.enable(["a"])
    assert store.query("SELECT * FROM tasks") == []
    with pytest.raises(WriteError, match="WRITE_REQUEST_EXPIRED"):
        c.begin_write_task("a", ticket)

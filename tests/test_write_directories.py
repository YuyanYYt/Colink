import json

import pytest

import code_context.write_operations as operations
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator, WriteError


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("one\n")
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    c = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    yield source, store, c
    c.close()
    store.close()


def begin(c, paths=None):
    c.enable(["a"])
    return c.begin_write_task("a", c.status()["next_task_request_id"], paths=paths)["task_id"]


def test_directory_and_nested_file_have_same_task_ownership_and_diff(parts):
    source, store, c = parts
    task = begin(c, ["new", "new/deeper", "new/deeper/file.py"])
    result = c.create_directory("a", task, "mkdir_001", "new")
    assert c.create_directory("a", task, "mkdir_001", "new") == result
    c.create_directory("a", task, "mkdir_002", "new/deeper")
    c.create_file("a", task, "create_001", "new/deeper/file.py", "created\n")
    rows = store.query("SELECT * FROM files ORDER BY path")
    assert [row["kind"] for row in rows] == ["directory", "directory", "created"]
    assert json.loads(rows[0]["directory_identity"])["mode"] == 0o755
    assert c.get_diff("a")["summary"]["directories_added"] == 2
    assert c.get_diff("a")["summary"]["files_changed"] == 1
    assert c.finish_write_task("a", task, "finish_001")["state"] == "completed"
    assert not list(source.root.rglob(".colink-write-*"))


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_existing_objects_never_adopted(parts, kind):
    source, store, c = parts
    path = source.root / "existing"
    if kind == "directory":
        path.mkdir()
    elif kind == "file":
        path.write_text("original\n")
    else:
        path.symlink_to(source.root / "a.py")
    task = begin(c)
    with pytest.raises(WriteError, match="WRITE_TARGET_EXISTS"):
        c.create_directory("a", task, "mkdir_001", "existing")
    assert store.query("SELECT * FROM operations") == []
    assert store.query("SELECT * FROM files") == []


def test_declared_scope_directory_and_new_parents_stay_explicit(parts):
    source, store, c = parts
    task = begin(c, ["declared"])
    with pytest.raises(WriteError, match="WRITE_TASK_PATH_SCOPE"):
        c.create_directory("a", task, "mkdir_001", "undeclared")
    with pytest.raises(SourceError, match="INVALID_PARENT"):
        c.create_directory("a", task, "mkdir_002", "missing/deeper")
    assert not (source.root / "missing").exists()


def test_control_closed_after_prepare_preserves_registered_temp_no_target(parts, monkeypatch):
    source, store, c = parts
    task = begin(c)
    actual = operations.prepare_directory

    def prepare(*args, **kwargs):
        prepared = actual(*args, **kwargs)
        c.disable()
        return prepared

    monkeypatch.setattr(operations, "prepare_directory", prepare)
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        c.create_directory("a", task, "mkdir_001", "new")
    assert not (source.root / "new").exists()
    temps = list(source.root.glob(".colink-write-*"))
    assert len(temps) == 1 and temps[0].is_dir()
    assert json.loads(store.query("SELECT * FROM operations")[0]["metadata"])["prepared"]
    assert c.status()["recovery_required"]


@pytest.mark.parametrize("path", [".git", ".code-context", "../escape", ".env", "node_modules"])
def test_excluded_directory_paths_cannot_write(parts, path):
    source, store, c = parts
    task = begin(c)
    with pytest.raises(SourceError):
        c.create_directory("a", task, "mkdir_001", path)
    assert store.query("SELECT * FROM operations") == []

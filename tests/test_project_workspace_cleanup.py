"""Complete local workspace retirement, independent of source/index availability."""

import fcntl
import hashlib
import json
import os
import plistlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import code_context.project_storage as storage
from code_context.project_registry import ProjectRegistry
from code_context.source_access import SourceError
from code_context.workspace import initialize_workspace


def saved(tmp_path, *, multiple=False, cache=True):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("def value(x): return x\n")
    key = hashlib.sha256(str(source).encode()).hexdigest()
    folder = data / ".code-context/desktop/workspaces" / key
    runtime = folder / "live-v1"
    registry = ProjectRegistry(source, data_dir=runtime / "registry")
    identifier = registry.register(display_name="Main", enabled=True)
    if multiple:
        (source / "child").mkdir()
        registry.register("child", display_name="Child", enabled=False)
    tunnel = folder / "tunnel"
    tunnel.mkdir(mode=0o700)
    (tunnel / "profile.yaml").write_text("synthetic local configuration")
    index = runtime / "index"
    index.mkdir(mode=0o700)
    if cache:
        project = index / "projects" / hashlib.sha256(identifier.encode()).hexdigest()
        project.mkdir(parents=True, mode=0o700)
        (project / "live-index.sqlite3").write_bytes(b"synthetic cache")
    (index / "live-index.sqlite3").write_bytes(b"synthetic old cache")
    terminal = runtime / "terminal-v1"
    terminal.mkdir(mode=0o700)
    (terminal / "request-synthetic.json").write_text("{}")
    return data, source, folder, identifier


@pytest.fixture
def trash(tmp_path, monkeypatch):
    path = tmp_path / "Trash"
    path.mkdir(mode=0o700)
    monkeypatch.setattr(storage, "_trash_directory", lambda: path)
    return path


def test_workspace_view_groups_projects_and_counts_all_data(tmp_path, monkeypatch):
    data, _, folder, _ = saved(tmp_path, multiple=True)
    before = {str(p): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("listing must be offline"))
    result = storage.project_storage(data, view="workspaces")
    assert result["total"] == 1 and result["total_projects"] == 2
    workspace = result["workspaces"][0]
    assert workspace["project_count"] == 2
    assert workspace["project_names"] == ["Main", "Child · child"]
    assert workspace["workspace_directory"] == str(folder)
    assert workspace["workspace_bytes"] == sum(len(v) for v in before.values())
    assert workspace["legacy_index_bytes"] == len(b"synthetic old cache")
    assert workspace["index_bytes"] == len(b"synthetic cachesynthetic old cache")
    assert before == {str(p): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_entire_workspace_and_registration_leave_list_and_can_be_rebuilt(tmp_path, trash):
    data, source, folder, _ = saved(tmp_path, multiple=True)
    key = folder.name
    before = {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    original = (source / "main.py").read_bytes()
    global_config = data / ".code-context/tunnel/profile.yaml"
    global_config.parent.mkdir()
    global_config.write_text("global configuration must remain")
    result = storage.trash_project_workspace(data, key)
    assert result["cleared"] and result["recoverable"] and result["project_count"] == 2
    assert not folder.exists() and storage.project_storage(data)["total"] == 0
    staged = Path(result["trash_path"])
    assert staged.parent == trash
    assert {**before, "live-v1/runtime.lock": b""} == {
        str(p.relative_to(staged)): p.read_bytes() for p in staged.rglob("*") if p.is_file()
    }
    assert (source / "main.py").read_bytes() == original
    assert global_config.read_text() == "global configuration must remain"
    assert storage.trash_project_workspace(data, key)["already_empty"]
    initialize_workspace(source, folder / "live-v1", name="Rebuilt")
    assert storage.project_storage(data)["total"] >= 1
    assert (source / "main.py").read_bytes() == original


def test_deleted_source_and_absent_new_index_can_still_be_cleared(tmp_path, trash):
    data, source, folder, _ = saved(tmp_path, cache=False)
    (source / "main.py").unlink()
    source.rmdir()
    row = storage.project_storage(data, view="workspaces")["workspaces"][0]
    assert not row["source_exists"] and row["workspace_bytes"] > 0
    assert storage.trash_project_workspace(data, folder.name)["cleared"]
    assert storage.project_storage(data, view="workspaces")["total_workspaces"] == 0


@pytest.mark.parametrize(
    "relative",
    ["runtime.lock", "index/project-index.lock", "index/live-index.lock", "terminal-v1/git.lock"],
)
def test_held_lifecycle_lease_keeps_workspace(tmp_path, trash, relative):
    data, _, folder, _ = saved(tmp_path)
    path = folder / "live-v1" / relative
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SourceError, match="WORKSPACE_IN_USE"):
            storage.trash_project_workspace(data, folder.name)
        assert folder.exists() and not list(trash.iterdir())
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "unknown-root", "unknown-runtime", "wrong-path"]
)
def test_unverified_or_foreign_data_is_retained(tmp_path, trash, kind):
    data, _, folder, _ = saved(tmp_path)
    file = folder / "tunnel/profile.yaml"
    if kind == "symlink":
        file.unlink()
        file.symlink_to(tmp_path / "other")
    elif kind == "hardlink":
        os.link(file, tmp_path / "other")
    elif kind == "unknown-root":
        (folder / "user-data").mkdir()
    elif kind == "unknown-runtime":
        (folder / "live-v1/user-database").mkdir()
    else:
        registry = folder / "live-v1/registry/projects.json"
        value = json.loads(registry.read_text())
        value["workspace"]["path"] += "-changed"
        registry.write_text(json.dumps(value))
    with pytest.raises(SourceError, match="WORKSPACE_CLEAR_REFUSED"):
        storage.trash_project_workspace(data, folder.name)
    assert folder.exists() and not list(trash.iterdir())


def test_pending_global_write_recovery_refuses_forget_and_never_changes_db(tmp_path, trash):
    data, _, folder, identifier = saved(tmp_path)
    recovery = data / ".code-context/write-recovery-v1"
    recovery.mkdir(mode=0o700)
    db_path = recovery / "recovery.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE tasks(project_id TEXT,state TEXT)")
        db.execute("INSERT INTO tasks VALUES (?, 'recovery_required')", (identifier,))
    before = db_path.read_bytes()
    with pytest.raises(SourceError, match="WORKSPACE_RECOVERY_REQUIRED"):
        storage.trash_project_workspace(data, folder.name)
    assert db_path.read_bytes() == before and folder.exists()
    assert not list(trash.iterdir())


def test_native_trash_staging_moves_whole_folder_and_leaves_another_workspace(
    tmp_path, monkeypatch
):
    data, source, folder, _ = saved(tmp_path)
    sibling_source = tmp_path / "other-source"
    sibling_source.mkdir()
    sibling_key = hashlib.sha256(str(sibling_source).encode()).hexdigest()
    other = folder.parent / sibling_key
    initialize_workspace(sibling_source, other / "live-v1")
    monkeypatch.setattr(
        storage, "_trash_directory", lambda: pytest.fail("native Trash owns final step")
    )
    result = storage.trash_project_workspace(data, folder.name, stage_for_native_trash=True)
    target = Path(result["trash_path"])
    assert (
        result["native_trash_pending"]
        and target.parent == data / ".code-context/retired-workspaces"
    )
    assert (target / "live-v1/registry/projects.json").is_file()
    assert other.is_dir() and source.is_dir()
    remaining = storage.project_storage(data, view="workspaces")
    assert remaining["total_workspaces"] == 1
    assert remaining["workspaces"][0]["id"] == sibling_key


@pytest.mark.parametrize("shape", [None, [], 7, "malformed"])
def test_malformed_registry_shape_does_not_break_other_rows(tmp_path, shape):
    data, _, folder, _ = saved(tmp_path)
    registry = folder / "live-v1/registry/projects.json"
    registry.write_text(json.dumps(shape))
    other = tmp_path / "other"
    other.mkdir()
    key = hashlib.sha256(str(other).encode()).hexdigest()
    initialize_workspace(other, folder.parent / key / "live-v1")
    result = storage.project_storage(data, view="workspaces")
    assert result["unavailable_metadata"] == 1 and result["total_workspaces"] == 1


def real_scope(folder):
    from code_context import execution_process
    from code_context.execution_scope import _scope_json, _supervisor_plist

    scope = folder / "live-v1/terminal-v1/scope" / ("1" * 32)
    scope.mkdir(mode=0o700, parents=True)
    control = {
        "uid": os.getuid(),
        "label": "com.colink.execution." + scope.name,
        "port": 45678,
        "token": "2" * 64,
        "job_id": "job-" + "3" * 32,
        "kernel_boot": "1234:5",
    }
    control_raw = _scope_json(control)
    plist_raw = plistlib.dumps(_supervisor_plist(scope, Path(execution_process.__file__).resolve()))
    identity = {
        "job_id": control["job_id"],
        "kernel_boot": control["kernel_boot"],
        "control_sha256": hashlib.sha256(control_raw).hexdigest(),
        "plist_sha256": hashlib.sha256(plist_raw).hexdigest(),
        "identity": {"pid": 12345, "unique": 45678, "version": 2},
        "resource_id": 98765,
    }
    for name, body in [
        ("control.json", control_raw),
        ("job.plist", plist_raw),
        ("identity.json", _scope_json(identity)),
    ]:
        (scope / name).write_bytes(body)
        (scope / name).chmod(0o600)
    return scope


@pytest.mark.parametrize("live", ["none", "pid", "member", "supervisor"])
def test_real_scope_format_allows_stopped_tasks_and_refuses_live_owners(
    tmp_path, trash, monkeypatch, live
):
    import code_context.execution_scope as scope_module

    data, _, folder, _ = saved(tmp_path)
    real_scope(folder)
    monkeypatch.setattr(scope_module, "_kernel_boot_identity", lambda: "1234:5")
    monkeypatch.setattr(
        scope_module,
        "MacKernel",
        lambda: SimpleNamespace(
            identity=lambda pid: object() if live == "pid" else None,
            members=lambda resource: {12345: object()} if live == "member" else {},
        ),
    )
    monkeypatch.setattr(
        storage.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0 if live == "supervisor" else 113,
        ),
    )
    if live == "none":
        assert storage.trash_project_workspace(data, folder.name)["cleared"]
    else:
        with pytest.raises(SourceError, match="WORKSPACE_TASK_IN_USE"):
            storage.trash_project_workspace(data, folder.name)
        assert folder.exists() and not list(trash.iterdir())


def test_external_task_mount_keeps_recovery_journal_and_workspace(tmp_path, trash):
    data, _, folder, _ = saved(tmp_path)
    jobs = folder / "live-v1/execution-v1/jobs"
    jobs.mkdir(parents=True)
    journal = jobs / ("job-" + "4" * 32 + ".disk.json")
    journal.write_text(
        json.dumps(
            {
                "state": "attached",
                "cleanup_verified": True,
                "mount": "/private/tmp/cl-w-owned",
            }
        )
    )
    journal.chmod(0o600)
    before = journal.read_bytes()
    with pytest.raises(SourceError, match="WORKSPACE_DISK_IN_USE"):
        storage.trash_project_workspace(data, folder.name)
    assert folder.exists() and journal.read_bytes() == before and not list(trash.iterdir())


@pytest.mark.parametrize("attached", [False, True])
def test_cache_attachment_outside_tree_is_checked(tmp_path, trash, monkeypatch, attached):
    data, _, folder, _ = saved(tmp_path)
    image = folder / "live-v1/execution-v1/cache/cache.sparsebundle"
    image.mkdir(parents=True)
    body = plistlib.dumps({"images": [{"image-path": str(image)}] if attached else []})
    monkeypatch.setattr(
        storage.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=body)
    )
    if attached:
        with pytest.raises(SourceError, match="WORKSPACE_DISK_IN_USE"):
            storage.trash_project_workspace(data, folder.name)
        assert folder.exists()
    else:
        assert storage.trash_project_workspace(data, folder.name)["cleared"]


def test_one_listing_deadline_is_shared_across_all_workspace_scans(tmp_path, monkeypatch):
    data, _, folder, _ = saved(tmp_path)
    for number in range(3):
        other = tmp_path / f"source-{number}"
        other.mkdir()
        key = hashlib.sha256(str(other).encode()).hexdigest()
        initialize_workspace(other, folder.parent / key / "live-v1")
    deadlines = []

    def count(boundary, relative, *, request_deadline):
        deadlines.append(request_deadline)
        return 1, 4096

    monkeypatch.setattr(storage, "_tree_storage", count)
    result = storage.project_storage(data, view="workspaces")
    assert result["total_workspaces"] == 4 and len(set(deadlines)) == 1

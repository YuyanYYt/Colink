"""Offline metadata stays useful after source deletion without touching data."""

import fcntl
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

import code_context.project_storage as storage_module
from code_context.project_registry import ProjectRegistry
from code_context.project_storage import project_storage, trash_project_index
from code_context.source_access import SourceError


def saved(tmp_path, name="Project", cache=True, schema=3):
    storage = tmp_path / "runtime"
    storage.mkdir(exist_ok=True, mode=0o700)
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "code.py").write_text("raise RuntimeError('must never execute')\n")
    key = hashlib.sha256(str(source).encode()).hexdigest()
    runtime = storage / ".code-context/desktop/workspaces" / key / "live-v1"
    registry = ProjectRegistry(source, data_dir=runtime / "registry")
    identifier = registry.register(display_name=name, enabled=True)
    registry_file = runtime / "registry/projects.json"
    value = json.loads(registry_file.read_text())
    value["schema"] = schema
    registry_file.write_text(json.dumps(value))
    index = runtime / "index/projects" / hashlib.sha256(identifier.encode()).hexdigest()
    if cache:
        index.mkdir(parents=True, mode=0o700)
        (index / "live-index.sqlite3").write_bytes(b"synthetic derived bytes")
        (index / "live-index.sqlite3-journal").write_bytes(b"journal")
    return storage, source, index, registry_file


def test_lists_exact_project_index_and_auxiliary_size_without_db_open(tmp_path, monkeypatch):
    storage, source, index, _ = saved(tmp_path)
    before = {p: p.read_bytes() for p in storage.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("listing must not open/initialize a SQLite database")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    result = project_storage(storage)
    assert result["total"] == 1 and result["unavailable_metadata"] == 0
    project = result["projects"][0]
    assert project["display_name"] == "Project" and project["source_path"] == str(source)
    assert project["index_directory"] == str(index) and project["source_exists"]
    assert project["index_bytes"] == len(b"synthetic derived bytesjournal")
    assert before == {p: p.read_bytes() for p in storage.rglob("*") if p.is_file()}


def test_deleted_original_source_still_maps_saved_cache(tmp_path):
    storage, source, index, _ = saved(tmp_path)
    (source / "code.py").unlink()
    source.rmdir()
    result = project_storage(storage)
    assert result["total"] == 1
    project = result["projects"][0]
    assert project["source_exists"] is False
    assert project["index_directory"] == str(index) and project["index_exists"]


def test_missing_cache_is_listed_without_creating_it(tmp_path):
    storage, source, index, _ = saved(tmp_path, cache=False)
    result = project_storage(storage)
    assert result["total"] == 1 and result["unavailable_metadata"] == 0
    project = result["projects"][0]
    assert project["source_path"] == str(source)
    assert project["index_directory"] == str(index) and project["index_bytes"] == 0
    assert project["index_exists"] is False and not index.exists()


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_reads_older_registry_shapes_without_migrating_them(tmp_path, schema):
    storage, _, _, registry = saved(tmp_path, schema=schema)
    before = registry.read_bytes()
    assert project_storage(storage)["total"] == 1
    assert registry.read_bytes() == before


@pytest.mark.parametrize("target", ["registry", "index", "file"])
def test_links_never_escape_into_other_directory_metadata(tmp_path, target):
    storage, _, index, registry = saved(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    if target == "registry":
        replacement = foreign / "projects.json"
        replacement.write_text(registry.read_text())
        registry.rename(registry.with_suffix(".saved"))
        registry.symlink_to(replacement)
    elif target == "index":
        index.rename(index.with_name("original-index"))
        index.symlink_to(foreign, target_is_directory=True)
    else:
        file = index / "live-index.sqlite3"
        file.rename(index / "original-db")
        file.symlink_to(foreign / "unknown-db")
    result = project_storage(storage)
    assert result["total"] == 0 and result["unavailable_metadata"] == 1


def test_damaged_registry_retained_without_echoing_content(tmp_path):
    storage, _, _, registry = saved(tmp_path)
    registry.write_text('{"pretend-token":')
    result = project_storage(storage)
    assert result["unavailable_metadata"] == 1
    assert "pretend-token" not in str(result)
    assert registry.read_text() == '{"pretend-token":'


def test_pagination_and_identical_names_keep_source_identity(tmp_path):
    storage, source, _, registry = saved(tmp_path)
    value = json.loads(registry.read_text())
    original = value["projects"][0]
    value["projects"] = [
        {**original, "project_id": f"p_{i:032x}", "relative_root": f"service{i}"} for i in range(25)
    ]
    registry.write_text(json.dumps(value))
    page = project_storage(storage, offset=10, limit=10)
    assert page["total"] == 25 and len(page["projects"]) == 10
    assert page["has_more"] and page["next_offset"] == 20
    assert len({project["index_directory"] for project in page["projects"]}) == 10
    assert all(Path(project["source_path"]).is_relative_to(source) for project in page["projects"])


def test_empty_data_directory_listing_has_no_new_files(tmp_path):
    storage = tmp_path / "empty"
    storage.mkdir()
    assert project_storage(storage)["projects"] == []
    assert list(storage.iterdir()) == []


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_unprotected_registry_refused_without_changing_permissions(tmp_path, mode):
    storage, _, _, registry = saved(tmp_path)
    os.chmod(registry, mode)
    assert project_storage(storage)["unavailable_metadata"] == 1
    assert registry.stat().st_mode & 0o777 == mode


@pytest.fixture
def trash(tmp_path, monkeypatch):
    directory = tmp_path / "Trash"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(storage_module, "_trash_directory", lambda: directory)
    return directory


def test_clear_deleted_project_moves_only_derived_index_to_recoverable_trash(tmp_path, trash):
    storage, source, index, registry = saved(tmp_path)
    row = project_storage(storage)["projects"][0]
    registry_before = registry.read_bytes()
    recovery = storage / ".code-context/write-recovery-v1/keep.db"
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b"unfinished recovery must remain")
    (source / "code.py").unlink()
    source.rmdir()
    result = trash_project_index(storage, row["id"])
    assert result["cleared"] and result["recoverable"] and not index.exists()
    assert Path(result["trash_path"]).is_relative_to(trash)
    assert (
        Path(result["trash_path"]) / "live-index.sqlite3"
    ).read_bytes() == b"synthetic derived bytes"
    assert registry.read_bytes() == registry_before
    assert recovery.read_bytes() == b"unfinished recovery must remain"
    assert project_storage(storage)["projects"][0]["index_exists"] is False
    assert trash_project_index(storage, row["id"])["already_empty"]


@pytest.mark.parametrize("lock_kind", ["runtime", "pool", "project"])
def test_active_runtime_or_index_cannot_be_cleared(tmp_path, trash, lock_kind):
    storage, _, index, _ = saved(tmp_path)
    row = project_storage(storage)["projects"][0]
    runtime = Path(row["runtime_directory"])
    path = {
        "runtime": runtime / "runtime.lock",
        "pool": runtime / "index/project-index.lock",
        "project": index / "live-index.lock",
    }[lock_kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SourceError, match="INDEX_IN_USE"):
            trash_project_index(storage, row["id"])
        assert index.is_dir() and not list(trash.iterdir())
    finally:
        os.close(fd)


@pytest.mark.parametrize("extra", ["unknown-file", "symlink", "hardlink"])
def test_unknown_or_linked_index_contents_are_preserved(tmp_path, trash, extra):
    storage, _, index, _ = saved(tmp_path)
    row = project_storage(storage)["projects"][0]
    if extra == "unknown-file":
        (index / "user-file.txt").write_text("user file")
    elif extra == "symlink":
        (index / "live-index.sqlite3").unlink()
        (index / "live-index.sqlite3").symlink_to(tmp_path / "unrelated")
    else:
        os.link(index / "live-index.sqlite3", tmp_path / "linked-database")
    with pytest.raises(SourceError):
        trash_project_index(storage, row["id"])
    assert index.is_dir() and not list(trash.iterdir())


def test_unregistered_id_cannot_select_another_hashed_folder(tmp_path, trash):
    storage, _, index, _ = saved(tmp_path)
    row = project_storage(storage)["projects"][0]
    fake = row["id"].split(":")[0] + ":p_" + "0" * 32
    with pytest.raises(SourceError):
        trash_project_index(storage, fake)
    assert index.is_dir() and not list(trash.iterdir())


def test_native_trash_staging_is_owned_recoverable_and_never_opens_user_trash(
    tmp_path, monkeypatch
):
    storage, _, index, registry = saved(tmp_path)
    row = project_storage(storage)["projects"][0]
    before = registry.read_bytes()

    def forbidden():
        pytest.fail("native macOS trash must be handled by the app, not direct Python access")

    monkeypatch.setattr(storage_module, "_trash_directory", forbidden)
    result = trash_project_index(storage, row["id"], stage_for_native_trash=True)
    staged = Path(result["trash_path"])
    assert result["cleared"] and result["native_trash_pending"] and not index.exists()
    assert staged.is_relative_to(storage / ".code-context/retired-indexes")
    assert (staged / "live-index.sqlite3").read_bytes() == b"synthetic derived bytes"
    assert registry.read_bytes() == before

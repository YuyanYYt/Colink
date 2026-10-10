"""Explicit migration preserves exact facts, ownership and existing caches."""

import fcntl
import hashlib
import json
import os
import sqlite3
import zlib
from pathlib import Path

import pytest

import code_context.project_index_migration as migration
from code_context.live_index import _APPLICATION_ID, _SCHEMA, _SCHEMA_VERSION, _TABLES
from code_context.project_index_migration import migrate_project_indexes
from code_context.project_registry import ProjectRegistry
from code_context.source_access import SourceError


def _database(path, rows, *, old_binding=False, full_indexes=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = sqlite3.connect(path)
    try:
        db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
        db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        db.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA:
            if old_binding and "li_binding_cache" in statement:
                continue
            if full_indexes and statement.startswith("CREATE INDEX ci_relation_"):
                statement = statement.split(" WHERE ")[0]
            db.execute(statement)
        for ordinal, (identifier, source_id) in enumerate(rows, start=1):
            db.execute(
                "INSERT INTO li_projects VALUES (?,?,'manifest','version','{}',?)",
                (identifier, source_id, ordinal),
            )
            db.execute(
                "INSERT INTO files VALUES (?,?,1,'code.py','sha',7,'[1,2,3]')",
                (identifier, source_id),
            )
            parsed = b"CP1:" + zlib.compress(b'{"original":"unchanged"}')
            db.execute(
                "INSERT INTO li_parse_cache VALUES (?,?,'code.py','sha','version',?)",
                (identifier, source_id, parsed),
            )
            db.execute(
                "INSERT INTO ci_files VALUES (?,?,1,'code.py','python','indexed','code','[]')",
                (identifier, source_id),
            )
            db.execute(
                "INSERT INTO ci_symbols VALUES (?,?,1,?,'code.py','f','f','function',1,9,'{}')",
                (identifier, source_id, "symbol_" + identifier),
            )
            # Distinct call sites have duplicate endpoints but differing rowids.
            for line in (9, 2, 7):
                payload = b"CL1:" + zlib.compress(json.dumps({"line": line}).encode())
                db.execute(
                    "INSERT INTO ci_relations VALUES (?,?,1,'code.py',?,'code.py',?,"
                    "'CALL','resolved',?,?)",
                    (
                        identifier,
                        source_id,
                        "symbol_" + identifier,
                        "symbol_" + identifier,
                        line,
                        payload,
                    ),
                )
            if not old_binding:
                binding = b"BC1:" + zlib.compress(b'{"binding":"unchanged"}')
                db.execute(
                    "INSERT INTO li_binding_cache VALUES (?,?,'code.py',?)",
                    (identifier, source_id, binding),
                )
        db.commit()
    finally:
        db.close()


def _saved(tmp_path, *, count=2, old_binding=False, full_indexes=False, extra=()):
    storage = tmp_path / "runtime"
    storage.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("raise RuntimeError('source must not execute')\n")
    key = hashlib.sha256(str(source).encode()).hexdigest()
    runtime = storage / ".code-context/desktop/workspaces" / key / "live-v1"
    registry = ProjectRegistry(source, data_dir=runtime / "registry")
    identifiers = [registry.register(display_name="Root", enabled=True)]
    for i in range(1, count):
        (source / f"sub{i}").mkdir()
        identifiers.append(registry.register(f"sub{i}", display_name=f"Sub {i}", enabled=True))
    rows = json.loads((runtime / "registry/projects.json").read_text())["projects"]
    mapping = {row["project_id"]: row["source_id"] for row in rows}
    old = runtime / "index/live-index.sqlite3"
    _database(old, [*mapping.items(), *extra], old_binding=old_binding, full_indexes=full_indexes)
    return storage, key, runtime, mapping, old


def _facts(path, identifier):
    db = sqlite3.connect(path)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            table: db.execute(
                f'SELECT rowid,* FROM "{table}" WHERE project_id=? ORDER BY rowid', (identifier,)
            ).fetchall()
            for table in ("li_projects", *_TABLES)
            if table in tables
        }
    finally:
        db.close()


def _target(runtime, identifier):
    return (
        runtime
        / "index/projects"
        / hashlib.sha256(identifier.encode()).hexdigest()
        / "live-index.sqlite3"
    )


@pytest.fixture
def trash(tmp_path, monkeypatch):
    path = tmp_path / "Trash"
    path.mkdir(mode=0o700)
    monkeypatch.setattr(migration.storage, "_trash_directory", lambda: path)
    return path


@pytest.mark.parametrize("old_binding", [False, True])
@pytest.mark.parametrize("full_indexes", [False, True])
def test_multi_project_split_preserves_rowids_and_blob_payloads(
    tmp_path, old_binding, full_indexes
):
    storage, key, runtime, mapping, old = _saved(
        tmp_path, old_binding=old_binding, full_indexes=full_indexes
    )
    before = {identifier: _facts(old, identifier) for identifier in mapping}
    registration = (runtime / "registry/projects.json").read_bytes()
    result = migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert result["retired"] and len(result["migrated_projects"]) == 2
    assert result["native_trash_pending"] and result["recoverable"]
    assert result["unregistered_projects"] == result["stale_source_projects"] == 0
    for identifier in mapping:
        after = _facts(_target(runtime, identifier), identifier)
        assert {table: after[table] for table in before[identifier]} == before[identifier]
        assert _target(runtime, identifier).stat().st_nlink == 1
        db = sqlite3.connect(_target(runtime, identifier))
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        db.close()
    assert not old.exists()
    assert (Path(result["trash_path"]) / old.name).is_file()
    assert (runtime / "registry/projects.json").read_bytes() == registration
    assert migrate_project_indexes(storage, key, stage_for_native_trash=True)["already_current"]


def test_existing_current_cache_is_retained_byte_for_byte(tmp_path, trash):
    storage, key, runtime, mapping, old = _saved(tmp_path)
    identifier, source_id = next(iter(mapping.items()))
    target = _target(runtime, identifier)
    _database(target, [(identifier, source_id)])
    db = sqlite3.connect(target)
    db.execute("UPDATE li_projects SET manifest='newer-existing-cache'")
    db.commit()
    db.close()
    before = target.read_bytes()
    result = migrate_project_indexes(storage, key)
    assert result["existing_projects"] == [identifier]
    assert len(result["migrated_projects"]) == 1
    assert target.read_bytes() == before and not old.exists()
    assert Path(result["trash_path"]).is_relative_to(trash)


def test_unregistered_projects_retire_recoverably_without_import(tmp_path):
    identifier = "p_" + "0" * 32
    storage, key, runtime, mapping, old = _saved(tmp_path, extra=[(identifier, "a" * 64)])
    expected = _facts(old, identifier)
    result = migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert result["unregistered_projects"] == 1
    assert not _target(runtime, identifier).exists()
    assert _facts(Path(result["trash_path"]) / old.name, identifier) == expected
    assert len(result["migrated_projects"]) == len(mapping)


def test_stale_source_facts_retire_without_import(tmp_path):
    storage, key, runtime, mapping, old = _saved(tmp_path, count=1)
    db = sqlite3.connect(old)
    for table in ("li_projects", *_TABLES):
        db.execute(f'UPDATE "{table}" SET source_id=?', ("a" * 64,))
    db.commit()
    db.close()
    result = migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert result["stale_source_projects"] == 1
    assert result["migrated_projects"] == []
    assert not _target(runtime, next(iter(mapping))).exists()


def test_empty_legacy_database_retires_without_creating_project_cache(tmp_path):
    storage, key, runtime, mapping, old = _saved(tmp_path)
    old.unlink()
    _database(old, [])
    result = migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert result["legacy_projects"] == 0 and result["retired"]
    assert all(not _target(runtime, identifier).exists() for identifier in mapping)


@pytest.mark.parametrize("damage", ["bytes", "app_id", "schema", "foreign_key", "sidecar"])
def test_corrupt_or_unfamiliar_legacy_is_unchanged(tmp_path, damage):
    storage, key, runtime, _, old = _saved(tmp_path)
    if damage == "bytes":
        old.write_bytes(b"not a SQLite file")
    elif damage == "sidecar":
        old.with_name(old.name + "-wal").write_bytes(b"unsettled journal")
    else:
        db = sqlite3.connect(old)
        if damage == "app_id":
            db.execute("PRAGMA application_id=123")
        elif damage == "schema":
            db.execute("CREATE TABLE user_owned(secret TEXT)")
        else:
            db.execute("INSERT INTO files VALUES ('unknown','other',1,'x',NULL,0,'[]')")
        db.commit()
        db.close()
    before = old.read_bytes()
    with pytest.raises(SourceError, match="INDEX_MIGRATION_REFUSED"):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert old.read_bytes() == before
    assert not (runtime / "index/projects").exists()


@pytest.mark.parametrize("lock_kind", ["runtime", "pool", "legacy", "current"])
def test_all_relevant_live_owners_are_respected(tmp_path, lock_kind):
    storage, key, runtime, mapping, old = _saved(tmp_path)
    target = _target(runtime, next(iter(mapping))).parent
    target.mkdir(parents=True, mode=0o700)
    path = {
        "runtime": runtime / "runtime.lock",
        "pool": runtime / "index/project-index.lock",
        "legacy": runtime / "index/live-index.lock",
        "current": target / "live-index.lock",
    }[lock_kind]
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SourceError, match="INDEX_IN_USE"):
            migrate_project_indexes(storage, key, stage_for_native_trash=True)
        assert old.exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize("link_kind", ["legacy_symlink", "legacy_hardlink", "parent", "current"])
def test_links_refuse_without_touching_linked_data(tmp_path, link_kind):
    storage, key, runtime, mapping, old = _saved(tmp_path)
    identifier = next(iter(mapping))
    if link_kind == "legacy_symlink":
        real = old.with_name("saved.db")
        old.rename(real)
        old.symlink_to(real)
    elif link_kind == "legacy_hardlink":
        real = tmp_path / "linked.db"
        os.link(old, real)
    elif link_kind == "parent":
        real = runtime.parent.with_name("original")
        runtime.parent.rename(real)
        runtime.parent.symlink_to(real, target_is_directory=True)
        real = real / "live-v1/index/live-index.sqlite3"
    else:
        target = _target(runtime, identifier)
        target.parent.mkdir(parents=True)
        real = tmp_path / "not-ours.db"
        real.write_bytes(b"untouched")
        target.symlink_to(real)
    before = real.read_bytes()
    with pytest.raises(SourceError):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert real.read_bytes() == before


def test_current_corruption_preserves_all_legacy_and_existing(tmp_path):
    storage, key, runtime, mapping, old = _saved(tmp_path)
    target = _target(runtime, next(iter(mapping)))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing corrupt bytes")
    before = old.read_bytes()
    with pytest.raises(SourceError, match="INDEX_MIGRATION_REFUSED"):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert old.read_bytes() == before and target.read_bytes() == b"existing corrupt bytes"


def test_registration_hash_and_source_association_must_match(tmp_path):
    storage, key, runtime, _, old = _saved(tmp_path)
    registry = runtime / "registry/projects.json"
    value = json.loads(registry.read_text())
    value["projects"][0]["source_id"] = "e" * 64
    registry.write_text(json.dumps(value))
    before = old.read_bytes()
    with pytest.raises(SourceError):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert old.read_bytes() == before


def test_partial_build_failure_restarts_without_overwrite(tmp_path, monkeypatch):
    storage, key, runtime, mapping, old = _saved(tmp_path, count=3)
    original = migration._copy_project
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated storage exhaustion")
        return original(*args)

    monkeypatch.setattr(migration, "_copy_project", fail_second)
    before = old.read_bytes()
    with pytest.raises(SourceError):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert old.read_bytes() == before
    first = _target(runtime, sorted(mapping)[0])
    first_bytes = first.read_bytes()
    assert list((storage / ".code-context/retired-indexes").iterdir())
    monkeypatch.setattr(migration, "_copy_project", original)
    result = migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert result["existing_projects"] == [sorted(mapping)[0]]
    assert len(result["migrated_projects"]) == 2 and not old.exists()
    assert first.read_bytes() == first_bytes


def test_publication_never_replaces_racing_file(tmp_path, monkeypatch):
    storage, key, runtime, mapping, old = _saved(tmp_path, count=1)
    target = _target(runtime, next(iter(mapping)))
    original = migration._rename_exclusive

    def race(source_parent, source, target_parent, destination):
        if source.startswith("project-"):
            target.write_bytes(b"must not overwrite")
        return original(source_parent, source, target_parent, destination)

    monkeypatch.setattr(migration, "_rename_exclusive", race)
    before = old.read_bytes()
    with pytest.raises(SourceError):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert old.read_bytes() == before and target.read_bytes() == b"must not overwrite"
    assert list((storage / ".code-context/retired-indexes").rglob("project-*.sqlite3"))


def test_deleted_original_source_does_not_block_offline_cache_migration(tmp_path):
    storage, key, runtime, mapping, _ = _saved(tmp_path, count=1)
    source = Path(json.loads((runtime / "registry/projects.json").read_text())["workspace"]["path"])
    (source / "code.py").unlink()
    source.rmdir()
    result = migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert result["retired"] and len(result["migrated_projects"]) == 1
    assert _target(runtime, next(iter(mapping))).is_file()
    assert not source.exists()


def test_retirement_failure_restores_old_and_keeps_durable_current(tmp_path, monkeypatch):
    storage, key, runtime, mapping, old = _saved(tmp_path, count=1)
    original = migration._rename_exclusive

    def fail_old_lock(source_parent, source, target_parent, destination):
        if source == "live-index.lock":
            raise OSError("simulated Trash failure")
        return original(source_parent, source, target_parent, destination)

    before = old.read_bytes()
    monkeypatch.setattr(migration, "_rename_exclusive", fail_old_lock)
    with pytest.raises(SourceError):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert old.read_bytes() == before
    target = _target(runtime, next(iter(mapping)))
    target_bytes = target.read_bytes()
    monkeypatch.setattr(migration, "_rename_exclusive", original)
    result = migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert result["retired"] and not result["migrated_projects"]
    assert target.read_bytes() == target_bytes


def test_empty_current_cache_is_not_overwritten_or_silently_promoted(tmp_path):
    storage, key, runtime, mapping, old = _saved(tmp_path, count=1)
    target = _target(runtime, next(iter(mapping)))
    _database(target, [])
    before = old.read_bytes(), target.read_bytes()
    with pytest.raises(SourceError):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert (old.read_bytes(), target.read_bytes()) == before


def test_unknown_current_file_prevents_retirement(tmp_path):
    storage, key, runtime, mapping, old = _saved(tmp_path, count=1)
    identifier, source_id = next(iter(mapping.items()))
    target = _target(runtime, identifier)
    _database(target, [(identifier, source_id)])
    (target.parent / "unknown.txt").write_text("preserve user file")
    before = old.read_bytes(), target.read_bytes()
    with pytest.raises(SourceError):
        migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert (old.read_bytes(), target.read_bytes()) == before


def test_no_legacy_cache_does_not_initialize_any_database(tmp_path):
    storage, key, runtime, mapping, old = _saved(tmp_path, count=1)
    old.unlink()
    result = migrate_project_indexes(storage, key, stage_for_native_trash=True)
    assert result["already_current"] and not result["retired"]
    assert not list(runtime.rglob("*.sqlite3"))
    assert all(not _target(runtime, identifier).exists() for identifier in mapping)

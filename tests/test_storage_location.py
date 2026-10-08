"""Relocation keeps source identity and never overwrites or loses old state."""

import fcntl
import json
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from code_context.onboarding import configure_desktop
from code_context.recovery_store import RecoveryStore
from code_context.storage_location import (
    StorageLocationError,
    inspect_storage,
    relocate_storage,
)
from code_context.tunnel import load_profile


@pytest.fixture
def configured(tmp_path):
    root = tmp_path / "original"
    root.mkdir(mode=0o700)
    sample = root / "examples/sample_project"
    sample.mkdir(parents=True)
    (sample / "main.py").write_text("print('user source stays at its original location')\n")
    configure_desktop(
        root,
        json.dumps({"tunnel_id": "tunnel_" + "a" * 32, "api_key": "sk-" + "b" * 32}),
    )
    return root


def test_copy_preserves_credentials_and_project_root_without_copying_source(configured, tmp_path):
    target = tmp_path / "destination"
    old_profile = configured / ".code-context/tunnel/profile.yaml"
    original_profile = old_profile.read_bytes()
    result = relocate_storage(configured, target)
    assert result["copied"] and result["original_retained"]
    assert (configured / ".env.local").read_bytes() == (target / ".env.local").read_bytes()
    assert old_profile.read_bytes() == original_profile
    profile, source, project, data = load_profile(target / ".code-context/tunnel/profile.yaml")
    assert source == configured / "examples/sample_project"
    assert project == "sample"
    assert data == target / ".code-context/local-sample"
    assert profile["health"]["url_file"] == str(target / ".code-context/tunnel/health.url")
    assert shlex.split(profile["mcp"]["commands"][0]["command"])[0] == sys.executable
    assert not (target / "examples").exists()
    selection = target / ".code-context/desktop/selection-live.json"
    assert json.loads(selection.read_text())["selected_root"] == str(source)
    assert (source / "main.py").is_file()
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / ".env.local").stat().st_mode & 0o777 == 0o600


def test_multiple_project_profiles_and_selection_keep_external_source_identity(
    configured, tmp_path
):
    from code_context.tunnel import prepare_profile

    external = tmp_path / "external-project"
    external.mkdir()
    state = configured / ".code-context/desktop/workspaces/saved/live-v1"
    profile = state.parent / "tunnel/profile.yaml"
    prepare_profile(
        external, "other-project", state, "tunnel_" + "a" * 32, profile, mode="workspace"
    )
    selection = configured / ".code-context/desktop/selection-live.json"
    selection.write_text(json.dumps({"selected_root": str(external)}))
    target = tmp_path / "destination"
    relocate_storage(configured, target)
    _, selected, _, moved = load_profile(target / profile.relative_to(configured))
    assert selected == external
    assert moved == target / state.relative_to(configured)
    assert (target / selection.relative_to(configured)).read_bytes() == selection.read_bytes()


def test_completed_recovery_objects_rebind_to_verified_copy(configured, tmp_path):
    state = configured / ".code-context/write-recovery-v1"
    with RecoveryStore(state) as store:
        sha = store.put_blob(b"retained original for completed operation", "test-owner")
    target = tmp_path / "destination"
    relocate_storage(configured, target)
    with RecoveryStore(target / ".code-context/write-recovery-v1") as copied:
        assert copied.read_blob(sha) == b"retained original for completed operation"
    with RecoveryStore(state) as original:
        assert original.read_blob(sha) == b"retained original for completed operation"


@pytest.mark.parametrize("state", ["active", "recovery_required", "rolling_back"])
def test_incomplete_write_tasks_reject_before_destination_created(configured, tmp_path, state):
    recovery = configured / ".code-context/write-recovery-v1"
    with RecoveryStore(recovery) as store:
        with store.transaction() as db:
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
                ("task", "request", "project", "source", state, 0, None, "{}"),
            )
    target = tmp_path / "destination"
    with pytest.raises(StorageLocationError, match="STORAGE_RECOVERY_REQUIRED"):
        relocate_storage(configured, target)
    assert not target.exists()
    assert (recovery / "recovery.sqlite3").exists()


def test_pending_recovery_objects_reject(configured, tmp_path):
    recovery = configured / ".code-context/write-recovery-v1"
    with RecoveryStore(recovery) as store:
        sha = store.put_blob(b"origin", "owner")
        with store.transaction() as db:
            db.execute("UPDATE objects SET state='pending' WHERE sha256=?", (sha,))
    with pytest.raises(StorageLocationError, match="STORAGE_RECOVERY_REQUIRED"):
        relocate_storage(configured, tmp_path / "destination")


@pytest.mark.parametrize("name", ["connection.lock", "runtime.lock", "recovery.lock"])
def test_held_lifecycle_lease_rejects_copy(configured, tmp_path, name):
    path = configured / ".code-context" / name
    path.touch(mode=0o600)
    with path.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(StorageLocationError, match="STORAGE_CONNECTION_ACTIVE"):
            relocate_storage(configured, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_nonempty_target_is_not_overwritten_or_chmodded(configured, tmp_path):
    target = tmp_path / "destination"
    target.mkdir(mode=0o755)
    (target / "user-data.txt").write_text("retain")
    before = target.stat().st_mode
    with pytest.raises(StorageLocationError, match="STORAGE_TARGET_NOT_EMPTY"):
        relocate_storage(configured, target)
    assert (target / "user-data.txt").read_text() == "retain"
    assert target.stat().st_mode == before
    assert not (target / ".code-context").exists()


def test_owned_empty_picker_directory_becomes_private(configured, tmp_path):
    target = tmp_path / "destination"
    target.mkdir(mode=0o755)
    assert relocate_storage(configured, target)["copied"]
    assert target.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("source_link", [False, True])
def test_source_or_destination_symlink_ancestors_are_rejected(configured, tmp_path, source_link):
    alias = tmp_path / "alias"
    alias.symlink_to(configured if source_link else tmp_path, target_is_directory=True)
    source = alias if source_link else configured
    destination = tmp_path / "destination" if source_link else alias / "destination"
    with pytest.raises(StorageLocationError, match="STORAGE_DIRECTORY_UNSAFE"):
        relocate_storage(source, destination)
    assert (configured / ".env.local").exists()


def test_linked_state_is_never_followed(configured, tmp_path):
    outside = tmp_path / "original-material.txt"
    outside.write_text("retain")
    (configured / ".code-context/linked").symlink_to(outside)
    with pytest.raises(StorageLocationError, match="STORAGE_FILE_UNSAFE"):
        relocate_storage(configured, tmp_path / "destination")
    assert outside.read_text() == "retain"


def test_source_change_during_copy_retains_partial_target_and_old_data(
    configured, tmp_path, monkeypatch
):
    import code_context.storage_location as storage

    original = storage._copy_file

    def change_source(source, target, relative, expected):
        original(source, target, relative, expected)
        if relative.name == "profile.yaml":
            (source / ".code-context/added.json").write_text("{}")

    monkeypatch.setattr(storage, "_copy_file", change_source)
    target = tmp_path / "destination"
    with pytest.raises(StorageLocationError, match="STORAGE_CHANGED"):
        relocate_storage(configured, target)
    assert (target / ".code-context/tunnel/profile.yaml").exists()
    assert (configured / ".env.local").exists()


def test_profile_outside_state_is_rejected_without_deleting_original(configured, tmp_path):
    profile = configured / ".code-context/tunnel/profile.yaml"
    value = json.loads(profile.read_text())
    args = shlex.split(value["mcp"]["commands"][0]["command"])
    args[9] = str(tmp_path / "external-state")
    value["mcp"]["commands"][0]["command"] = shlex.join(args)
    profile.write_text(json.dumps(value))
    original = profile.read_bytes()
    with pytest.raises(StorageLocationError, match="STORAGE_PROFILE_INVALID"):
        relocate_storage(configured, tmp_path / "destination")
    assert profile.read_bytes() == original


def test_inspect_is_read_only_and_cli_never_exposes_credential_body(configured):
    credential = configured / ".env.local"
    before = credential.stat()
    assert inspect_storage(configured)["ready"]
    assert credential.stat().st_mtime_ns == before.st_mtime_ns
    result = subprocess.run(
        [sys.executable, "-B", "-m", "code_context.storage_location", str(configured)],
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(result.stdout)["ready"]
    assert "sk-" not in result.stdout + result.stderr


def test_overlap_rejected_and_same_location_is_checked(configured):
    with pytest.raises(StorageLocationError, match="STORAGE_OVERLAP"):
        relocate_storage(configured, configured / "child")
    with pytest.raises(StorageLocationError, match="STORAGE_OVERLAP"):
        relocate_storage(configured, configured.parent)
    assert relocate_storage(configured, configured)["copied"] is False


def test_invalid_database_stays_at_original_location(configured, tmp_path):
    path = configured / ".code-context/broken.sqlite3"
    path.write_bytes(b"not a database")
    with pytest.raises(StorageLocationError, match="STORAGE_DATABASE_INVALID"):
        relocate_storage(configured, tmp_path / "destination")
    assert path.read_bytes() == b"not a database"


def _leave_committed_wal(path, statements):
    """Abrupt writer exit leaves real, committed, uncheckpointed WAL pages."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import json, os, sqlite3, sys; "
            "db=sqlite3.connect(sys.argv[1]); "
            "db.execute('PRAGMA journal_mode=WAL'); "
            "db.execute('PRAGMA wal_autocheckpoint=0'); "
            "[db.execute(statement) for statement in json.loads(sys.argv[2])]; "
            "db.commit(); os._exit(0)",
            str(path),
            json.dumps(statements),
        ],
        check=True,
    )
    assert Path(str(path) + "-wal").is_file()
    assert Path(str(path) + "-wal").stat().st_size > 0


def test_sqlite_backup_preserves_committed_wal_only_data(configured, tmp_path):
    database = configured / ".code-context/index/live-index.sqlite3"
    _leave_committed_wal(
        database,
        [
            "CREATE TABLE facts (id INTEGER PRIMARY KEY, label TEXT)",
            "INSERT INTO facts VALUES (1, 'committed exclusively in WAL')",
            "INSERT INTO facts VALUES (2, 'second committed row')",
        ],
    )
    target = tmp_path / "destination"
    result = relocate_storage(configured, target)
    assert result["copied"]
    copied = target / database.relative_to(configured)
    with closing(sqlite3.connect(copied)) as db:
        assert db.execute("SELECT * FROM facts ORDER BY id").fetchall() == [
            (1, "committed exclusively in WAL"),
            (2, "second committed row"),
        ]
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not Path(str(copied) + "-wal").exists()
    assert not Path(str(copied) + "-shm").exists()
    with closing(sqlite3.connect(database)) as original:
        assert original.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_real_sqlite_close_may_remove_sidecars_before_copy(configured, tmp_path):
    import code_context.storage_location as storage

    database = configured / ".code-context/index/live-index.sqlite3"
    _leave_committed_wal(
        database, ["CREATE TABLE facts (value TEXT)", "INSERT INTO facts VALUES ('retained')"]
    )
    entries = storage._inventory(configured)
    assert all(not str(path).endswith(("-wal", "-shm")) for path in entries)
    # SQLite's last ordinary reader close checkpoints and deletes bookkeeping;
    # a precomputed file-copy list must never depend on those sidecars existing.
    with closing(sqlite3.connect(database)) as reader:
        assert reader.execute("SELECT value FROM facts").fetchone()[0] == "retained"
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()
    target = tmp_path / "destination"
    assert relocate_storage(configured, target)["copied"]
    with closing(sqlite3.connect(target / database.relative_to(configured))) as copied:
        assert copied.execute("SELECT value FROM facts").fetchone()[0] == "retained"


def test_recovery_pending_only_in_wal_is_still_rejected(configured, tmp_path):
    recovery = configured / ".code-context/write-recovery-v1"
    with RecoveryStore(recovery):
        pass
    database = recovery / "recovery.sqlite3"
    _leave_committed_wal(
        database,
        ["INSERT INTO tasks VALUES('task','request','project','source','active',0,NULL,'{}')"],
    )
    target = tmp_path / "destination"
    with pytest.raises(StorageLocationError, match="STORAGE_RECOVERY_REQUIRED"):
        relocate_storage(configured, target)
    assert not target.exists()


def test_readonly_inspection_followed_by_relocation_keeps_wal_data(configured, tmp_path):
    database = configured / ".code-context/index/live-index.sqlite3"
    _leave_committed_wal(
        database,
        ["CREATE TABLE facts (value TEXT)", "INSERT INTO facts VALUES ('WAL after inspection')"],
    )
    assert inspect_storage(configured)["ready"]
    target = tmp_path / "destination"
    assert relocate_storage(configured, target)["copied"]
    with closing(sqlite3.connect(target / database.relative_to(configured))) as copied:
        assert copied.execute("SELECT value FROM facts").fetchone()[0] == "WAL after inspection"


def test_corrupt_recovery_identity_is_not_reauthorized(configured, tmp_path):
    recovery = configured / ".code-context/write-recovery-v1"
    with RecoveryStore(recovery) as store:
        sha = store.put_blob(b"original", "owner")
    with sqlite3.connect(recovery / "recovery.sqlite3") as db:
        db.execute("UPDATE objects SET identity='[0,0,0]' WHERE sha256=?", (sha,))
    with pytest.raises(StorageLocationError, match="STORAGE_RECOVERY_CHANGED"):
        relocate_storage(configured, tmp_path / "destination")


def test_cli_failure_does_not_disclose_source_error(configured, tmp_path):
    target = tmp_path / "destination"
    target.mkdir()
    (target / "original.txt").write_text("retain")
    result = subprocess.run(
        [sys.executable, "-B", "-m", "code_context.storage_location", str(configured), str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "error": "STORAGE_RELOCATION_FAILED",
        "original_retained": True,
    }
    assert result.stderr == ""


def test_swift_storage_default_and_build_include_custom_storage():
    root = Path(__file__).resolve().parents[1]
    native = (root / "macos/CodeConnect/StorageLocation.swift").read_text()
    runtime = (
        (root / "macos/CodeConnect/Runtime.swift").read_text().split("struct WorkspaceProject")[0]
    )
    builder = (root / "macos/build.py").read_text()
    assert 'appendingPathComponent("CoLink-data"' in native
    assert "applicationSupportDirectory" not in runtime
    assert "StorageLocation.savedURL ?? StorageLocation.defaultURL" in runtime
    assert "try previous.relocated(to: workspace)" in runtime
    assert 'str(sources / "StorageLocation.swift")' in builder

import fcntl
import hashlib
import json
import os
import stat
from dataclasses import replace

import pytest

import code_context.source_access as access_module
import code_context.source_identity as identity_module
from code_context.project_registry import (
    ProjectRegistry,
    RegistryError,
    upgrade_legacy_registry_identity,
)
from code_context.recovery_store import RecoveryStore
from code_context.scanner import _identity
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator, WriteError


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "main.py").write_text("VALUE = 1\n")
    child = root / "child"
    child.mkdir()
    (child / ".git").mkdir()
    data = tmp_path / "state/registry"
    registry = ProjectRegistry(root, data_dir=data)
    registry.register(display_name="Existing", aliases=("saved",), enabled=True)
    registry.register("child", enabled=False)
    original = json.loads((data / "projects.json").read_text())
    device = root.stat().st_dev + 3
    original["schema"] = 2
    original["workspace"].pop("stable_id")
    workspace_id = SourceAccess(root).legacy_source_id(device)
    original["workspace"]["source_id"] = workspace_id
    for row in original["projects"]:
        row.pop("stable_id")
        relative = row["relative_root"]
        row["source_id"] = SourceAccess(root / relative).legacy_source_id(device)
        encoded = json.dumps([workspace_id, relative, row["source_id"]]).encode()
        row["project_id"] = "p_" + hashlib.sha256(encoded).hexdigest()[:32]
    original["authorized_projects"] = sorted(
        row["project_id"] for row in original["projects"] if row["enabled"]
    )
    path = data / "projects.json"
    path.write_text(json.dumps(original))
    path.chmod(0o600)
    (data.parent / "runtime.lock").touch(mode=0o600)
    return root, data, device, original


def test_volume_identity_is_obtained_from_each_ancestor_even_with_equal_dev(tmp_path, monkeypatch):
    observed = []

    def volume(fd):
        observed.append(_identity(os.fstat(fd)))
        return "uuid:" + str(len(observed))

    monkeypatch.setattr(identity_module, "volume_identity", volume)
    source = SourceAccess(tmp_path)
    assert len(observed) == 1 + len(source.ancestor_identity)
    assert observed[0] == source.root_identity


def test_persistent_identity_survives_device_change_but_live_accessor_rejects_it(
    tmp_path, monkeypatch
):
    before = SourceAccess(tmp_path)
    raw_identity = access_module._identity
    raw_stable = access_module.stable_source_identities

    def changed(info):
        device, inode, mode = raw_identity(info)
        return device + 3, inode, mode

    def stable(root, fd, root_identity, ancestors):
        root_identity = (root_identity[0] - 3, *root_identity[1:])
        ancestors = tuple((p[0] - 3, *p[1:]) for p in ancestors)
        return raw_stable(root, fd, root_identity, ancestors)

    monkeypatch.setattr(access_module, "_identity", changed)
    monkeypatch.setattr(access_module, "stable_source_identities", stable)
    after = SourceAccess(tmp_path)
    assert before.stable_id == after.stable_id
    assert before.legacy_source_id() != after.legacy_source_id()
    with pytest.raises(SourceError, match="SOURCE_REPLACED"):
        before.ensure_available()
    after.ensure_available()


def test_volume_change_changes_identity_and_registry_rejects_it(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    data = tmp_path / "registry"
    ProjectRegistry(root, data_dir=data)
    before = (data / "projects.json").read_bytes()
    monkeypatch.setattr(identity_module, "volume_identity", lambda fd: "uuid:other-volume")
    with pytest.raises(RegistryError, match="REGISTRY_WORKSPACE_MISMATCH"):
        ProjectRegistry(root, data_dir=data)
    assert (data / "projects.json").read_bytes() == before


def test_missing_volume_identity_fails_closed(tmp_path, monkeypatch):
    def unavailable(fd):
        raise OSError("unavailable")

    monkeypatch.setattr(identity_module, "volume_identity", unavailable)
    with pytest.raises(SourceError, match="SOURCE_UNAVAILABLE"):
        SourceAccess(tmp_path)


@pytest.mark.parametrize("schema", [1, 2])
def test_explicit_legacy_upgrade_preserves_ids_authorization_and_original_backup(legacy, schema):
    root, data, device, old = legacy
    old["schema"] = schema
    if schema == 1:
        for row in old["projects"]:
            row.pop("name_origin")
    (data / "projects.json").write_text(json.dumps(old))
    original = (data / "projects.json").read_bytes()
    # Opaque recovery/execution records must not be rewritten by registration repair.
    (data.parent / "pending-recovery-record.bin").write_bytes(b"original incomplete receipt")
    upgrade_legacy_registry_identity(root, data, device)
    assert (data / "projects.pre-volume-v1.json").read_bytes() == original
    assert stat.S_IMODE((data / "projects.pre-volume-v1.json").stat().st_mode) == 0o600
    saved = json.loads((data / "projects.json").read_text())
    assert saved["schema"] == 3
    assert saved["authorized_projects"] == old["authorized_projects"]
    assert saved["workspace"]["source_id"] == old["workspace"]["source_id"]
    assert saved["workspace"]["stable_id"] == SourceAccess(root).stable_id
    for before, after in zip(old["projects"], saved["projects"], strict=True):
        assert {key: after[key] for key in before} == before
    registry = ProjectRegistry(root, data_dir=data)
    project_id = old["authorized_projects"][0]
    source_id = next(row["source_id"] for row in old["projects"] if row["enabled"])
    assert registry.source(project_id).source_id == source_id
    assert registry.source(project_id)._fingerprints.source_id == source_id
    assert registry.resolve_name("saved") == project_id
    registry.discover()
    assert (
        registry.register(display_name="Existing", aliases=("saved",), enabled=True) == project_id
    )
    assert ProjectRegistry(root, data_dir=data).resolve_name("saved") == project_id
    assert (
        data.parent / "pending-recovery-record.bin"
    ).read_bytes() == b"original incomplete receipt"


@pytest.mark.parametrize(
    "problem", ["device", "workspace", "ancestor", "project", "id", "authorized"]
)
def test_legacy_upgrade_rejects_incorrect_or_replaced_binding_without_modifying_state(
    legacy, problem
):
    root, data, device, old = legacy
    if problem == "device":
        device += 1
    elif problem == "workspace":
        root.rename(root.with_name("saved-workspace"))
        root.mkdir()
    elif problem == "ancestor":
        saved_parent = root.parent.with_name(root.parent.name + "-saved")
        root.parent.rename(saved_parent)
        root.parent.mkdir()
        (saved_parent / root.name).rename(root)
        (saved_parent / "state").rename(data.parent)
    elif problem == "project":
        (root / "child").rename(root / "saved-child")
        (root / "child").mkdir()
    elif problem == "id":
        old["projects"][0]["project_id"] = "p_forged"
        (data / "projects.json").write_text(json.dumps(old))
    elif problem == "authorized":
        old["authorized_projects"] = []
        (data / "projects.json").write_text(json.dumps(old))
    original = (data / "projects.json").read_bytes()
    with pytest.raises(RegistryError):
        upgrade_legacy_registry_identity(root, data, device)
    assert (data / "projects.json").read_bytes() == original
    assert not (data / "projects.pre-volume-v1.json").exists()


def test_local_upgrade_refuses_active_runtime(legacy):
    root, data, device, _ = legacy
    original = (data / "projects.json").read_bytes()
    with (data.parent / "runtime.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RegistryError, match="WORKSPACE_ALREADY_RUNNING"):
            upgrade_legacy_registry_identity(root, data, device)
    assert (data / "projects.json").read_bytes() == original


def test_legacy_upgrade_rechecks_sources_before_committing(legacy, monkeypatch):
    root, data, device, _ = legacy
    original = (data / "projects.json").read_bytes()
    save = ProjectRegistry._save

    def replace_then_save(registry):
        (root / "child").rename(root / "saved-child")
        (root / "child").mkdir()
        return save(registry)

    monkeypatch.setattr(ProjectRegistry, "_save", replace_then_save)
    with pytest.raises(RegistryError):
        upgrade_legacy_registry_identity(root, data, device)
    assert (data / "projects.json").read_bytes() == original


def test_migration_preserves_real_pending_recovery_and_does_not_enable_writes(legacy):
    root, data, device, old = legacy
    row = next(row for row in old["projects"] if row["enabled"])
    recovery = data.parent / "write-recovery-v1"
    with RecoveryStore(recovery) as store, store.transaction() as db:
        db.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
            (
                "pending",
                "request-pending",
                row["project_id"],
                row["source_id"],
                "recovery_required",
                0,
                None,
                "{}",
            ),
        )
    original = (recovery / "recovery.sqlite3").read_bytes()
    upgrade_legacy_registry_identity(root, data, device)
    assert (recovery / "recovery.sqlite3").read_bytes() == original
    registry = ProjectRegistry(root, data_dir=data)
    with RecoveryStore(recovery) as store:
        writer = WriteCoordinator(store, registry.source, control_alive=lambda: True)
        assert writer.status()["recovery_required"]
        assert not writer.status()["write_enabled"]
        with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
            writer.enable([row["project_id"]])
        assert store.query("SELECT source_id FROM tasks")[0]["source_id"] == row["source_id"]


def test_upgrade_is_idempotent_and_preserves_backup(legacy):
    root, data, device, _ = legacy
    assert upgrade_legacy_registry_identity(root, data, device)["upgraded"]
    original = (data / "projects.json").read_bytes()
    backup = (data / "projects.pre-volume-v1.json").read_bytes()
    assert not upgrade_legacy_registry_identity(root, data, device)["upgraded"]
    assert (data / "projects.json").read_bytes() == original
    assert (data / "projects.pre-volume-v1.json").read_bytes() == backup


def test_interrupted_backup_publish_can_retry_without_a_partial_final_backup(legacy, monkeypatch):
    root, data, device, _ = legacy
    original = (data / "projects.json").read_bytes()
    sync = os.fsync

    def interrupted(fd):
        raise OSError("simulated interruption before backup publish")

    monkeypatch.setattr(os, "fsync", interrupted)
    with pytest.raises(SourceError):
        upgrade_legacy_registry_identity(root, data, device)
    assert (data / "projects.json").read_bytes() == original
    assert not (data / "projects.pre-volume-v1.json").exists()
    monkeypatch.setattr(os, "fsync", sync)
    assert upgrade_legacy_registry_identity(root, data, device)["upgraded"]
    assert (data / "projects.pre-volume-v1.json").read_bytes() == original


def test_existing_fifo_backup_is_rejected_without_blocking(legacy):
    root, data, device, _ = legacy
    original = (data / "projects.json").read_bytes()
    os.mkfifo(data / "projects.pre-volume-v1.json", 0o600)
    with pytest.raises(RegistryError, match="UNSAFE_REGISTRY_STATE"):
        upgrade_legacy_registry_identity(root, data, device)
    assert (data / "projects.json").read_bytes() == original


def test_state_parent_replacement_after_lock_is_rejected_before_migration(legacy, monkeypatch):
    root, data, device, _ = legacy
    original = (data / "projects.json").read_bytes()
    load = ProjectRegistry._load

    def replace_before_load(registry, state):
        saved = data.parent.with_name("saved-state")
        data.parent.rename(saved)
        data.parent.mkdir(mode=0o700)
        (saved / "registry").rename(data)
        return load(registry, state)

    monkeypatch.setattr(ProjectRegistry, "_load", replace_before_load)
    with pytest.raises(RegistryError):
        upgrade_legacy_registry_identity(root, data, device)
    assert (data / "projects.json").read_bytes() == original
    assert not (data / "projects.pre-volume-v1.json").exists()


def test_unavailable_legacy_project_recovers_its_original_id_when_restored(legacy):
    root, data, _, old = legacy
    device = root.stat().st_dev
    workspace_id = SourceAccess(root).legacy_source_id()
    old["workspace"]["source_id"] = workspace_id
    for row in old["projects"]:
        row["source_id"] = SourceAccess(root / row["relative_root"]).legacy_source_id()
        encoded = json.dumps([workspace_id, row["relative_root"], row["source_id"]]).encode()
        row["project_id"] = "p_" + hashlib.sha256(encoded).hexdigest()[:32]
    old["authorized_projects"] = sorted(
        row["project_id"] for row in old["projects"] if row["enabled"]
    )
    (data / "projects.json").write_text(json.dumps(old))
    child = next(row for row in old["projects"] if row["relative_root"] == "child")
    (root / "child").rename(root / "saved-child")
    ProjectRegistry(root, data_dir=data)
    partial = json.loads((data / "projects.json").read_text())
    assert (
        next(row for row in partial["projects"] if row["relative_root"] == "child")["stable_id"]
        is None
    )
    (root / "saved-child").rename(root / "child")
    result = upgrade_legacy_registry_identity(root, data, device)
    restored = next(row for row in result["projects"] if row["relative_root"] == "child")
    assert restored["project_id"] == child["project_id"]
    assert restored["status"] == "pending" and not restored["enabled"]
    saved = json.loads((data / "projects.json").read_text())
    assert next(row for row in saved["projects"] if row["relative_root"] == "child")["stable_id"]


def test_backup_interrupted_after_atomic_publish_remains_complete_and_retryable(
    legacy, monkeypatch
):
    root, data, device, _ = legacy
    original = (data / "projects.json").read_bytes()
    sync = os.fsync
    calls = 0

    def interrupted(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption after atomic publish")
        sync(fd)

    monkeypatch.setattr(os, "fsync", interrupted)
    with pytest.raises(SourceError):
        upgrade_legacy_registry_identity(root, data, device)
    backup = data / "projects.pre-volume-v1.json"
    assert backup.read_bytes() == original and backup.stat().st_nlink == 1
    assert (data / "projects.json").read_bytes() == original
    monkeypatch.setattr(os, "fsync", sync)
    assert upgrade_legacy_registry_identity(root, data, device)["upgraded"]


def test_null_source_rechecks_legacy_binding_between_probes(legacy, monkeypatch):
    root, data, device, old = legacy
    upgrade_legacy_registry_identity(root, data, device)
    project = next(row for row in old["projects"] if row["relative_root"] == "child")
    registry = ProjectRegistry(root, data_dir=data)
    registry._legacy_device = device
    registry._projects[project["project_id"]] = replace(
        registry._projects[project["project_id"]], stable_id=None
    )
    registry.set_enabled(project["project_id"], True)
    before = (data / "projects.json").read_bytes()
    available = registry._available

    def replace_after_probe(row):
        result = available(row)
        (root / "child").rename(root / "saved-child")
        (root / "child").mkdir()
        return result

    monkeypatch.setattr(registry, "_available", replace_after_probe)
    with pytest.raises(RegistryError, match="PROJECT_SOURCE_CHANGED"):
        registry.source(project["project_id"])
    assert (data / "projects.json").read_bytes() == before

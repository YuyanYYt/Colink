"""Reboot compatibility only; no edits to user source or write receipts."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

import code_context.recovery_identity as identities
from code_context.recovery_store import RecoveryError, RecoveryStore
from code_context.scanner import _identity
from code_context.storage_location import relocate_storage


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "state"
    with RecoveryStore(root) as store:
        sha = store.put_blob(b"retained recovery body", "origin")
        with store.transaction() as db:
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
                (
                    "task",
                    "request",
                    "project",
                    "source",
                    "recovery_required",
                    1,
                    None,
                    '{"dev":42}',
                ),
            )
    device = root.stat().st_dev + 3
    blob = root / "objects" / sha
    with sqlite3.connect(root / "recovery.sqlite3") as db:
        db.execute("DROP TABLE object_identities")
        db.execute(
            "UPDATE objects SET identity=? WHERE sha256=?",
            (json.dumps([device, *_identity(blob.stat())[1:]]), sha),
        )
    return root, sha, device


def test_legacy_repair_preserves_original_database_bodies_and_pending_task(legacy, tmp_path):
    root, sha, device = legacy
    database = root / "recovery.sqlite3"
    before = database.read_bytes()
    body = (root / "objects" / sha).read_bytes()
    with pytest.raises(RecoveryError, match="RECOVERY_OBJECT_CHANGED"):
        RecoveryStore(root)
    assert database.read_bytes() == before
    report = identities.upgrade_recovery_identity(root, device, tmp_path / "backups")
    assert report["updated_bindings"] == 1
    assert Path(report["backup"]).read_bytes() == before
    assert Path(report["backup"]).stat().st_mode & 0o777 == 0o600
    with RecoveryStore(root) as store:
        assert store.read_blob(sha) == body
        task = store.query("SELECT * FROM tasks")[0]
        assert task["state"] == "recovery_required"
        assert task["metadata"] == '{"dev":42}'
        assert store.query("SELECT * FROM object_refs") == [{"owner": "origin", "sha256": sha}]
        assert len(store.query("PRAGMA table_info(objects)")) == 4


def test_stable_objects_translate_only_device_on_next_boot_and_still_collect(tmp_path):
    root = tmp_path / "state"
    with RecoveryStore(root) as store:
        sha = store.put_blob(b"retained", "owner")
    body = root / "objects" / sha
    with sqlite3.connect(root / "recovery.sqlite3") as db:
        db.execute(
            "UPDATE objects SET identity=? WHERE sha256=?",
            (json.dumps([body.stat().st_dev + 3, *_identity(body.stat())[1:]]), sha),
        )
    with RecoveryStore(root) as store:
        assert store.identity_upgrade["updated_bindings"] == 1
        assert store.read_blob(sha) == b"retained"
        assert json.loads(store.query("SELECT identity FROM objects")[0]["identity"]) == list(
            _identity(body.stat())
        )
        store.release_owner("owner")
        assert store.collect_unreferenced() == 1
        assert store.query("SELECT * FROM object_identities") == []


@pytest.mark.parametrize("damage", ["device", "inode", "mode", "body", "size", "hardlink"])
def test_unproved_legacy_identity_does_not_change_database(legacy, tmp_path, damage):
    root, sha, device = legacy
    blob = root / "objects" / sha
    if damage == "body":
        blob.write_bytes(b"changed recovery body")
    elif damage == "mode":
        blob.chmod(0o400)
    elif damage == "hardlink":
        os.link(blob, tmp_path / "linked-body")
    elif damage == "inode":
        info = _identity(blob.stat())
        with sqlite3.connect(root / "recovery.sqlite3") as db:
            db.execute(
                "UPDATE objects SET identity=?", (json.dumps([device, info[1] + 1, info[2]]),)
            )
    elif damage == "size":
        with sqlite3.connect(root / "recovery.sqlite3") as db:
            db.execute("UPDATE objects SET size=size+1")
    before = (root / "recovery.sqlite3").read_bytes()
    with pytest.raises(RecoveryError, match="RECOVERY_OBJECT_CHANGED|UNSAFE_RECOVERY_FILE"):
        identities.upgrade_recovery_identity(
            root, device + 1 if damage == "device" else device, tmp_path / "backups"
        )
    assert (root / "recovery.sqlite3").read_bytes() == before
    assert blob.exists()


def test_stable_parent_binding_change_still_refuses_open(tmp_path):
    root = tmp_path / "state"
    with RecoveryStore(root) as store:
        store.put_blob(b"bound", "owner")
    with sqlite3.connect(root / "recovery.sqlite3") as db:
        db.execute("UPDATE object_identities SET stable_identity='[\"different-volume\",1,32768]'")
    before = (root / "recovery.sqlite3").read_bytes()
    with pytest.raises(RecoveryError, match="RECOVERY_OBJECT_CHANGED"):
        RecoveryStore(root)
    assert (root / "recovery.sqlite3").read_bytes() == before


def test_repair_refuses_active_store_without_touching_it(legacy, tmp_path):
    root, sha, device = legacy
    identities.upgrade_recovery_identity(root, device, tmp_path / "backups")
    with RecoveryStore(root) as store:
        before = (root / "recovery.sqlite3").read_bytes()
        with pytest.raises(RecoveryError, match="RECOVERY_ALREADY_OPEN"):
            identities.upgrade_recovery_identity(root, device, tmp_path / "backups")
        assert (root / "recovery.sqlite3").read_bytes() == before
        assert store.read_blob(sha) == b"retained recovery body"


def test_replacement_after_backup_keeps_original_binding_and_backup(legacy, tmp_path, monkeypatch):
    root, sha, device = legacy
    before = (root / "recovery.sqlite3").read_bytes()
    backup = identities._backup

    def changed(store, backup_dir):
        result = backup(store, backup_dir)
        blob = root / "objects" / sha
        blob.rename(tmp_path / "original-body")
        blob.write_bytes(b"retained recovery body")
        blob.chmod(0o600)
        return result

    monkeypatch.setattr(identities, "_backup", changed)
    with pytest.raises(RecoveryError, match="RECOVERY_OBJECT_CHANGED"):
        identities.upgrade_recovery_identity(root, device, tmp_path / "backups")
    assert (root / "recovery.sqlite3").read_bytes() == before
    originals = [p for p in (tmp_path / "backups").glob("*.sqlite3") if "snapshot" not in p.name]
    assert len(originals) == 1 and originals[0].read_bytes() == before


def test_complete_legacy_pending_body_stays_pending(legacy, tmp_path):
    root, sha, device = legacy
    with sqlite3.connect(root / "recovery.sqlite3") as db:
        db.execute("UPDATE objects SET state='pending'")
    identities.upgrade_recovery_identity(root, device, tmp_path / "backups")
    with RecoveryStore(root) as store:
        assert store.query("SELECT state FROM objects") == [{"state": "pending"}]
        assert store.read_blob(sha, _recovering=True) == b"retained recovery body"
        with pytest.raises(RecoveryError, match="RECOVERY_NOT_READY"):
            store.read_blob(sha)


@pytest.mark.parametrize("registered", [False, True])
def test_interrupted_pending_body_preserves_existing_startup_semantics(tmp_path, registered):
    root = tmp_path / "state"
    with RecoveryStore(root) as store:
        sha = store.put_blob(b"original full body", "owner")
    blob = root / "objects" / sha
    blob.write_bytes(b"partial")
    with sqlite3.connect(root / "recovery.sqlite3") as db:
        db.execute("UPDATE objects SET state='pending'")
        if registered:
            db.execute(
                "UPDATE objects SET identity=?",
                (json.dumps([blob.stat().st_dev + 3, *_identity(blob.stat())[1:]]),),
            )
        else:
            db.execute("UPDATE objects SET identity=NULL")
            db.execute("DELETE FROM object_identities")
    with RecoveryStore(root) as store:
        assert store.usage()["object_count"] == 1
        assert store.query("SELECT state FROM objects") == [{"state": "pending"}]
        with pytest.raises(
            RecoveryError, match="RECOVERY_OBJECT_CHANGED|RECOVERY_IDENTITY_UNAVAILABLE"
        ):
            store.read_blob(sha, _recovering=True)
    assert blob.read_bytes() == b"partial"


def test_backup_contains_committed_wal_state_and_raw_auxiliaries(legacy, tmp_path):
    root, _, device = legacy
    database = root / "recovery.sqlite3"
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO settings VALUES('committed-in-wal','preserved')")
        writer.commit()
        captured = {
            suffix: Path(str(database) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")
        }
    writer.close()
    # Restore the bytes that a crash would leave, without a live SQLite writer.
    for suffix, raw in captured.items():
        path = Path(str(database) + suffix)
        path.write_bytes(raw)
        path.chmod(0o600)
    report = identities.upgrade_recovery_identity(root, device, tmp_path / "backups")
    backup = Path(report["backup"])
    assert Path(str(backup) + "-wal").is_file()
    snapshot = backup.with_name(backup.stem + "-snapshot.sqlite3")
    with sqlite3.connect(snapshot) as restored:
        assert restored.execute(
            "SELECT value FROM settings WHERE key='committed-in-wal'"
        ).fetchone() == ("preserved",)


def test_storage_relocation_rebinds_verified_stable_bodies_after_device_drift(tmp_path):
    source = tmp_path / "source"
    root = source / ".code-context/write-recovery-v1"
    with RecoveryStore(root) as store:
        sha = store.put_blob(b"copied recovery body", "owner")
    blob = root / "objects" / sha
    with sqlite3.connect(root / "recovery.sqlite3") as db:
        db.execute(
            "UPDATE objects SET identity=?",
            (json.dumps([blob.stat().st_dev + 3, *_identity(blob.stat())[1:]]),),
        )
    before = (root / "recovery.sqlite3").read_bytes()
    target = tmp_path / "target"
    assert relocate_storage(source, target)["original_retained"]
    assert (root / "recovery.sqlite3").read_bytes() == before
    with RecoveryStore(target / ".code-context/write-recovery-v1") as store:
        assert store.read_blob(sha) == b"copied recovery body"

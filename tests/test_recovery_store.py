"""Recovery storage only: all objects are synthetic, no source project writes."""

import hashlib
import json
import os
import sqlite3

import pytest

from code_context.recovery_store import RecoveryError, RecoveryStore, encode_metadata


def test_objects_private_verified_deduplicated_referenced_and_survive_reopen(tmp_path):
    root = tmp_path / "state"
    raw = ("本地\r\n" * 100).encode()
    with RecoveryStore(root) as store:
        sha = store.put_blob(raw, "task:origin")
        assert sha == hashlib.sha256(raw).hexdigest()
        assert store.put_blob(raw, "op:before") == sha
        assert store.read_blob(sha) == raw
        assert store.usage()["object_count"] == 1
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert store.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 1
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert (root / "objects" / sha).stat().st_mode & 0o777 == 0o600
        store.release_owner("op:before")
        assert store.collect_unreferenced() == 0
    with RecoveryStore(root) as store:
        assert store.read_blob(sha) == raw
        store.release_owner("task:origin")
        assert store.collect_unreferenced() == 1
        assert not (root / "objects" / sha).exists()
        assert store.usage()["object_count"] == 0
        assert store.usage()["sqlite_auxiliary_bytes"] == 0


def test_three_hundred_versions_only_retain_origin_and_current_pending_object(tmp_path):
    raw = b"x" * (100 * 1024 - 8)
    with RecoveryStore(tmp_path / "state") as store:
        origin = store.put_blob(raw + b"original", "task:origin")
        maximum = 0
        for number in range(300):
            updated = raw + f"{number:08d}".encode()
            sha = store.put_blob(updated, "op:pending")
            assert store.read_blob(sha) == updated
            store.collect_unreferenced()
            usage = store.usage()
            maximum = max(maximum, usage["resident_bytes"])
            assert usage["object_count"] == 2
        assert store.read_blob(origin) == raw + b"original"
        assert maximum < 512 * 1024
        store.release_owner("op:pending")
        store.collect_unreferenced()
        assert store.usage()["object_count"] == 1
        assert store.usage()["object_bytes"] < 128 * 1024
        assert not (store.root / "recovery.sqlite3-wal").exists()


def test_reserve_refuses_capacity_without_saving_or_losing_protected_materials(tmp_path):
    with RecoveryStore(
        tmp_path / "state",
        max_bytes=512 * 1024,
        max_peak_bytes=1024 * 1024,
        max_metadata_bytes=256 * 1024,
    ) as store:
        sha = store.put_blob(b"protected", "task:origin")
        before = store.usage()
        with pytest.raises(RecoveryError, match="RECOVERY_CAPACITY"):
            store.put_blob(b"q" * (512 * 1024), "too-large")
        assert store.usage() == before
        assert store.read_blob(sha) == b"protected"
        with pytest.raises(RecoveryError, match="RECOVERY_CAPACITY"):
            store.reserve(source_temp_bytes=2 * 1024 * 1024)


def test_low_disk_rejects_before_object_changes(tmp_path, monkeypatch):
    with RecoveryStore(tmp_path / "state") as store:
        monkeypatch.setattr(
            "code_context.recovery_store.shutil.disk_usage",
            lambda _: type("Space", (), {"free": 1})(),
        )
        with pytest.raises(RecoveryError, match="RECOVERY_DISK_SPACE"):
            store.put_blob(b"new", "op")
        assert store.query("SELECT * FROM objects") == []
        assert list((store.root / "objects").iterdir()) == []


def test_metadata_reservation_keeps_completion_headroom_before_any_write(tmp_path):
    with RecoveryStore(
        tmp_path / "metadata-headroom",
        max_bytes=512 * 1024,
        max_peak_bytes=1024 * 1024,
        max_metadata_bytes=128 * 1024,
        min_free_bytes=0,
    ) as store:
        before = store.usage()
        with pytest.raises(RecoveryError, match="RECOVERY_METADATA_CAPACITY"):
            store.reserve(metadata_bytes=128 * 1024)
        assert store.usage() == before
        assert store.query("SELECT * FROM objects") == []


def test_metadata_transaction_rollback_and_reopen_persistence(tmp_path):
    root = tmp_path / "state"
    with RecoveryStore(root) as store:
        with store.transaction() as db:
            db.execute("INSERT INTO settings VALUES(?,?)", ("ticket", "opaque-test-ticket"))
        with pytest.raises(RecoveryError, match="RECOVERY_COMMIT_FAILED"):
            with store.transaction() as db:
                db.execute("INSERT INTO settings VALUES(?,?)", ("other", "not-committed"))
                db.execute("INSERT INTO settings VALUES(?,?)", ("ticket", "duplicate"))
        assert store.query("SELECT * FROM settings WHERE key!='schema_version'") == [
            {"key": "ticket", "value": "opaque-test-ticket"}
        ]
    with RecoveryStore(root) as store:
        assert (
            store.query("SELECT value FROM settings WHERE key='ticket'")[0]["value"]
            == "opaque-test-ticket"
        )


def test_single_lease_and_file_replacement_and_hardlink_rejected(tmp_path):
    root = tmp_path / "state"
    with RecoveryStore(root) as store:
        with pytest.raises(RecoveryError, match="RECOVERY_ALREADY_OPEN"):
            RecoveryStore(root)
        sha = store.put_blob(b"owned", "task")
        link = tmp_path / "hardlink"
        os.link(root / "objects" / sha, link)
        with pytest.raises(RecoveryError, match="UNSAFE_RECOVERY_FILE"):
            store.read_blob(sha)
        assert link.read_bytes() == b"owned"
    with pytest.raises(RecoveryError, match="UNSAFE_RECOVERY_FILE"):
        RecoveryStore(root)


def test_unknown_and_tampered_objects_never_reclaimed(tmp_path):
    with RecoveryStore(tmp_path / "state") as store:
        sha = store.put_blob(b"saved", "task")
        store.release_owner("task")
        (store.root / "objects" / sha).write_bytes(b"external-content")
        with pytest.raises(RecoveryError, match="RECOVERY_OBJECT_CHANGED"):
            store.collect_unreferenced()
        assert (store.root / "objects" / sha).read_bytes() == b"external-content"
    root = tmp_path / "other"
    with RecoveryStore(root) as store:
        unknown = root / "objects" / ("a" * 64)
        unknown.write_bytes(b"not-registered")
        unknown.chmod(0o600)
        with pytest.raises(RecoveryError, match="UNKNOWN_RECOVERY_OBJECT"):
            store.usage()
        assert unknown.read_bytes() == b"not-registered"


def test_unreferenced_retirement_interruption_can_resume_without_body(tmp_path):
    with RecoveryStore(tmp_path / "state") as store:
        sha = store.put_blob(b"retired", "task")
        store.release_owner("task")
        with store.transaction() as db:
            db.execute("UPDATE objects SET state='retiring' WHERE sha256=?", (sha,))
        # Mimic this registered garbage collector's crash after unlink and before row deletion.
        (store.root / "objects" / sha).unlink()
        assert store.collect_unreferenced() == 1
        assert store.query("SELECT * FROM objects") == []


@pytest.mark.parametrize("value", [float("nan"), {"x": "v" * (256 * 1024)}, {"x": object()}])
def test_metadata_limits_are_content_free(value):
    with pytest.raises(RecoveryError, match="RECOVERY_METADATA_LIMIT") as error:
        encode_metadata(value)
    assert "vvv" not in str(error.value)
    assert json.loads(encode_metadata({"x": "中文"})) == {"x": "中文"}


def test_database_replacement_refuses_queries(tmp_path):
    with RecoveryStore(tmp_path / "state") as store:
        database = store.root / "recovery.sqlite3"
        database.rename(store.root / "saved-original.sqlite3")
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE external (value TEXT)")
        database.chmod(0o600)
        with pytest.raises(RecoveryError, match="RECOVERY_REPLACED"):
            store.query("SELECT * FROM settings")


def test_existing_unknown_database_is_not_initialized_or_altered(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    database = root / "recovery.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE user_data(value TEXT)")
        db.execute("INSERT INTO user_data VALUES('preserve')")
    database.chmod(0o600)
    original = database.read_bytes()
    with pytest.raises(RecoveryError, match="RECOVERY_SCHEMA_INVALID"):
        RecoveryStore(root)
    assert database.read_bytes() == original


def test_replaced_lease_is_not_accepted(tmp_path):
    with RecoveryStore(tmp_path / "state") as store:
        lease = store.root / "recovery.lock"
        lease.rename(store.root / "old-lease")
        lease.touch(mode=0o600)
        with pytest.raises(RecoveryError, match="RECOVERY_LEASE_REPLACED"):
            store.usage()


def test_resumed_retirement_does_not_delete_changed_content(tmp_path):
    with RecoveryStore(tmp_path / "state") as store:
        sha = store.put_blob(b"retired", "task")
        store.release_owner("task")
        with store.transaction() as db:
            db.execute("UPDATE objects SET state='retiring' WHERE sha256=?", (sha,))
        (store.root / "objects" / sha).write_bytes(b"later-modified")
        with pytest.raises(RecoveryError, match="RECOVERY_OBJECT_CHANGED"):
            store.collect_unreferenced()
        assert (store.root / "objects" / sha).read_bytes() == b"later-modified"

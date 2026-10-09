"""Bind recovery bodies across boots without rebinding source write receipts."""

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path

from code_context.local_control import private_directory
from code_context.policy import MAX_FILE_BYTES
from code_context.scanner import _identity, _version


def stable_object_identity(directory, info):
    return directory.stable_id, info.st_ino, info.st_mode


def _backup(store, backup_dir):
    """Preserve the original database outside the recovery directory whitelist."""
    from code_context.recovery_store import RecoveryError
    from code_context.write_move import _rename_no_replace

    location = Path(os.path.abspath(Path(backup_dir).expanduser()))
    if location == store.root or location.is_relative_to(store.root):
        raise RecoveryError("RECOVERY_BACKUP_UNSAFE: choose separate private storage")
    backup = private_directory(location)
    with store.directory.root_fd() as parent, backup.root_fd() as target:
        fd = os.open("recovery.sqlite3", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as original:
            info = os.fstat(original.fileno())
            store._private(info)
            if info.st_size > store.max_metadata_bytes:
                raise RecoveryError("RECOVERY_BACKUP_LIMIT: preserve original database")
            raw = original.read(store.max_metadata_bytes + 1)
            if _version(info) != _version(os.fstat(original.fileno())) or _version(
                info
            ) != _version(os.stat("recovery.sqlite3", dir_fd=parent, follow_symlinks=False)):
                raise RecoveryError("RECOVERY_BACKUP_CHANGED: preserve original database")
        name = "recovery-pre-volume-" + hashlib.sha256(raw).hexdigest() + ".sqlite3"
        temporary = ".backup-" + secrets.token_hex(16)
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target
        )
        with os.fdopen(fd, "wb") as destination:
            destination.write(raw)
            destination.flush()
            os.fsync(destination.fileno())
        # A unique publication avoids adopting or overwriting an existing backup.
        name = name.removesuffix(".sqlite3") + "-" + secrets.token_hex(6) + ".sqlite3"
        _rename_no_replace(target, temporary, target, name)
        # Keep raw SQLite auxiliaries as well as an independently restorable
        # committed snapshot. Main-file bytes alone are insufficient for WAL.
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                fd = os.open(
                    "recovery.sqlite3" + suffix,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent,
                )
            except FileNotFoundError:
                continue
            with os.fdopen(fd, "rb") as original:
                info = os.fstat(original.fileno())
                store._private(info)
                if info.st_size > 3 * store.max_metadata_bytes:
                    raise RecoveryError("RECOVERY_BACKUP_LIMIT: preserve original database")
                raw_aux = original.read(3 * store.max_metadata_bytes + 1)
                fd = os.open(
                    name + suffix,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=target,
                )
                with os.fdopen(fd, "wb") as destination:
                    destination.write(raw_aux)
                    destination.flush()
                    os.fsync(destination.fileno())
                if _version(info) != _version(os.fstat(original.fileno())) or _version(
                    info
                ) != _version(
                    os.stat("recovery.sqlite3" + suffix, dir_fd=parent, follow_symlinks=False)
                ):
                    raise RecoveryError("RECOVERY_BACKUP_CHANGED: preserve original database")
        snapshot = name.removesuffix(".sqlite3") + "-snapshot.sqlite3"
        fd = os.open(
            snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target
        )
        os.close(fd)
        with sqlite3.connect(backup.root / snapshot) as copy:
            store.db.backup(copy)
            if copy.execute("PRAGMA integrity_check(1)").fetchone()[0] != "ok":
                raise RecoveryError("RECOVERY_BACKUP_CHANGED: preserve original database")
        backup.ensure_available()
        fd = os.open(snapshot, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=target)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(target)
        return str(backup.root / name)


def refresh_object_identities(store, *, legacy_device=None, backup_dir=None):
    """Only startup can translate a proved stable binding into this boot's dev.

    An explicit local repair can prove a legacy body from its complete raw
    identity plus size/SHA. Pending source receipts and task states are untouched.
    """
    from code_context.recovery_store import OBJECT_NAME, RecoveryError, encode_metadata

    if (legacy_device is None) != (backup_dir is None) or (
        legacy_device is not None and (type(legacy_device) is not int or legacy_device <= 0)
    ):
        raise RecoveryError("INVALID_RECOVERY_REPAIR: device and private backup required")
    rows = [dict(row) for row in store.db.execute("SELECT * FROM objects")]
    if len(rows) > 4096:
        raise RecoveryError("RECOVERY_OBJECT_LIMIT: storage object limit reached")
    exists = store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='object_identities'"
    ).fetchone()
    bindings = dict(store.db.execute("SELECT * FROM object_identities")) if exists else {}
    changes, observed = [], []
    with store.objects.root_fd() as parent:
        entries = os.listdir(parent)
        if len(entries) > 4096 or set(entries) - {row["sha256"] for row in rows}:
            raise RecoveryError("UNKNOWN_RECOVERY_OBJECT: preserve unregistered files")
        for row in rows:
            sha = row["sha256"]
            if not isinstance(sha, str) or OBJECT_NAME.fullmatch(sha) is None:
                raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve recovery materials")
            try:
                fd = os.open(sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                if row["state"] in {"pending", "retiring"}:
                    continue
                raise RecoveryError("RECOVERY_OBJECT_UNAVAILABLE: preserve materials") from None
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                store._private(info)
                current = _identity(info)
                stable = stable_object_identity(store.objects, info)
                try:
                    old = json.loads(row["identity"]) if row["identity"] else None
                    previous = json.loads(bindings[sha]) if sha in bindings else None
                except (ValueError, TypeError):
                    raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials") from None
                if old is None and row["state"] in {"pending", "retiring"}:
                    # Preserve the pre-existing crash window before identity
                    # registration. It remains unreadable and unreclaimable.
                    continue
                if (
                    not isinstance(old, list)
                    or len(old) != 3
                    or any(type(value) is not int or value <= 0 for value in old)
                    or old[1:] != list(current[1:])
                    or (previous is not None and previous != list(stable))
                ):
                    raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
                drifting = old != list(current)
                if drifting and previous is None and old != [legacy_device, *current[1:]]:
                    raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
                if drifting and (row["state"] == "ready" or previous is None):
                    if info.st_size > MAX_FILE_BYTES or info.st_size != row["size"]:
                        raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
                    raw = stream.read(MAX_FILE_BYTES + 1)
                    if len(raw) != row["size"] or hashlib.sha256(raw).hexdigest() != sha:
                        raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
                if _version(info) != _version(os.fstat(stream.fileno())) or _version(
                    info
                ) != _version(os.stat(sha, dir_fd=parent, follow_symlinks=False)):
                    raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
                observed.append((sha, _version(info)))
                if drifting or previous is None:
                    changes.append((encode_metadata(current), sha, encode_metadata(stable)))
        store._check()
        if not changes and backup_dir is None:
            return {"objects": len(rows), "updated_bindings": 0, "backup": None}
        backup = _backup(store, backup_dir) if backup_dir is not None else None
        store.db.execute("BEGIN IMMEDIATE")
        try:
            if [dict(row) for row in store.db.execute("SELECT * FROM objects")] != rows:
                raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS object_identities ("
                "sha256 TEXT PRIMARY KEY REFERENCES objects(sha256) ON DELETE CASCADE, "
                "stable_identity TEXT NOT NULL)"
            )
            for identity, sha, stable in changes:
                store.db.execute("UPDATE objects SET identity=? WHERE sha256=?", (identity, sha))
                store.db.execute(
                    "INSERT OR REPLACE INTO object_identities VALUES(?,?)", (sha, stable)
                )
            store._check()
            for sha, expected in observed:
                if _version(os.stat(sha, dir_fd=parent, follow_symlinks=False)) != expected:
                    raise RecoveryError("RECOVERY_OBJECT_CHANGED: preserve materials")
            # Apply the existing storage budget before committing the additive
            # bindings, so a refused upgrade does not leave a changed database.
            store.reserve()
            store.db.execute("COMMIT")
        except BaseException:
            if store.db.in_transaction:
                store.db.execute("ROLLBACK")
            raise
    return {"objects": len(rows), "updated_bindings": len(changes), "backup": backup}


def upgrade_recovery_identity(root, legacy_device, backup_dir):
    from code_context.recovery_store import RecoveryStore

    with RecoveryStore(root, legacy_device=legacy_device, identity_backup_dir=backup_dir) as store:
        return store.identity_upgrade

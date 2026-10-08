"""Local storage relocation. Copy and verify state; never move source projects.

The original state is retained even after success. A failed copy is retained at
the destination for inspection and is never selected by the native application.
This module is a local maintenance entry point, not an MCP tool.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import sqlite3
import stat
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from code_context.scanner import _identity, _version

LOCKS = {"connection.lock", "runtime.lock", "local.lock", "recovery.lock", "execution.lock"}
ENTRIES = (".code-context", ".env.local")
MAX_ENTRIES = 200_000


class StorageLocationError(Exception):
    """Safe errors containing neither configuration bodies nor credentials."""


def _sqlite_auxiliary(name):
    return name.endswith((".sqlite3-wal", ".sqlite3-shm"))


@contextmanager
def _directory(path: Path, *, owned=True, mutable_mode=False):
    """Pin every ancestor without following symbolic links."""
    path = Path(os.path.abspath(path.expanduser()))
    descriptors, links = [], []
    try:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for name in path.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(child)
            links.append((fd, name, _identity(os.fstat(child))))
            fd = child
        if owned and os.fstat(fd).st_uid != os.getuid():
            raise StorageLocationError("STORAGE_DIRECTORY_UNSAFE: choose an owned real directory")
        yield fd
        for parent, name, expected in links:
            actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
            identity = _identity(actual)
            if mutable_mode and (parent, name, expected) == links[-1]:
                changed = (
                    identity[:2] != expected[:2]
                    or not stat.S_ISDIR(actual.st_mode)
                    or actual.st_uid != os.getuid()
                    or actual.st_mode & 0o077
                )
            else:
                changed = identity != expected
            if changed:
                raise StorageLocationError("STORAGE_DIRECTORY_CHANGED: original storage retained")
    except OSError:
        raise StorageLocationError(
            "STORAGE_DIRECTORY_UNSAFE: real directories are required"
        ) from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _private_file(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise StorageLocationError("STORAGE_FILE_UNSAFE: regular owned files are required")


def _tree(parent, prefix, entries):
    for name in sorted(os.listdir(parent)):
        relative = prefix / name
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if _sqlite_auxiliary(name):
                # Closing the last SQLite reader may retire WAL bookkeeping.
                continue
            raise
        if info.st_uid != os.getuid():
            raise StorageLocationError("STORAGE_FILE_UNSAFE: only owned state can be copied")
        if stat.S_ISDIR(info.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if _identity(os.fstat(fd)) != _identity(info):
                    raise StorageLocationError(
                        "STORAGE_CHANGED: retry after stopping the connection"
                    )
                if os.fstat(fd).st_dev != os.fstat(parent).st_dev:
                    raise StorageLocationError(
                        "STORAGE_MOUNT_ACTIVE: stop execution before changing storage"
                    )
                entries[relative] = ("directory", _identity(info))
                _tree(fd, relative, entries)
            finally:
                os.close(fd)
        elif stat.S_ISREG(info.st_mode):
            _private_file(info)
            if not _sqlite_auxiliary(name):
                entries[relative] = ("file", _version(info))
        elif stat.S_ISSOCK(info.st_mode) and ".code-context/controls/" in relative.as_posix():
            # A disconnected local socket is recreated by the new runtime.
            continue
        else:
            raise StorageLocationError(
                "STORAGE_FILE_UNSAFE: links or special state must be reviewed"
            )
        if len(entries) > MAX_ENTRIES:
            raise StorageLocationError(
                "STORAGE_ENTRY_LIMIT: review oversized state before relocation"
            )


def _inventory(root):
    entries = {}
    with _directory(root) as parent:
        for name in ENTRIES:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if name == ".code-context" and stat.S_ISDIR(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    entries[Path(name)] = ("directory", _identity(info))
                    _tree(fd, Path(name), entries)
                finally:
                    os.close(fd)
            elif name == ".env.local":
                _private_file(info)
                if info.st_mode & 0o077 or info.st_size > 65536:
                    raise StorageLocationError(
                        "STORAGE_CREDENTIAL_UNSAFE: check private credential permissions"
                    )
                entries[Path(name)] = ("file", _version(info))
            else:
                raise StorageLocationError("STORAGE_FILE_UNSAFE: state must use real directories")
    return entries


@contextmanager
def _leases(root, entries):
    with ExitStack() as stack:
        for relative, (kind, expected) in entries.items():
            if kind != "file" or relative.name not in LOCKS:
                continue
            parent = stack.enter_context(_directory(root / relative.parent))
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            if _version(os.fstat(fd)) != expected:
                raise StorageLocationError("STORAGE_CHANGED: retry after stopping the connection")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StorageLocationError(
                    "STORAGE_CONNECTION_ACTIVE: stop the connection before changing storage"
                ) from None
        yield


def _check_database(db, relative):
    if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise StorageLocationError("STORAGE_DATABASE_INVALID: preserve original state")
    if relative.name == "recovery.sqlite3":
        pending = db.execute(
            "SELECT 1 FROM tasks WHERE state NOT IN ('completed','rolled_back') LIMIT 1"
        ).fetchone()
        pending = (
            pending
            or db.execute(
                "SELECT 1 FROM operations "
                "WHERE state IN ('prepared','committing','rollback_prepared') LIMIT 1"
            ).fetchone()
        )
        pending = (
            pending or db.execute("SELECT 1 FROM objects WHERE state!='ready' LIMIT 1").fetchone()
        )
        if pending:
            raise StorageLocationError("STORAGE_RECOVERY_REQUIRED: finish interrupted writes first")
    if (
        relative.name == "execution.sqlite3"
        and db.execute(
            "SELECT 1 FROM jobs WHERE state NOT IN "
            "('exited','timeout','cancelled','interrupted','resource_limit','failed') LIMIT 1"
        ).fetchone()
    ):
        raise StorageLocationError("STORAGE_EXECUTION_PENDING: stop or recover execution first")


@contextmanager
def _database_snapshots(root, entries):
    """Hold real read transactions through validation and SQLite backup.

    Do not use immutable mode: committed pages may exist only in the WAL. WAL
    and SHM files are SQLite-owned bookkeeping and may vanish after db.close().
    """
    databases = {}
    try:
        with ExitStack() as stack:
            for relative, (kind, expected) in entries.items():
                if kind != "file" or relative.suffix != ".sqlite3":
                    continue
                parent = stack.enter_context(_directory(root / relative.parent))
                fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                stack.callback(os.close, fd)
                if _version(os.fstat(fd)) != expected:
                    raise StorageLocationError("STORAGE_CHANGED: original state retained")
                db = stack.enter_context(
                    closing(sqlite3.connect((root / relative).as_uri() + "?mode=ro", uri=True))
                )
                db.execute("BEGIN")
                _check_database(db, relative)
                databases[relative] = db
            yield databases
            # Verify while the snapshot connections are still open. Their
            # eventual close may checkpoint or retire source WAL sidecars.
            for relative in databases:
                if _version((root / relative).lstat()) != entries[relative][1]:
                    raise StorageLocationError("STORAGE_CHANGED: original state retained")
    except sqlite3.Error:
        raise StorageLocationError("STORAGE_DATABASE_INVALID: preserve original state") from None


def _check_databases(root, entries):
    with _database_snapshots(root, entries):
        pass


def inspect_storage(root: Path) -> dict:
    root = Path(os.path.abspath(root.expanduser()))
    entries = _inventory(root)
    with _leases(root, entries):
        _check_databases(root, entries)
    return {
        "ready": True,
        "files": sum(kind == "file" for kind, _ in entries.values()),
        "bytes": sum(value[3] for kind, value in entries.values() if kind == "file"),
    }


def _copy_file(source, target, relative, expected):
    with _directory(source / relative.parent) as old, _directory(target / relative.parent) as new:
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=old)
        with os.fdopen(fd, "rb") as stream:
            if _version(os.fstat(stream.fileno())) != expected:
                raise StorageLocationError("STORAGE_CHANGED: original state retained")
            copied = os.open(
                relative.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=new
            )
            with os.fdopen(copied, "w+b") as destination:
                digest = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
                destination.seek(0)
                if hashlib.file_digest(destination, "sha256").hexdigest() != digest.hexdigest():
                    raise StorageLocationError(
                        "STORAGE_COPY_VERIFY_FAILED: original state retained"
                    )
            if _version(os.fstat(stream.fileno())) != expected:
                raise StorageLocationError("STORAGE_CHANGED: original state retained")
        os.fsync(new)


def _database_digest(db):
    digest = hashlib.sha256()
    for statement in db.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.digest()


def _copy_database(source, target, relative, expected, snapshot):
    """Copy the committed snapshot, including committed pages still in WAL."""
    with _directory(source / relative.parent) as old, _directory(target / relative.parent) as new:
        if _version(os.stat(relative.name, dir_fd=old, follow_symlinks=False)) != expected:
            raise StorageLocationError("STORAGE_CHANGED: original state retained")
        fd = os.open(
            relative.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=new,
        )
        os.close(fd)
        with closing(sqlite3.connect((target / relative).as_uri() + "?mode=rw", uri=True)) as copy:
            snapshot.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
            _check_database(copy, relative)
            if _database_digest(snapshot) != _database_digest(copy):
                raise StorageLocationError("STORAGE_COPY_VERIFY_FAILED: original state retained")
        if _version(os.stat(relative.name, dir_fd=old, follow_symlinks=False)) != expected:
            raise StorageLocationError("STORAGE_CHANGED: original state retained")
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=new)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(new)


def _json_write(path, value):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _rebind_state(source, target, entries):
    import sys

    from code_context.models import validate_project
    from code_context.tunnel import _TUNNEL_ID, _profile

    baseline_root = None
    for relative, (kind, _) in entries.items():
        if kind != "file":
            continue
        path = target / relative
        if relative.name == "profile.yaml" and relative.parent.name == "tunnel":
            try:
                value = json.loads(path.read_text())
                command = value["mcp"]["commands"][0]
                args = shlex.split(command["command"])
                if (
                    len(args) != 10
                    or not Path(args[0]).is_absolute()
                    or args[1:3] != ["-m", "code_context"]
                    or args[3] not in {"local", "workspace"}
                    or args[4] != "--root"
                    or args[6] != "--project"
                    or args[8] != "--data-dir"
                ):
                    raise ValueError
                original_data = Path(args[9])
                original_root = Path(args[5])
                tunnel_id = value["control_plane"]["tunnel_id"]
                project = validate_project(args[7])
                if (
                    not original_data.is_absolute()
                    or ".." in original_data.parts
                    or not original_data.is_relative_to(source)
                    or not original_root.is_absolute()
                    or ".." in original_root.parts
                    or _TUNNEL_ID.fullmatch(tunnel_id) is None
                ):
                    raise ValueError
                expected = _profile(
                    original_root,
                    project,
                    original_data,
                    tunnel_id,
                    source / relative.parent,
                    args[3],
                )
                expected["mcp"]["commands"][0]["command"] = command["command"]
                if value != expected:
                    raise ValueError
                # Selected project roots and source identities stay at their
                # original paths; only application-owned state is relocated.
                args[0] = str(Path(sys.executable).absolute())
                args[9] = str(target / original_data.relative_to(source))
                command["command"] = shlex.join(args)
                value["health"]["url_file"] = str(path.parent / "health.url")
                _json_write(path, value)
                if relative == Path(".code-context/tunnel/profile.yaml"):
                    baseline_root = args[5]
            except (ValueError, KeyError, IndexError, TypeError):
                raise StorageLocationError(
                    "STORAGE_PROFILE_INVALID: original configuration retained"
                ) from None
        elif relative.name == "recovery.sqlite3":
            with sqlite3.connect(path) as db:
                for sha, size, identity in db.execute(
                    "SELECT sha256,size,identity FROM objects"
                ).fetchall():
                    old = source / relative.parent / "objects" / sha
                    new = path.parent / "objects" / sha
                    if (
                        tuple(json.loads(identity)) != _identity(old.lstat())
                        or new.stat().st_size != size
                    ):
                        raise StorageLocationError(
                            "STORAGE_RECOVERY_CHANGED: original objects retained"
                        )
                    with new.open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                            raise StorageLocationError(
                                "STORAGE_RECOVERY_CHANGED: original objects retained"
                            )
                    db.execute(
                        "UPDATE objects SET identity=? WHERE sha256=?",
                        (json.dumps(_identity(new.stat())), sha),
                    )
        elif relative.name == "cache.json" and (path.parent / "cache.sparsebundle").is_dir():
            from code_context.execution_cache import _persistent_identity

            value = json.loads(path.read_text())
            old = source / relative.parent / "cache.sparsebundle"
            if value.get("image_identity") != _persistent_identity(old.stat()):
                raise StorageLocationError("STORAGE_CACHE_CHANGED: preserve original cache")
            value["image_identity"] = _persistent_identity(
                (path.parent / "cache.sparsebundle").stat()
            )
            _json_write(path, value)
    selection = target / ".code-context/desktop/selection-live.json"
    if baseline_root is not None and not selection.exists():
        selection.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(selection, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"selected_root": baseline_root}, stream)
            stream.flush()
            os.fsync(stream.fileno())


def relocate_storage(source: Path, target: Path) -> dict:
    source = Path(os.path.abspath(source.expanduser()))
    target = Path(os.path.abspath(target.expanduser()))
    if source == target:
        return {**inspect_storage(source), "copied": False, "original_retained": True}
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise StorageLocationError("STORAGE_OVERLAP: choose a separate storage directory")
    entries = _inventory(source)
    with _leases(source, entries), _database_snapshots(source, entries) as databases:
        with _directory(target.parent) as parent:
            try:
                os.mkdir(target.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        with _directory(target, mutable_mode=True) as directory:
            if os.listdir(directory):
                raise StorageLocationError(
                    "STORAGE_TARGET_NOT_EMPTY: choose an empty directory; nothing overwritten"
                )
            os.fchmod(directory, 0o700)
            for relative, (kind, expected) in entries.items():
                if kind == "directory":
                    with _directory(target / relative.parent) as parent:
                        os.mkdir(relative.name, 0o700, dir_fd=parent)
                elif relative in databases:
                    _copy_database(source, target, relative, expected, databases[relative])
                else:
                    _copy_file(source, target, relative, expected)
            if _inventory(source) != entries:
                raise StorageLocationError(
                    "STORAGE_CHANGED: original state retained; new location not selected"
                )
            _rebind_state(source, target, entries)
            _check_databases(target, _inventory(target))
            if _inventory(source) != entries:
                raise StorageLocationError("STORAGE_CHANGED: original state retained")
            os.fsync(directory)
    return {
        "copied": True,
        "original_retained": True,
        "source_paths_preserved": True,
        "files": sum(kind == "file" for kind, _ in entries.values()),
        "bytes": sum(value[3] for kind, value in entries.values() if kind == "file"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Safely copy local CoLink state; retain the original"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        result = (
            inspect_storage(args.source)
            if args.target is None
            else relocate_storage(args.source, args.target)
        )
        print(json.dumps(result))
    except (StorageLocationError, OSError, ValueError, sqlite3.Error):
        # Native UI gets only a stable failure marker; no config or path body.
        print(json.dumps({"error": "STORAGE_RELOCATION_FAILED", "original_retained": True}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

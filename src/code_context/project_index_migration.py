"""Explicit offline migration of owned CLIX caches; never called by live queries.

The old shared database stays in place until every matching project has a
durable current cache. Existing current caches win and are never overwritten.
Unknown/stale project facts remain recoverable with the retired shared cache.
Source directories, registration, credentials and task recovery are untouched.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import stat
import sys
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

import code_context.project_storage as storage
from code_context.live_index import (
    _APPLICATION_ID,
    _SCHEMA,
    _SCHEMA_VERSION,
    _TABLES,
    MAX_LIVE_DATABASE_BYTES,
)
from code_context.models import validate_project
from code_context.source_access import SourceAccess, SourceError

_MAIN = "live-index.sqlite3"
_FILES = (_MAIN, _MAIN + "-journal", _MAIN + "-wal", _MAIN + "-shm", "live-index.lock")
_TABLE_NAMES = ("li_projects", *_TABLES)


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _normal_sql(sql):
    return " ".join(sql.lower().replace("if not exists ", "").split())


def _saved_projects(saved, workspace_id):
    """Validate saved source associations without probing or rebinding sources."""
    if not isinstance(saved, dict) or set(saved) != {
        "schema",
        "workspace",
        "projects",
        "authorized_projects",
    }:
        raise ValueError("invalid registration")
    schema = saved["schema"]
    if type(schema) is not int or schema not in {1, 2, 3}:
        raise ValueError("invalid registration")
    root = saved["workspace"]
    if not isinstance(root, dict) or set(root) != (
        {"path", "source_id", "stable_id"} if schema == 3 else {"path", "source_id"}
    ):
        raise ValueError("invalid workspace association")
    path = Path(storage._text(root["path"], 4096))
    if (
        not path.is_absolute()
        or ".." in path.parts
        or str(path) != root["path"]
        or hashlib.sha256(str(path).encode()).hexdigest() != workspace_id
        or storage._KEY.fullmatch(root["source_id"]) is None
        or schema == 3
        and storage._KEY.fullmatch(root["stable_id"]) is None
    ):
        raise ValueError("invalid workspace association")
    rows = saved["projects"]
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError("invalid projects")
    projects, roots = {}, set()
    for row in rows:
        expected = {
            "project_id",
            "relative_root",
            "source_id",
            "display_name",
            "aliases",
            "enabled",
        }
        if schema >= 2:
            expected.add("name_origin")
        if schema == 3:
            expected.add("stable_id")
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError("invalid project")
        identifier = validate_project(row["project_id"])
        source_id = row["source_id"]
        relative = row["relative_root"]
        if (
            not isinstance(relative, str)
            or len(relative) > 4096
            or relative
            and str(PurePosixPath(relative)) != relative
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or any(ord(c) < 32 or ord(c) == 127 for c in relative)
            or relative in roots
            or storage._KEY.fullmatch(source_id) is None
            or type(row["enabled"]) is not bool
            or not isinstance(row["aliases"], list)
            or any(not isinstance(alias, str) for alias in row["aliases"])
            or schema >= 2
            and row["name_origin"] not in {"auto", "custom", "legacy"}
            or schema == 3
            and row["stable_id"] is not None
            and storage._KEY.fullmatch(row["stable_id"]) is None
        ):
            raise ValueError("invalid project association")
        storage._text(row["display_name"], 128)
        expected_id = (
            "p_"
            + hashlib.sha256(
                json.dumps([root["source_id"], relative, source_id]).encode()
            ).hexdigest()[:32]
        )
        if identifier != expected_id or identifier in projects:
            raise ValueError("invalid project association")
        projects[identifier] = source_id
        roots.add(relative)
    if saved["authorized_projects"] != sorted(row["project_id"] for row in rows if row["enabled"]):
        raise ValueError("invalid authorization metadata")
    return projects


class _OwnedTree:
    """Keep directory FDs pinned and verify their links before publication."""

    def __init__(self, stack, root):
        self.stack, self.root, self.links = stack, root, []

    def directory(self, parent, name, *, create=False):
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        child = storage._directory_at(parent, name)
        self.stack.callback(os.close, child)
        self.links.append((parent, name, child))
        return child

    def lock(self, parent, name):
        handle = storage._locked_at(parent, name)
        self.stack.callback(os.close, handle)
        return handle

    def verify(self):
        for parent, name, child in self.links:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ValueError("persisted directory changed")


def _files(directory, *, strict=False):
    names = (
        os.listdir(directory) if strict else [name for name in _FILES if _exists(directory, name)]
    )
    result = {}
    for name in names:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            name not in _FILES
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
        ):
            raise ValueError("unsafe derived file")
        if name != _MAIN and name != "live-index.lock" and info.st_size:
            # Immutable reads cannot recover a journal/WAL. Never ignore one.
            raise ValueError("unsettled index journal")
        result[name] = _stamp(info)
    return result


def _exists(directory, name):
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


@contextmanager
def _read_database(directory):
    fd = os.open(_MAIN, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_LIVE_DATABASE_BYTES
        ):
            raise ValueError("unsafe derived database")
        # SQLite opens the pinned file descriptor. No source path is followed,
        # and immutable read mode cannot create sidecars or run recovery writes.
        db = sqlite3.connect(f"file:/dev/fd/{fd}?mode=ro&immutable=1", uri=True)
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            yield db
            if _stamp(os.fstat(fd)) != _stamp(info) or _stamp(
                os.stat(_MAIN, dir_fd=directory, follow_symlinks=False)
            ) != _stamp(info):
                raise ValueError("derived database changed")
        finally:
            db.close()
    finally:
        os.close(fd)


def _validate_database(db):
    if (
        db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
        or db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION
        or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
        or db.execute("PRAGMA foreign_key_check").fetchone() is not None
    ):
        raise ValueError("invalid fact cache")
    expected = {}
    for statement in _SCHEMA:
        words = _normal_sql(statement).split()
        expected[words[2]] = _normal_sql(statement)
    present = set()
    for kind, name, sql in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name"):
        if sql is None and kind == "index" and name.startswith("sqlite_autoindex_"):
            continue
        normalized = _normal_sql(sql) if isinstance(sql, str) else ""
        allowed = {expected.get(name)}
        if name in {"ci_relation_source", "ci_relation_target"}:
            allowed.add(expected[name].split(" where ")[0])
        if name not in expected or normalized not in allowed:
            raise ValueError("unfamiliar cache schema")
        present.add(name)
    if set(expected) - present - {"li_binding_cache"}:
        raise ValueError("incomplete cache schema")
    projects = {}
    for project_id, source_id in db.execute("SELECT project_id,source_id FROM li_projects"):
        validate_project(project_id)
        if storage._KEY.fullmatch(source_id) is None:
            raise ValueError("invalid cache source identity")
        projects[project_id] = source_id
    return projects, present


def _rename_exclusive(source_parent, source, target_parent, target):
    """Publish without ever replacing an existing file, even between checks."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function, flag = library.renameatx_np, 4  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function, flag = library.renameat2, 1  # RENAME_NOREPLACE
    else:
        raise SourceError("INDEX_MIGRATION_UNAVAILABLE: exclusive publication is unavailable")
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    if function(source_parent, os.fsencode(source), target_parent, os.fsencode(target), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _same_rows(source, target, table, project_id):
    columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
    selection = "rowid," + ",".join(f'"{column}"' for column in columns)
    statement = f'SELECT {selection} FROM "{table}" WHERE project_id=? ORDER BY rowid'
    left, right = source.execute(statement, (project_id,)), target.execute(statement, (project_id,))
    count = 0
    while True:
        a, b = left.fetchmany(256), right.fetchmany(256)
        if a != b:
            raise ValueError("copied facts differ")
        if not a:
            return count
        count += len(a)


def _copy_project(old, old_schema, project_id, staging, destination):
    temporary = "project-" + uuid.uuid4().hex + ".sqlite3"
    fd = os.open(
        temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging
    )
    try:
        db = sqlite3.connect(f"file:/dev/fd/{fd}?mode=rw", uri=True)
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            # This file is unpublished. OFF avoids sidecars at /dev/fd and a
            # failed build is retained as staging, never opened by live code.
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA page_size=4096")
            db.execute("PRAGMA auto_vacuum=FULL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            db.execute(f"PRAGMA max_page_count={MAX_LIVE_DATABASE_BYTES // 4096}")
            db.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                db.execute(statement)
            for table in _TABLE_NAMES:
                if table not in old_schema:
                    continue
                columns = [row[1] for row in old.execute(f'PRAGMA table_info("{table}")')]
                selected = "rowid," + ",".join(f'"{column}"' for column in columns)
                rows = old.execute(
                    f'SELECT {selected} FROM "{table}" WHERE project_id=? ORDER BY rowid',
                    (project_id,),
                )
                placeholders = ",".join("?" for _ in range(len(columns) + 1))
                statement = f'INSERT INTO "{table}" ({selected}) VALUES ({placeholders})'
                while batch := rows.fetchmany(256):
                    db.executemany(statement, batch)
            db.commit()
            _validate_database(db)
            counts = {
                table: _same_rows(old, db, table, project_id)
                for table in _TABLE_NAMES
                if table in old_schema
            }
        finally:
            db.close()
        os.fsync(fd)
        _rename_exclusive(staging, temporary, destination, _MAIN)
        os.fsync(destination)
        return counts
    finally:
        os.close(fd)


def _retirement_directory(tree, root_fd, workspace_id, *, native):
    if native:
        context = tree.directory(root_fd, ".code-context")
        trash = tree.directory(context, "retired-indexes", create=True)
        path = tree.root / ".code-context/retired-indexes"
    else:
        path = storage._trash_directory()
        trash = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        tree.stack.callback(os.close, trash)
        info = os.fstat(trash)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe trash directory")
    name = "CoLink-legacy-" + workspace_id[:12] + "-" + uuid.uuid4().hex[:12]
    os.mkdir(name, mode=0o700, dir_fd=trash)
    return tree.directory(trash, name), path / name


def migrate_project_indexes(workspace: Path, workspace_id: str, *, stage_for_native_trash=False):
    """Split a saved workspace's old cache, then retire it recoverably offline.

    Refuses active connections, unfamiliar schema/ownership, corrupt caches and
    unresolved sidecars. A failed/partial attempt preserves old data and staged
    files. Repeating after some destinations were published never overwrites
    those databases. It does not grant or change authorization.
    """
    if not isinstance(workspace_id, str) or storage._KEY.fullmatch(workspace_id) is None:
        raise SourceError("INVALID_STORAGE_WORKSPACE: choose a saved workspace")
    try:
        boundary = SourceAccess(workspace.expanduser())
        relative = f".code-context/desktop/workspaces/{workspace_id}/live-v1"
        result = {
            "workspace_id": workspace_id,
            "migrated_projects": [],
            "existing_projects": [],
            "unregistered_projects": 0,
            "stale_source_projects": 0,
            "retired": False,
            "already_current": False,
            "local_only": True,
        }
        with boundary.root_fd() as root_fd, ExitStack() as stack:
            tree = _OwnedTree(stack, boundary.root)
            runtime = root_fd
            for part in PurePosixPath(relative).parts:
                runtime = tree.directory(runtime, part)
            tree.lock(runtime, "runtime.lock")
            saved = storage._registry(boundary, relative + "/registry/projects.json")
            registered = _saved_projects(saved, workspace_id)
            try:
                indexes = tree.directory(runtime, "index")
            except FileNotFoundError:
                result["already_current"] = True
                return result
            tree.lock(indexes, "project-index.lock")
            tree.lock(indexes, "live-index.lock")
            old_files = _files(indexes)
            if _MAIN not in old_files:
                if any(name not in {"live-index.lock"} for name in old_files):
                    raise ValueError("orphan legacy sidecar")
                result["already_current"] = True
                return result
            with _read_database(indexes) as old:
                old_projects, old_schema = _validate_database(old)
                result["legacy_projects"] = len(old_projects)
                matching = {}
                for project_id, source_id in old_projects.items():
                    if project_id not in registered:
                        result["unregistered_projects"] += 1
                    elif registered[project_id] != source_id:
                        result["stale_source_projects"] += 1
                    else:
                        matching[project_id] = source_id
                destinations = {}
                if matching:
                    projects = tree.directory(indexes, "projects", create=True)
                    for project_id, source_id in sorted(matching.items()):
                        key = hashlib.sha256(project_id.encode()).hexdigest()
                        destination = tree.directory(projects, key, create=True)
                        tree.lock(destination, "live-index.lock")
                        files = _files(destination, strict=True)
                        if _MAIN in files:
                            with _read_database(destination) as current:
                                identities, _ = _validate_database(current)
                                if identities != {project_id: source_id}:
                                    raise ValueError("current cache source mismatch")
                            result["existing_projects"].append(project_id)
                        else:
                            destinations[project_id] = destination
                # Only recognized and inactive caches reach this point. The
                # owned staging directory also retains any unpublished build.
                retired, retired_path = _retirement_directory(
                    tree, root_fd, workspace_id, native=stage_for_native_trash
                )
                if os.fstat(retired).st_dev != os.fstat(indexes).st_dev:
                    raise SourceError(
                        "INDEX_TRASH_DIFFERENT_VOLUME: use native Trash on the storage volume"
                    )
                for project_id, destination in destinations.items():
                    tree.verify()
                    counts = _copy_project(old, old_schema, project_id, retired, destination)
                    result["migrated_projects"].append(
                        {
                            "project_id": project_id,
                            "index_directory": str(
                                boundary.root
                                / relative
                                / "index/projects"
                                / hashlib.sha256(project_id.encode()).hexdigest()
                            ),
                            "row_counts": counts,
                        }
                    )
                tree.verify()
                if saved != storage._registry(boundary, relative + "/registry/projects.json"):
                    raise ValueError("registration changed")
                if _files(indexes) != old_files:
                    raise ValueError("legacy cache changed")
            # Recheck every new/current destination after all copies complete.
            # No old file moves until this verifies durable, usable ownership.
            tree.verify()
            for project_id, source_id in matching.items():
                key = hashlib.sha256(project_id.encode()).hexdigest()
                destination = tree.directory(projects, key)
                _files(destination, strict=True)
                with _read_database(destination) as current:
                    identities, _ = _validate_database(current)
                    if identities != {project_id: source_id}:
                        raise ValueError("current cache source mismatch")
            report = {
                **result,
                "retired": True,
                "recoverable": True,
                "native_trash_pending": stage_for_native_trash,
                "trash_path": str(retired_path),
            }
            fd = os.open(
                "migration-report.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=retired,
            )
            with os.fdopen(fd, "w") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            tree.verify()
            if _files(indexes) != old_files or saved != storage._registry(
                boundary, relative + "/registry/projects.json"
            ):
                raise ValueError("legacy metadata changed before retirement")
            moved = []
            try:
                for name in _FILES:
                    if name in old_files:
                        _rename_exclusive(indexes, name, retired, name)
                        moved.append(name)
                os.fsync(retired)
                os.fsync(indexes)
            except BaseException:
                # Best effort restoration; a failed restoration still leaves
                # the only old file safely in the owned retirement directory.
                for name in reversed(moved):
                    try:
                        _rename_exclusive(retired, name, indexes, name)
                    except OSError:
                        pass
                raise
            return report
    except SourceError as error:
        if str(error).startswith(
            ("INDEX_IN_USE:", "INDEX_TRASH_DIFFERENT_VOLUME:", "INDEX_MIGRATION_UNAVAILABLE:")
        ):
            raise
        raise SourceError(
            "INDEX_MIGRATION_REFUSED: cache or saved ownership could not be verified; "
            "old data retained"
        ) from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        raise SourceError(
            "INDEX_MIGRATION_REFUSED: cache or saved ownership could not be verified; "
            "old data retained"
        ) from None

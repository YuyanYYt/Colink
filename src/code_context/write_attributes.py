"""Necessary write attributes; no widening, filesystem paths or raw values in errors."""

import json
import os
import stat

from code_context.file_attributes import (
    FileAttributes,
    capture_directory_attributes,
    capture_file_attributes,
)
from code_context.scanner import _version
from code_context.write_coordinator import WriteError


def recorded_attributes(record):
    try:
        return FileAttributes.from_record(json.loads(record))
    except (ValueError, TypeError, RecursionError):
        raise WriteError("WRITE_ATTRIBUTE_RECORD: preserve invalid recovery attributes") from None


def task_attributes(c, task_id, path, *, origin=False):
    rows = c.store.query(
        "SELECT origin_record,last_record FROM file_attributes WHERE task_id=? AND path=?",
        (task_id, path),
    )
    if not rows or (origin and rows[0]["origin_record"] is None):
        raise WriteError(
            "WRITE_ATTRIBUTE_ORIGIN_UNAVAILABLE: complete recovery attributes required"
        )
    return recorded_attributes(rows[0]["origin_record" if origin else "last_record"])


def directory_attributes(source, path, identity=None, mode=None):
    with source.parent_fd(path, directory=True) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (identity is not None and (before.st_dev, before.st_ino) != tuple(identity)) or (
                mode is not None and stat.S_IMODE(before.st_mode) != mode
            ):
                raise WriteError("WRITE_ATTRIBUTE_CONFLICT: directory identity or mode changed")
            attrs = capture_directory_attributes(fd)
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _version(before) != _version(after) or _version(after) != _version(named):
                raise WriteError("WRITE_ATTRIBUTE_CONFLICT: directory changed during capture")
            return attrs
        finally:
            os.close(fd)


def current_file_attributes(source, path, version):
    """Attribute-only validation of the previously body-verified source version."""
    with source.parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if _version(before) != tuple(version) or before.st_nlink != 1:
                raise WriteError("WRITE_ATTRIBUTE_CONFLICT: source version or links changed")
            attrs = capture_file_attributes(fd)
            after = os.fstat(fd)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _version(before) != _version(after) or _version(after) != _version(named):
                raise WriteError("WRITE_ATTRIBUTE_CONFLICT: source changed during capture")
            return attrs
        finally:
            os.close(fd)


def verify_created_parents(c, task_id, source, path):
    """Do not extend a task into its created parent after external replacement/ACL changes."""
    parts = path.split("/")
    for size in range(1, len(parts)):
        parent_path = "/".join(parts[:size])
        rows = c.store.query(
            "SELECT * FROM files WHERE task_id=? AND path=? AND kind='directory'",
            (task_id, parent_path),
        )
        if rows:
            binding = json.loads(rows[0]["directory_identity"])
            actual = directory_attributes(source, parent_path, binding["identity"], binding["mode"])
            if actual != task_attributes(c, task_id, parent_path):
                raise WriteError("WRITE_ATTRIBUTE_CONFLICT: task-created parent attributes changed")


def verify_task_attributes(c, task, source, rows):
    for row in rows:
        expected = task_attributes(c, task["task_id"], row["path"])
        if row["kind"] == "directory":
            binding = json.loads(row["directory_identity"])
            actual = directory_attributes(source, row["path"], binding["identity"], binding["mode"])
        else:
            actual = current_file_attributes(source, row["path"], json.loads(row["last_version"]))
        if expected != actual:
            raise WriteError("WRITE_ATTRIBUTE_CONFLICT: necessary source attributes changed")

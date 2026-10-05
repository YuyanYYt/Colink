"""Fd-bound, no-replace directory primitives for a caller-owned durable journal.

The caller authorizes and durably records intent, random temporary names and
recovery state before invoking these functions. Callbacks must durably record
creation/movement before returning. This module neither authorizes nor journals,
adopts an existing directory, creates ancestors, recursively deletes, reverses a
move, or automatically cleans failure material. Identities are (dev, inode).

Atomic no-replace moves isolate the actual namespace object, then verify it.
External edits can still race checks: this is not an arbitrary-editor transaction.
Only rmdir is used for removal, so contents appearing even in the final removal
window cannot be recursively lost. Pending material belongs to caller recovery.
"""

import ctypes
import errno
import os
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass

from code_context.file_mutation import FileMutationError
from code_context.scanner import _version
from code_context.source_access import SourceAccess

_TEMP_NAME = re.compile(r"\.colink-write-[a-f0-9]{32}\.tmp")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class DirectoryMutationError(FileMutationError):
    """Content/path-free directory failure, compatible with FileMutationError.

    may_have_committed is true after installation, rollback isolation or removal
    when any later verification, durability, close or source-context check fails.
    Preparation and failures before a namespace mutation leave it false.
    """


@dataclass(frozen=True)
class PreparedDirectory:
    """Registered temporary inode and intended final mode, never an open fd."""

    path: str
    temp_name: str
    parent_identity: tuple[int, int]
    directory_identity: tuple[int, int]
    mode: int
    source_id: str


@dataclass(frozen=True)
class DirectoryReceipt:
    """Final verified installed directory binding, after context exit checks."""

    path: str
    parent_identity: tuple[int, int]
    directory_identity: tuple[int, int]
    mode: int
    source_id: str


def _identity(info):
    return info.st_dev, info.st_ino


def _valid_identity(value):
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and all(
            isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in value
        )
    )


def _valid_mode(mode):
    return isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 0o777


def _valid_temp(name):
    return isinstance(name, str) and _TEMP_NAME.fullmatch(name) is not None


def _validate_prepared(source, prepared):
    if (
        not isinstance(prepared, PreparedDirectory)
        or not isinstance(prepared.path, str)
        or not _valid_temp(prepared.temp_name)
        or not _valid_identity(prepared.parent_identity)
        or not _valid_identity(prepared.directory_identity)
        or not _valid_mode(prepared.mode)
    ):
        raise DirectoryMutationError("INVALID_PREPARED: invalid directory bindings")
    if prepared.source_id != source.source_id:
        raise DirectoryMutationError("SOURCE_REPLACED: directory belongs to another source")


def _stat(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _check_directory(info, identity=None, modes=None):
    if (
        info is None
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) > 0o777
    ):
        raise DirectoryMutationError("UNSAFE_DIRECTORY: expected an owned real directory")
    if identity is not None and _identity(info) != identity:
        raise DirectoryMutationError(
            "DIRECTORY_CHANGED: registered directory identity no longer matches"
        )
    if modes is not None and stat.S_IMODE(info.st_mode) not in modes:
        raise DirectoryMutationError(
            "DIRECTORY_CHANGED: registered directory mode no longer matches"
        )


def _verify_opened(parent, name, fd, identity, modes):
    before = os.fstat(fd)
    _check_directory(before, identity, modes)
    with os.scandir(fd) as entries:
        if next(entries, None) is not None:
            raise DirectoryMutationError("DIRECTORY_NOT_EMPTY: directory contains entries")
    after, named = os.fstat(fd), _stat(parent, name)
    _check_directory(after, identity, modes)
    _check_directory(named, identity, modes)
    if (
        _version(before) != _version(after)
        or _version(after) != _version(named)
        or before.st_uid != after.st_uid
        or after.st_uid != named.st_uid
    ):
        raise DirectoryMutationError(
            "DIRECTORY_CHANGED: directory changed during empty verification"
        )
    return after


def _inspect(parent, name, identity, modes):
    before = _stat(parent, name)
    _check_directory(before, identity, modes)
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        actual = _verify_opened(parent, name, fd, identity, modes)
        if _version(before) != _version(actual):
            raise DirectoryMutationError("DIRECTORY_CHANGED: directory changed before inspection")
        return actual
    finally:
        os.close(fd)


def _check_parent(parent, prepared):
    if _identity(os.fstat(parent)) != prepared.parent_identity:
        raise DirectoryMutationError("PARENT_CHANGED: registered parent no longer matches")


def _rename_excl(parent, old_name, new_name):
    """Use the SDK's five-argument renameatx_np, RENAME_EXCL=4, on macOS.

    Linux uses libc renameat2 with RENAME_NOREPLACE=1. Missing support or an
    unsupported filesystem fails explicitly, never falling back to plain rename.
    """
    native = {"darwin": ("renameatx_np", 0x4), "linux": ("renameat2", 0x1)}.get(sys.platform)
    if native is None:
        raise DirectoryMutationError("UNSUPPORTED_ATOMIC_NOREPLACE: no safe directory move")
    symbol, flag = native
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, symbol)
    except (OSError, AttributeError):
        raise DirectoryMutationError(
            "UNSUPPORTED_ATOMIC_NOREPLACE: no safe directory move"
        ) from None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(parent, os.fsencode(old_name), parent, os.fsencode(new_name), flag) != 0:
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
            raise DirectoryMutationError(
                "UNSUPPORTED_ATOMIC_NOREPLACE: filesystem lacks a safe move"
            )
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise DirectoryMutationError("DESTINATION_EXISTS: no-replace destination is occupied")
        raise DirectoryMutationError("ATOMIC_MOVE_FAILED: directory entries could not be moved")


def _move_observed(parent, old_name, new_name, identity):
    """Classify exceptions at the syscall/return boundary conservatively."""
    try:
        old, new = _stat(parent, old_name), _stat(parent, new_name)
        return (
            old is None
            or _identity(old) != identity
            or (new is not None and _identity(new) == identity)
        )
    except BaseException:
        return True


def _move(parent, old_name, new_name, identity, state):
    try:
        _rename_excl(parent, old_name, new_name)
    except BaseException:
        state[0] = _move_observed(parent, old_name, new_name, identity)
        raise
    state[0] = True


def _raise_failure(exc, changed, code):
    if changed:
        raise DirectoryMutationError(
            "DIRECTORY_PENDING: namespace change requires durable verification or recovery", True
        ) from None
    if isinstance(exc, DirectoryMutationError):
        raise exc from None
    raise DirectoryMutationError(code) from None


def prepare_directory(
    source: SourceAccess,
    path: str,
    mode: int,
    temp_name: str,
    on_created: Callable[[PreparedDirectory], None],
) -> PreparedDirectory:
    """Exclusively mkdir a private 0700 temp, register its inode, then chmod/fsync.

    mkdir is inherently no-replace, including existing symlinks. Only an existing
    source-policy-allowed parent is used. Discovery invokes on_created immediately
    before opening/chmod, so failures leave the registered empty/changed temporary
    entry in place. The callback mode is the intended final 0..0777 mode (no bool
    or special bits), not the initial private mode. Permissions are never widened
    later to make an unreadable directory committable. Creation mode is subject
    to the process umask; the final fchmod is exact.
    """
    try:
        if (
            not isinstance(path, str)
            or not _valid_mode(mode)
            or not _valid_temp(temp_name)
            or not callable(on_created)
        ):
            raise DirectoryMutationError(
                "INVALID_PREPARE: invalid directory preparation parameters"
            )
        with source.parent_fd(path, directory=True) as (parent, _):
            parent_identity = _identity(os.fstat(parent))
            os.mkdir(temp_name, 0o700, dir_fd=parent)
            created = _stat(parent, temp_name)
            _check_directory(created)
            prepared = PreparedDirectory(
                path, temp_name, parent_identity, _identity(created), mode, source.source_id
            )
            try:
                on_created(prepared)
            except BaseException:
                raise DirectoryMutationError(
                    "TEMP_JOURNAL_FAILED: directory creation record was not confirmed"
                ) from None
            fd = os.open(temp_name, _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                _check_directory(os.fstat(fd), prepared.directory_identity)
                os.fchmod(fd, mode)
                os.fsync(fd)
                os.fsync(parent)
                _verify_opened(parent, temp_name, fd, prepared.directory_identity, (mode,))
            finally:
                os.close(fd)
        return prepared
    except BaseException as exc:
        _raise_failure(exc, False, "PREPARE_FAILED: directory preparation could not be verified")


def commit_directory(source: SourceAccess, prepared: PreparedDirectory) -> DirectoryReceipt:
    """Install the same registered empty inode with an atomic no-replace move.

    No existing object, even an identical empty directory, can be adopted. All
    post-move checks (including source/parent context exit) are pending on error.
    A failed move leaves its material in place; no implicit cleanup or reversal.
    """
    changed = [False]
    try:
        _validate_prepared(source, prepared)
        with source.parent_fd(prepared.path, directory=True) as (parent, target):
            _check_parent(parent, prepared)
            _inspect(parent, prepared.temp_name, prepared.directory_identity, (prepared.mode,))
            if _stat(parent, target) is not None:
                raise DirectoryMutationError("DESTINATION_EXISTS: directory target is occupied")
            _move(parent, prepared.temp_name, target, prepared.directory_identity, changed)
            os.fsync(parent)
            _inspect(parent, target, prepared.directory_identity, (prepared.mode,))
            receipt = DirectoryReceipt(
                prepared.path,
                prepared.parent_identity,
                prepared.directory_identity,
                prepared.mode,
                prepared.source_id,
            )
        return receipt
    except BaseException as exc:
        _raise_failure(
            exc, changed[0], "COMMIT_FAILED: directory installation could not be verified"
        )


def _remove_empty_temp(parent, name, identity, modes, state):
    actual = _inspect(parent, name, identity, modes)
    latest = _stat(parent, name)
    _check_directory(latest, identity, modes)
    if _version(latest) != _version(actual):
        raise DirectoryMutationError(
            "DIRECTORY_CHANGED: temporary directory changed before removal"
        )
    try:
        os.rmdir(name, dir_fd=parent)
    except BaseException:
        try:
            remaining = _stat(parent, name)
            state[0] = state[0] or remaining is None or _identity(remaining) != identity
        except BaseException:
            state[0] = True
        raise
    state[0] = True
    os.fsync(parent)


def discard_directory_temp(source: SourceAccess, prepared: PreparedDirectory) -> None:
    """Remove only a registered, empty temporary inode; missing is idempotent.

    Final mode or the initial private 0700 mode is accepted, allowing cleanup of
    a registered pre-chmod failure. Unknown identities, other modes, foreign UIDs,
    symlinks or any contents are preserved. The target is never removed. The
    caller must first durably authorize cleanup, never of pending material.
    """
    changed = [False]
    try:
        _validate_prepared(source, prepared)
        with source.parent_fd(prepared.path, directory=True) as (parent, _):
            _check_parent(parent, prepared)
            if _stat(parent, prepared.temp_name) is not None:
                _remove_empty_temp(
                    parent,
                    prepared.temp_name,
                    prepared.directory_identity,
                    (prepared.mode, 0o700),
                    changed,
                )
    except BaseException as exc:
        _raise_failure(
            exc, changed[0], "DISCARD_FAILED: temporary directory cleanup could not be verified"
        )


def remove_created_directory(
    source: SourceAccess,
    path: str,
    expected_identity: tuple[int, int],
    expected_mode: int,
    temp_name: str,
    on_moved: Callable[[PreparedDirectory], None],
) -> None:
    """Isolate a caller-journaled created directory, record its move, then rmdir.

    The target must currently be empty, owned and match the expected inode/mode.
    An exclusive same-parent rename isolates the actual directory entry. on_moved
    immediately receives its *expected registered binding* at the temporary name,
    before fsync/reverification, not authority to adopt a substituted object.
    Substitution or new contents cause pending and preserve the isolated object.
    This is not recursive removal; a missing target is rejected, so caller recovery
    decides whether a prior move/removal completed instead of guessing or retrying.
    """
    changed = [False]
    try:
        if (
            not isinstance(path, str)
            or not _valid_identity(expected_identity)
            or not _valid_mode(expected_mode)
            or not _valid_temp(temp_name)
            or not callable(on_moved)
        ):
            raise DirectoryMutationError(
                "INVALID_REMOVE: invalid registered directory removal parameters"
            )
        with source.parent_fd(path, directory=True) as (parent, target):
            _inspect(parent, target, expected_identity, (expected_mode,))
            if _stat(parent, temp_name) is not None:
                raise DirectoryMutationError(
                    "DESTINATION_EXISTS: isolation temporary name is occupied"
                )
            prepared = PreparedDirectory(
                path,
                temp_name,
                _identity(os.fstat(parent)),
                expected_identity,
                expected_mode,
                source.source_id,
            )
            _move(parent, target, temp_name, expected_identity, changed)
            try:
                on_moved(prepared)
            except BaseException:
                raise DirectoryMutationError(
                    "TEMP_JOURNAL_FAILED: directory isolation record was not confirmed"
                ) from None
            os.fsync(parent)
            # A pinned old parent is not permission to clean after its live source
            # path was replaced during isolation or the durable callback.
            with source.parent_fd(path, directory=True) as (current_parent, _):
                _check_parent(current_parent, prepared)
                _remove_empty_temp(
                    current_parent, temp_name, expected_identity, (expected_mode,), changed
                )
        return None
    except BaseException as exc:
        _raise_failure(
            exc, changed[0], "REMOVE_FAILED: registered directory removal could not be verified"
        )

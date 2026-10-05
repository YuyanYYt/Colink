"""Narrow, fd-bound file installation primitives for a durable write coordinator.

The caller journals the planned temporary name and recovery copy *before*
``prepare_file`` and persists ``on_created`` before it returns. This module never
backs up, authorizes, journals, retries a commit, reverses a swap, or removes a
temporary file implicitly. Identity tuples are ``(st_dev, st_ino)``; permissions
are checked separately. All failures are content/path-free.

Checks detect external edits before installation and inspect both objects after
installation. They are not a filesystem transaction with arbitrary editors. A
post-install failure leaves recovery material in place and must be durably marked
pending by the caller. Final source metadata must be re-read after link cleanup,
which changes the installed inode's link count and ctime.
"""

import ctypes
import errno
import hashlib
import os
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass

from code_context.policy import MAX_FILE_BYTES, content_problem
from code_context.scanner import _version
from code_context.source_access import SourceAccess, SourceDocument, SourceError

_TEMP_NAME = re.compile(r"\.colink-write-[a-f0-9]{32}\.tmp")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_EXCHANGE = 0x2
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class FileMutationError(SourceError):
    """Safe failure with uncertainty about this attempt's target installation.

    ``may_have_committed`` is false for preparation and pre-install failures, and
    true for every failure after a successful swap/link, including directory or
    source-context exit checks. The caller must not retry or clean pending data.
    """

    def __init__(self, message: str, may_have_committed: bool = False):
        super().__init__(message)
        self.may_have_committed = may_have_committed


@dataclass(frozen=True)
class PreparedFile:
    """Bounded intended data and durable-journal bindings, never an open fd."""

    path: str
    temp_name: str
    parent_identity: tuple[int, int]
    temp_identity: tuple[int, int]
    sha256: str
    size: int
    mode: int
    source_id: str


@dataclass(frozen=True)
class _ReadFile:
    raw: bytes
    sha256: str
    info: os.stat_result


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


def _valid_hash(value):
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _validate_raw(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_FILE_BYTES:
        raise FileMutationError("INVALID_CONTENT: expected bounded UTF-8 bytes")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FileMutationError("INVALID_CONTENT: expected bounded UTF-8 bytes") from None
    if content_problem(content):
        raise FileMutationError("CONTENT_EXCLUDED: content is outside the source text policy")
    return content


def _validate_prepared(source, prepared):
    if (
        not isinstance(prepared, PreparedFile)
        or not isinstance(prepared.path, str)
        or not isinstance(prepared.temp_name, str)
        or _TEMP_NAME.fullmatch(prepared.temp_name) is None
        or not _valid_identity(prepared.parent_identity)
        or not _valid_identity(prepared.temp_identity)
        or not _valid_hash(prepared.sha256)
        or not isinstance(prepared.size, int)
        or isinstance(prepared.size, bool)
        or not 0 <= prepared.size <= MAX_FILE_BYTES
        or not _valid_mode(prepared.mode)
    ):
        raise FileMutationError("INVALID_PREPARED: invalid prepared file metadata")
    if prepared.source_id != source.source_id:
        raise FileMutationError("SOURCE_REPLACED: prepared file belongs to another source")


def _check_parent(parent, prepared):
    if _identity(os.fstat(parent)) != prepared.parent_identity:
        raise FileMutationError("PARENT_CHANGED: prepared parent binding is no longer current")


def _stat(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _check_regular(info, links):
    if (
        info is None
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink not in links
        or stat.S_IMODE(info.st_mode) > 0o777
    ):
        raise FileMutationError("UNSAFE_FILE: expected an owned regular file with allowed links")


def _read_opened(parent, name, fd, *, links=(1,)):
    before = os.fstat(fd)
    _check_regular(before, links)
    if before.st_size > MAX_FILE_BYTES:
        raise FileMutationError("FILE_SIZE_LIMIT: file is outside the bounded read limit")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, remaining = [], MAX_FILE_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    after, named = os.fstat(fd), _stat(parent, name)
    _check_regular(after, links)
    _check_regular(named, links)
    if (
        _version(before) != _version(after)
        or _version(after) != _version(named)
        or (before.st_uid, before.st_nlink) != (after.st_uid, after.st_nlink)
        or (after.st_uid, after.st_nlink) != (named.st_uid, named.st_nlink)
    ):
        raise FileMutationError("FILE_CHANGED: file changed during complete verification")
    raw = b"".join(chunks)
    if len(raw) > MAX_FILE_BYTES or len(raw) != after.st_size:
        raise FileMutationError("FILE_CHANGED: complete bounded verification was not possible")
    return _ReadFile(raw, hashlib.sha256(raw).hexdigest(), after)


def _read_named(parent, name, *, links=(1,)):
    _check_regular(_stat(parent, name), links)
    fd = os.open(name, _READ_FLAGS, dir_fd=parent)
    try:
        return _read_opened(parent, name, fd, links=links)
    finally:
        os.close(fd)


def _check_new(actual, prepared):
    if (
        _identity(actual.info) != prepared.temp_identity
        or actual.sha256 != prepared.sha256
        or len(actual.raw) != prepared.size
        or stat.S_IMODE(actual.info.st_mode) != prepared.mode
    ):
        raise FileMutationError("TEMP_CHANGED: prepared content or identity no longer matches")


def _installation_observed(parent, target, prepared, expected):
    """Conservatively classify an exception at the native-call/return boundary."""
    try:
        temp, installed = _stat(parent, prepared.temp_name), _stat(parent, target)
        if expected is None:
            return (installed is not None and _identity(installed) == prepared.temp_identity) or (
                temp is None or _identity(temp) != prepared.temp_identity or temp.st_nlink != 1
            )
        return (
            temp is None
            or _identity(temp) != prepared.temp_identity
            or installed is None
            or _identity(installed) != expected.version[:2]
        )
    except BaseException:
        return True


def _write_all(fd, raw):
    remaining = memoryview(raw)
    while remaining:
        written = os.write(fd, remaining[: 64 * 1024])
        if written <= 0:
            raise FileMutationError("TEMP_WRITE_FAILED: temporary write made no progress")
        remaining = remaining[written:]


def _exchange(parent, temp_name, target_name):
    """Swap two directory entries atomically; never fall back to replacement.

    macOS SDK sys/stdio.h declares renameatx_np(int, char*, int, char*, uint)
    and RENAME_SWAP = 0x00000002. Linux renameat2 uses RENAME_EXCHANGE = 2.
    """
    symbol = {"darwin": "renameatx_np", "linux": "renameat2"}.get(sys.platform)
    if symbol is None:
        raise FileMutationError("UNSUPPORTED_ATOMIC_SWAP: no safe exchange primitive")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, symbol)
    except (OSError, AttributeError):
        raise FileMutationError("UNSUPPORTED_ATOMIC_SWAP: no safe exchange primitive") from None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(parent, os.fsencode(temp_name), parent, os.fsencode(target_name), _EXCHANGE) != 0:
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
            raise FileMutationError("UNSUPPORTED_ATOMIC_SWAP: filesystem cannot safely exchange")
        raise FileMutationError("ATOMIC_SWAP_FAILED: entries could not be exchanged")


def prepare_file(
    source: SourceAccess,
    path: str,
    raw: bytes,
    mode: int,
    temp_name: str,
    on_created: Callable[[PreparedFile], None],
) -> PreparedFile:
    """Create/register a private temp, then write, chmod, fsync and verify it.

    The target is never opened or modified. A 0600 O_EXCL/O_NOFOLLOW temporary
    entry is created in its pinned, existing parent. ``on_created`` is invoked
    immediately after inode discovery, before chmod/data writes; its metadata
    describes the intended final content/mode. Even an empty/partial temp is left
    in place on failure, for the caller's pre-journaled recovery handling. Modes
    accept exactly 0..0777, not bool or special bits. No permissions are widened
    later to make unreadable prepared files committable.
    """
    try:
        _validate_raw(raw)
        if (
            not isinstance(path, str)
            or not _valid_mode(mode)
            or not isinstance(temp_name, str)
            or _TEMP_NAME.fullmatch(temp_name) is None
            or not callable(on_created)
        ):
            raise FileMutationError("INVALID_PREPARE: invalid temporary file parameters")
        with source.parent_fd(path) as (parent, _):
            parent_identity = _identity(os.fstat(parent))
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(temp_name, flags, 0o600, dir_fd=parent)
            try:
                prepared = PreparedFile(
                    path,
                    temp_name,
                    parent_identity,
                    _identity(os.fstat(fd)),
                    hashlib.sha256(raw).hexdigest(),
                    len(raw),
                    mode,
                    source.source_id,
                )
                try:
                    on_created(prepared)
                except BaseException:
                    raise FileMutationError(
                        "TEMP_JOURNAL_FAILED: creation record was not confirmed"
                    ) from None
                os.fchmod(fd, mode)
                _write_all(fd, raw)
                os.fsync(fd)
                os.fsync(parent)
                _check_new(_read_opened(parent, temp_name, fd), prepared)
            finally:
                os.close(fd)
        return prepared
    except FileMutationError:
        raise
    except BaseException:
        raise FileMutationError(
            "PREPARE_FAILED: temporary file preparation could not be verified"
        ) from None


def commit_file(
    source: SourceAccess, prepared: PreparedFile, expected: SourceDocument | None
) -> SourceDocument:
    """Install without blind overwrites, preserving the temporary entry.

    Existing targets must match the expected SHA and full source version in a
    late actual read, be owned regular single-link files, and are then exchanged.
    The displaced object is re-read and must match the expected hash/dev/inode/
    mode/size; rename-induced ctime changes are deliberately not compared to its
    old version. Any post-swap conflict stays pending, with no automatic reversal.

    New targets use nofollow link-at semantics: an existing entry is never
    overwritten. The temp and target then share one verified inode (nlink == 2).
    The returned SourceDocument precedes explicit discard; re-read it afterwards
    for final ctime. Context-manager exit errors also become pending.
    """
    committed = False
    try:
        _validate_prepared(source, prepared)
        if expected is not None and (
            not isinstance(expected, SourceDocument)
            or expected.path != prepared.path
            or not isinstance(expected.version, tuple)
            or len(expected.version) != 6
        ):
            raise FileMutationError("INVALID_EXPECTED: invalid expected source document")
        with source.parent_fd(prepared.path) as (parent, target):
            _check_parent(parent, prepared)
            _check_new(_read_named(parent, prepared.temp_name), prepared)
            if expected is None:
                if _stat(parent, target) is not None:
                    raise FileMutationError("TARGET_EXISTS: new target must not already exist")
                try:
                    os.link(
                        prepared.temp_name,
                        target,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                except BaseException:
                    committed = _installation_observed(parent, target, prepared, expected)
                    raise
                committed = True
                os.fsync(parent)
                _check_new(_read_named(parent, prepared.temp_name, links=(2,)), prepared)
                installed = _read_named(parent, target, links=(2,))
            else:
                before = _read_named(parent, target)
                if (
                    before.sha256 != expected.sha256
                    or _version(before.info) != expected.version
                    or len(before.raw) != expected.size
                    or stat.S_IMODE(before.info.st_mode) != expected.mode
                ):
                    raise FileMutationError(
                        "SOURCE_CHANGED: target no longer matches the expected document"
                    )
                try:
                    _exchange(parent, prepared.temp_name, target)
                except BaseException:
                    committed = _installation_observed(parent, target, prepared, expected)
                    raise
                committed = True
                os.fsync(parent)
                displaced = _read_named(parent, prepared.temp_name)
                if (
                    displaced.sha256 != expected.sha256
                    or _identity(displaced.info) != expected.version[:2]
                    or stat.S_IMODE(displaced.info.st_mode) != expected.mode
                    or len(displaced.raw) != expected.size
                ):
                    raise FileMutationError(
                        "SOURCE_CHANGED: exchanged target did not match the expected document"
                    )
                installed = _read_named(parent, target)
            _check_new(installed, prepared)
            document = SourceDocument(
                prepared.path,
                _validate_raw(installed.raw),
                installed.sha256,
                len(installed.raw),
                stat.S_IMODE(installed.info.st_mode),
                _version(installed.info),
            )
        return document
    except BaseException as exc:
        if committed:
            raise FileMutationError(
                "MUTATION_PENDING: installation requires durable verification or recovery", True
            ) from None
        if isinstance(exc, FileMutationError):
            raise
        raise FileMutationError(
            "COMMIT_FAILED: installation preconditions could not be verified"
        ) from None


def discard_prepared(
    source: SourceAccess,
    prepared: PreparedFile,
    expected_sha: str,
    expected_identity: tuple[int, int],
) -> None:
    """Unlink only the named, source/parent-bound, fully verified temporary entry.

    The caller supplies journaled hash/dev/inode for the object currently expected
    at the temporary name: prepared bytes before/new-link commit, or the displaced
    original after a successful swap. A missing entry is idempotent. Single-link
    objects require exact identity/hash and stable metadata through the last check.
    Two links are accepted only for this prepared new inode/hash/mode and a target
    sharing that same completely verified object. More links, unknown objects,
    symlinks, ownership changes and edits are rejected. No target is unlinked.
    The caller must never use this cleanup operation for pending recovery data.
    """
    try:
        _validate_prepared(source, prepared)
        if not _valid_hash(expected_sha) or not _valid_identity(expected_identity):
            raise FileMutationError("INVALID_DISCARD: invalid expected temporary metadata")
        with source.parent_fd(prepared.path) as (parent, target):
            _check_parent(parent, prepared)
            if _stat(parent, prepared.temp_name) is not None:
                actual = _read_named(parent, prepared.temp_name, links=(1, 2))
                if actual.sha256 != expected_sha or _identity(actual.info) != expected_identity:
                    raise FileMutationError(
                        "TEMP_CHANGED: temporary object does not match its journaled state"
                    )
                linked = None
                if actual.info.st_nlink == 2:
                    _check_new(actual, prepared)
                    linked = _read_named(parent, target, links=(2,))
                    _check_new(linked, prepared)
                latest = _stat(parent, prepared.temp_name)
                _check_regular(latest, (actual.info.st_nlink,))
                if _version(latest) != _version(actual.info):
                    raise FileMutationError("TEMP_CHANGED: temporary object changed before cleanup")
                if linked is not None:
                    current = _stat(parent, target)
                    _check_regular(current, (2,))
                    if _version(current) != _version(linked.info):
                        raise FileMutationError(
                            "TARGET_CHANGED: installed link changed before cleanup"
                        )
                os.unlink(prepared.temp_name, dir_fd=parent)
                os.fsync(parent)
    except FileMutationError:
        raise
    except BaseException:
        raise FileMutationError("DISCARD_FAILED: temporary cleanup could not be verified") from None

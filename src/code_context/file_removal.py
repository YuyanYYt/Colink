"""Removal of a caller-journaled task-created file, never a general delete tool.

The coordinator authorizes rollback and durably records intent, the planned temp
name and before-blob before invoking this module. on_moved must durably register
the expected receipt before returning. No recovery storage, authorization, MCP
surface, automatic reversal or failure cleanup is provided here.

An EXCL move preserves the actual object for post-move verification, including
an external object substituted in the check-to-move window. Only the registered
single-link inode/hash/mode can then be unlinked, while the target stays absent.
This is not an arbitrary-editor filesystem transaction: native unlink has no
identity/hash compare-and-swap. Last checks and held-fd postconditions detect
observable races; unrelated new targets are never overwritten or removed.
"""

import hashlib
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass

from code_context.directory_mutation import _rename_excl
from code_context.file_attributes import FileAttributes, capture_file_attributes
from code_context.file_mutation import (
    _READ_FLAGS,
    FileMutationError,
    _check_regular,
    _identity,
    _read_named,
    _read_opened,
    _stat,
    _valid_hash,
    _valid_identity,
    _valid_mode,
    _validate_raw,
)
from code_context.policy import MAX_FILE_BYTES
from code_context.scanner import _version
from code_context.source_access import SourceAccess, SourceDocument

_TEMP_NAME = re.compile(r"\.colink-write-[a-f0-9]{32}\.tmp")


class FileRemovalError(FileMutationError):
    """Safe rollback failure; post-isolation errors require durable pending state.

    All errors after a move, and errors resuming a valid RemovedFile receipt, set
    may_have_committed=True. Invalid inputs/bindings and pre-move failures leave
    it false. No exception contains supplied paths, bodies or callback messages.
    """


@dataclass(frozen=True)
class RemovedFile:
    """Expected registered isolated object, not proof that cleanup completed."""

    path: str
    temp_name: str
    parent_identity: tuple[int, int]
    file_identity: tuple[int, int]
    mode: int
    sha256: str
    size: int
    source_id: str
    attribute_sha256: str | None = None


def _valid_temp(name):
    return isinstance(name, str) and _TEMP_NAME.fullmatch(name) is not None


def _valid_size(size):
    return isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= MAX_FILE_BYTES


def _validate_expected(expected):
    if (
        not isinstance(expected, SourceDocument)
        or not isinstance(expected.path, str)
        or not isinstance(expected.content, str)
        or len(expected.content) > MAX_FILE_BYTES
        or not _valid_mode(expected.mode)
        or not _valid_hash(expected.sha256)
        or not _valid_size(expected.size)
        or not isinstance(expected.version, tuple)
        or len(expected.version) != 6
        or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in expected.version
        )
        or not _valid_identity(expected.version[:2])
        or expected.version[2] != stat.S_IFREG | expected.mode
        or expected.version[3] != expected.size
    ):
        raise FileRemovalError("INVALID_EXPECTED: invalid rollback source document")
    try:
        raw = expected.content.encode("utf-8")
        _validate_raw(raw)
    except (UnicodeError, FileMutationError):
        raise FileRemovalError("INVALID_EXPECTED: invalid rollback source document") from None
    if len(raw) != expected.size or hashlib.sha256(raw).hexdigest() != expected.sha256:
        raise FileRemovalError("INVALID_EXPECTED: inconsistent rollback source document")


def _validate_receipt(source, receipt):
    if (
        not isinstance(receipt, RemovedFile)
        or not isinstance(receipt.path, str)
        or not _valid_temp(receipt.temp_name)
        or not _valid_identity(receipt.parent_identity)
        or not _valid_identity(receipt.file_identity)
        or not _valid_mode(receipt.mode)
        or not _valid_hash(receipt.sha256)
        or not _valid_size(receipt.size)
        or (receipt.attribute_sha256 is not None and not _valid_hash(receipt.attribute_sha256))
    ):
        raise FileRemovalError("INVALID_RECEIPT: invalid registered removal bindings")
    if receipt.source_id != source.source_id:
        raise FileRemovalError("SOURCE_REPLACED: removal receipt belongs to another source")


def _check_parent(parent, receipt):
    if _identity(os.fstat(parent)) != receipt.parent_identity:
        raise FileRemovalError("PARENT_CHANGED: registered rollback parent no longer matches")


def _target_absent(parent, target):
    if _stat(parent, target) is not None:
        raise FileRemovalError("TARGET_EXISTS: rollback target is no longer absent")


def _check_isolated(actual, receipt):
    if (
        _identity(actual.info) != receipt.file_identity
        or actual.sha256 != receipt.sha256
        or len(actual.raw) != receipt.size
        or stat.S_IMODE(actual.info.st_mode) != receipt.mode
        or (
            receipt.attribute_sha256 is not None
            and (actual.attributes is None or actual.attributes.sha256 != receipt.attribute_sha256)
        )
    ):
        raise FileRemovalError(
            "ISOLATED_CHANGED: isolated file does not match its registered state"
        )


def _move_observed(parent, target, receipt):
    """Conservatively classify an exception at the native move/return boundary."""
    try:
        original, isolated = _stat(parent, target), _stat(parent, receipt.temp_name)
        return (
            original is None
            or _identity(original) != receipt.file_identity
            or (isolated is not None and _identity(isolated) == receipt.file_identity)
        )
    except BaseException:
        return True


def _unlink_isolated(parent, target, receipt):
    """Verify real complete bytes, check absence/stability, unlink only the temp."""
    _target_absent(parent, target)
    _check_regular(_stat(parent, receipt.temp_name), (1,))
    fd = os.open(receipt.temp_name, _READ_FLAGS, dir_fd=parent)
    try:
        attributes = receipt.attribute_sha256 is not None
        actual = _read_opened(parent, receipt.temp_name, fd, attributes=attributes)
        _check_isolated(actual, receipt)
        latest, opened = _stat(parent, receipt.temp_name), os.fstat(fd)
        _check_regular(latest, (1,))
        _check_regular(opened, (1,))
        if _version(latest) != _version(actual.info) or _version(opened) != _version(actual.info):
            raise FileRemovalError("ISOLATED_CHANGED: isolated file changed before unlink")
        _target_absent(parent, target)
        os.unlink(receipt.temp_name, dir_fd=parent)
        removed = os.fstat(fd)
        if attributes and capture_file_attributes(fd).sha256 != receipt.attribute_sha256:
            raise FileRemovalError("ISOLATED_CHANGED: removed attributes no longer agree")
        if (
            _identity(removed) != receipt.file_identity
            or removed.st_nlink != 0
            or removed.st_size != actual.info.st_size
            or removed.st_mtime_ns != actual.info.st_mtime_ns
            or removed.st_mode != actual.info.st_mode
        ):
            raise FileRemovalError(
                "ISOLATED_CHANGED: unlink outcome did not match the verified object"
            )
        if _stat(parent, receipt.temp_name) is not None:
            raise FileRemovalError("ISOLATED_CHANGED: temporary entry reappeared after unlink")
        _target_absent(parent, target)
        os.fsync(parent)
        _target_absent(parent, target)
    finally:
        os.close(fd)


def _raise_failure(exc, moved, default):
    if moved:
        raise FileRemovalError(
            "FILE_REMOVAL_PENDING: rollback isolation requires durable verification or recovery",
            True,
        ) from None
    if isinstance(exc, FileRemovalError):
        raise exc from None
    # Preserve only fixed, safe categories from the already-delivered helpers.
    for code, message in (
        ("UNSUPPORTED_ATOMIC_NOREPLACE", "safe file isolation is unavailable"),
        ("DESTINATION_EXISTS", "isolation temporary name is occupied"),
        ("UNSAFE_FILE", "rollback requires an owned regular single-link file"),
        ("FILE_CHANGED", "rollback file changed during complete verification"),
        ("FILE_SIZE_LIMIT", "rollback file is outside the bounded read limit"),
    ):
        if isinstance(exc, FileMutationError) and str(exc).startswith(code + ":"):
            raise FileRemovalError(f"{code}: {message}") from None
    raise FileRemovalError(default) from None


def remove_created_file(
    source: SourceAccess,
    expected: SourceDocument,
    temp_name: str,
    on_moved: Callable[[RemovedFile], None],
    expected_attributes: FileAttributes | None = None,
) -> RemovedFile:
    """Remove one task-created file after caller authorization and before-blob.

    Actual owned, regular, single-link contents and full source version must match
    expected. macOS RENAME_EXCL=4 / Linux RENAME_NOREPLACE=1 isolate without
    overwriting any object. on_moved immediately receives the *expected* receipt,
    not authority to adopt a substituted inode. After its durable return the
    current source/parent is re-opened, isolated contents are fully verified and
    last stable metadata plus target absence are checked before temp unlink/fsync.

    Any post-move error is pending; no reversal or implicit failure cleanup.
    A missing original is rejected, not guessed to be a completed rollback.
    The returned frozen receipt follows cleanup and source-context exit checks.
    """
    moved = False
    try:
        _validate_expected(expected)
        if not _valid_temp(temp_name) or not callable(on_moved):
            raise FileRemovalError("INVALID_REMOVE: invalid isolation parameters")
        with source.parent_fd(expected.path) as (parent, target):
            actual = _read_named(parent, target, attributes=expected_attributes is not None)
            if (
                actual.sha256 != expected.sha256
                or _version(actual.info) != expected.version
                or len(actual.raw) != expected.size
                or stat.S_IMODE(actual.info.st_mode) != expected.mode
                or (expected_attributes is not None and actual.attributes != expected_attributes)
            ):
                raise FileRemovalError(
                    "SOURCE_CHANGED: rollback file no longer matches expected state"
                )
            if _stat(parent, temp_name) is not None:
                raise FileRemovalError("DESTINATION_EXISTS: isolation temporary name is occupied")
            receipt = RemovedFile(
                expected.path,
                temp_name,
                _identity(os.fstat(parent)),
                expected.version[:2],
                expected.mode,
                expected.sha256,
                expected.size,
                source.source_id,
                expected_attributes.sha256 if expected_attributes is not None else None,
            )
            try:
                _rename_excl(parent, target, temp_name)
            except BaseException:
                moved = _move_observed(parent, target, receipt)
                raise
            moved = True
            try:
                on_moved(receipt)
            except BaseException:
                raise FileRemovalError(
                    "MOVE_JOURNAL_FAILED: file isolation record was not confirmed"
                ) from None
            os.fsync(parent)
            _validate_receipt(source, receipt)
            with source.parent_fd(expected.path) as (current_parent, current_target):
                _check_parent(current_parent, receipt)
                _unlink_isolated(current_parent, current_target, receipt)
            _target_absent(parent, target)
        return receipt
    except BaseException as exc:
        _raise_failure(
            exc, moved, "FILE_REMOVAL_FAILED: rollback file preconditions could not be verified"
        )


def discard_removed_file_temp(source: SourceAccess, receipt: RemovedFile) -> None:
    """Resume caller-approved cleanup of a durably registered isolated file.

    A valid receipt resumes an already-moved operation: subsequent errors are
    conservatively pending. It is not a general delete capability. Source/parent,
    real full hash/dev/inode/mode/size, ownership, nlink=1 and stable last metadata
    are all checked. Missing temp is idempotent only while target is also absent;
    any target entry (including a symlink) rejects success. Unknown or changed
    temporary material is never adopted, overwritten or cleaned automatically.
    """
    resumed = False
    try:
        _validate_receipt(source, receipt)
        resumed = True
        with source.parent_fd(receipt.path) as (parent, target):
            _check_parent(parent, receipt)
            _target_absent(parent, target)
            if _stat(parent, receipt.temp_name) is not None:
                _unlink_isolated(parent, target, receipt)
            else:
                os.fsync(parent)
            _target_absent(parent, target)
        return None
    except BaseException as exc:
        _raise_failure(
            exc, resumed, "FILE_REMOVAL_FAILED: registered removal cleanup could not be verified"
        )

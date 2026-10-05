"""Bounded, fd-only attributes for journaled temporary-file preparation.

The caller owns authorization, nofollow/source/parent/identity bindings and the
durable creation record. This module never opens a pathname, copies file data,
changes an original's attributes, restores timestamps or installs a file.
Mutating APIs require an owned, single-link regular file and a writable fd.
Empty-temp APIs establish private staging before body writes; the prepared API
applies final attributes only after a full identity/size/SHA proof. An fd alone
cannot prove that the caller created it or journaled its inode.

macOS ACLs retain entry order, UUIDs, permissions and inheritance flags. Linux
POSIX ACLs remain their original xattr bytes. Unsupported attributes fail closed;
there is no copyfile, AppleDouble, permission-relaxing or partial-copy fallback.
Darwin supports native APFS/HFS volumes; Linux requires zero inode flags and
fd xattr support. Kernel-managed attributes which cannot be made equal, or modes
preventing complete native verification, are rejected rather than ignored.
"""

import array
import base64
import ctypes
import errno
import hashlib
import json
import os
import stat
import struct
import sys
from dataclasses import dataclass, replace
from functools import lru_cache

from code_context.policy import MAX_FILE_BYTES
from code_context.source_access import SourceError

try:
    import fcntl
except ImportError:  # Windows can import records, but fd operations fail closed.
    fcntl = None

MAX_ATTRIBUTE_BYTES = 128 * 1024
MAX_XATTRS = 64
MAX_XATTR_BYTES = 64 * 1024
MAX_ACL_ENTRIES = 128
MAX_ACL_BYTES = 16 * 1024
MAX_NAME_BYTES = 127
MAX_NAMES_BYTES = MAX_XATTRS * (MAX_NAME_BYTES + 1)
MAX_RECORD_BYTES = 256 * 1024
_ACL_TYPE_EXTENDED = 0x100
_ACL_FLAGS = (1, 1 << 17)
_ENTRY_FLAGS = tuple(1 << bit for bit in range(4, 9))
_PERMISSIONS = 0x3FFE | (1 << 20)
_XATTR_SHOWCOMPRESSION = 0x20
_POSIX_ACLS = {"system.posix_acl_access", "system.posix_acl_default"}
_RECORD_KEYS = {"version", "platform", "uid", "gid", "mode", "flags", "acl", "xattrs", "sha256"}


class FileAttributesError(SourceError):
    """Content/path-free failure; the caller retains any partially changed temp."""


@dataclass(frozen=True)
class ACLEntry:
    """One ordered Darwin ACE; qualifier is the original 16-byte UUID."""

    tag: int
    qualifier: bytes
    permissions: int
    flags: int


@dataclass(frozen=True)
class FileAttributes:
    """Immutable attributes, not inode identity, timestamps or file content.

    ``acl=None`` is no extended Darwin ACL; an ACL tuple preserves order.
    ``xattrs`` is a name-sorted tuple of (UTF-8 name, original bytes).
    Records are versioned, JSON-safe, strictly decoded and digest checked.
    The digest excludes identity/time, so rename-induced ctime is irrelevant.
    """

    platform: str
    uid: int
    gid: int
    mode: int
    flags: int = 0
    acl: tuple[ACLEntry, ...] | None = None
    acl_flags: int = 0
    xattrs: tuple[tuple[str, bytes], ...] = ()

    @property
    def sha256(self) -> str:
        return self.to_record()["sha256"]

    def to_record(self) -> dict:
        """Export all bounded values, never only their hashes."""
        _validate(self)
        body = _record_body(self)
        body["sha256"] = hashlib.sha256(_encoded(body)).hexdigest()
        return body

    @classmethod
    def from_record(cls, record: dict) -> "FileAttributes":
        """Reject unknown keys, false integers, oversized or corrupted records.

        ACL records are typed entries, not opaque input for acl_copy_int (whose
        native API has no buffer length). Cross-platform records may be decoded
        for inspection, but cannot be applied on another platform.
        """
        try:
            _keys(record, _RECORD_KEYS)
            if type(record["version"]) is not int or record["version"] != 1:
                _invalid()
            digest = record["sha256"]
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                _invalid()
            acl, acl_flags = None, 0
            if record["acl"] is not None:
                item = record["acl"]
                _keys(item, {"flags", "entries"})
                if type(item["entries"]) is not list or len(item["entries"]) > MAX_ACL_ENTRIES:
                    _invalid()
                entries = []
                for entry in item["entries"]:
                    _keys(entry, {"tag", "qualifier", "permissions", "flags"})
                    value = entry["qualifier"]
                    if (
                        type(value) is not str
                        or len(value) != 32
                        or any(c not in "0123456789abcdef" for c in value)
                    ):
                        _invalid()
                    entries.append(
                        ACLEntry(
                            entry["tag"], bytes.fromhex(value), entry["permissions"], entry["flags"]
                        )
                    )
                acl, acl_flags = tuple(entries), item["flags"]
            if type(record["xattrs"]) is not list or len(record["xattrs"]) > MAX_XATTRS:
                _invalid()
            xattrs, total = [], 0
            for item in record["xattrs"]:
                _keys(item, {"name", "value"})
                name = _name(item["name"])
                value = item["value"]
                if type(value) is not str or len(value) > ((MAX_XATTR_BYTES + 2) // 3) * 4:
                    _invalid()
                raw = base64.b64decode(value, validate=True)
                if base64.b64encode(raw).decode("ascii") != value:
                    _invalid()
                total += len(name) + 1 + len(raw)
                if total > MAX_ATTRIBUTE_BYTES:
                    raise FileAttributesError(
                        "ATTRIBUTE_BUDGET: attributes exceed the bounded budget"
                    )
                xattrs.append((item["name"], raw))
            result = cls(
                record["platform"],
                record["uid"],
                record["gid"],
                record["mode"],
                record["flags"],
                acl,
                acl_flags,
                tuple(xattrs),
            )
            if result.to_record() != record:
                _invalid()
            return result
        except FileAttributesError:
            raise
        except Exception:
            _invalid()


def _invalid():
    raise FileAttributesError("INVALID_ATTRIBUTES: invalid bounded attribute record") from None


def _keys(value, keys):
    if type(value) is not dict or len(value) != len(keys) or set(value) != keys:
        _invalid()


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _name(name):
    if type(name) is not str or not name or len(name) > MAX_NAME_BYTES or "\0" in name:
        _invalid()
    try:
        raw = name.encode("utf-8")
    except UnicodeError:
        _invalid()
    if len(raw) > MAX_NAME_BYTES:
        _invalid()
    if name == "com.apple.ResourceFork" or name.startswith("com.apple.decmpfs"):
        raise FileAttributesError(
            "UNSUPPORTED_ATTRIBUTES: content-coupled attributes are unsupported"
        )
    return raw


def _posix_entries(value):
    # Linux UAPI posix_acl_xattr: little-endian version 2, then 8-byte entries.
    if len(value) < 4 or (len(value) - 4) % 8 or struct.unpack_from("<I", value)[0] != 2:
        _invalid()
    for tag, perm, identity in struct.iter_unpack("<HHI", value[4:]):
        if tag not in {1, 2, 4, 8, 16, 32} or perm > 7:
            _invalid()
        if (identity == 0xFFFFFFFF) != (tag not in {2, 8}):
            _invalid()
    return (len(value) - 4) // 8


def _validate(attrs):
    if (
        type(attrs) is not FileAttributes
        or type(attrs.platform) is not str
        or attrs.platform not in {"darwin", "linux"}
        or not _integer(attrs.uid, 0xFFFFFFFE)
        or not _integer(attrs.gid, 0xFFFFFFFE)
        or not _integer(attrs.mode, 0o777)
        or type(attrs.flags) is not int
        or attrs.flags != 0
        or not _integer(attrs.acl_flags, sum(_ACL_FLAGS))
        or attrs.acl_flags & ~sum(_ACL_FLAGS)
        or type(attrs.xattrs) is not tuple
        or len(attrs.xattrs) > MAX_XATTRS
    ):
        _invalid()
    total, entries = 0, 0
    if attrs.acl is None:
        if attrs.acl_flags:
            _invalid()
    else:
        if attrs.platform != "darwin" or type(attrs.acl) is not tuple:
            _invalid()
        entries = len(attrs.acl)
        if entries > MAX_ACL_ENTRIES or not entries and not attrs.acl_flags:
            _invalid()
        total += 8 + 32 * entries
        for entry in attrs.acl:
            if (
                type(entry) is not ACLEntry
                or type(entry.tag) is not int
                or entry.tag not in {1, 2}
                or type(entry.qualifier) is not bytes
                or len(entry.qualifier) != 16
                or not _integer(entry.permissions, _PERMISSIONS)
                or entry.permissions & ~_PERMISSIONS
                or not _integer(entry.flags, sum(_ENTRY_FLAGS))
                or entry.flags & ~sum(_ENTRY_FLAGS)
            ):
                _invalid()
    previous, names = None, 0
    for item in attrs.xattrs:
        if type(item) is not tuple or len(item) != 2 or type(item[1]) is not bytes:
            _invalid()
        name, value = item
        encoded = _name(name)
        if previous is not None and name <= previous or len(value) > MAX_XATTR_BYTES:
            _invalid()
        previous = name
        names += len(encoded) + 1
        total += len(encoded) + 1 + len(value)
        if attrs.platform == "linux" and name in _POSIX_ACLS:
            entries += _posix_entries(value)
    if names > MAX_NAMES_BYTES or total > MAX_ATTRIBUTE_BYTES or entries > MAX_ACL_ENTRIES:
        raise FileAttributesError("ATTRIBUTE_BUDGET: attributes exceed the bounded budget")


def _record_body(attrs):
    return {
        "version": 1,
        "platform": attrs.platform,
        "uid": attrs.uid,
        "gid": attrs.gid,
        "mode": attrs.mode,
        "flags": attrs.flags,
        "acl": None
        if attrs.acl is None
        else {
            "flags": attrs.acl_flags,
            "entries": [
                {
                    "tag": e.tag,
                    "qualifier": e.qualifier.hex(),
                    "permissions": e.permissions,
                    "flags": e.flags,
                }
                for e in attrs.acl
            ],
        },
        "xattrs": [
            {"name": name, "value": base64.b64encode(value).decode("ascii")}
            for name, value in attrs.xattrs
        ],
    }


def _encoded(body):
    raw = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_RECORD_BYTES:
        raise FileAttributesError("ATTRIBUTE_BUDGET: encoded attributes exceed the bounded budget")
    return raw


def _platform():
    if sys.platform not in {"darwin", "linux"}:
        raise FileAttributesError("UNSUPPORTED_ATTRIBUTES: platform has no supported fd attributes")
    return sys.platform


def _file_flags(fd, info):
    if _platform() == "darwin":
        return info.st_flags
    # Linux FS_IOC_GETFLAGS = _IOR('f', 1, long); no path or write operation.
    value = array.array("L", [0])
    command = 0x80000000 | (value.itemsize << 16) | (ord("f") << 8) | 1
    fcntl.ioctl(fd, command, value, True)
    return value[0]


class _StatFS(ctypes.Structure):
    # Apple SDK sys/mount.h __DARWIN_STRUCT_STATFS64; no paths are opened.
    _fields_ = [
        ("bsize", ctypes.c_uint32),
        ("iosize", ctypes.c_int32),
        ("counts", ctypes.c_uint64 * 5),
        ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("subtype", ctypes.c_uint32),
        ("name", ctypes.c_char * 16),
        ("mount", ctypes.c_char * 1024),
        ("from_name", ctypes.c_char * 1024),
        ("extended_flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_filesystem(fd):
    info = _StatFS()
    _ok(_native().fstatfs(fd, ctypes.byref(info)))
    if info.name not in {b"apfs", b"hfs"}:
        raise FileAttributesError("UNSUPPORTED_ATTRIBUTES: native APFS/HFS attributes are required")


def _checked_info(fd, *, temp=False, empty=True, directory=False):
    if type(fd) is not int or fd < 0:
        raise FileAttributesError("INVALID_FD: expected a live file descriptor")
    info = os.fstat(fd)
    if (
        not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) > 0o777
        or (not directory and info.st_nlink not in {0, 1, 2})
    ):
        raise FileAttributesError("UNSAFE_ATTRIBUTES: expected an owned regular file")
    if _file_flags(fd, info):
        raise FileAttributesError("UNSUPPORTED_ATTRIBUTES: nonzero file flags are unsupported")
    if _platform() == "darwin":
        _darwin_filesystem(fd)
    if temp and (
        (empty and info.st_size != 0)
        or info.st_nlink != 1
        or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE not in {os.O_WRONLY, os.O_RDWR}
    ):
        raise FileAttributesError("UNSAFE_TEMP: expected a writable owned empty single-link temp")
    return info


def _signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
        getattr(info, "st_flags", 0),
    )


@lru_cache(maxsize=1)
def _native():
    """Apple SDK sys/acl.h and sys/xattr.h prototypes; never copyfile."""
    lib = ctypes.CDLL(None, use_errno=True)
    p, i, z = ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t
    prototypes = {
        "fstatfs": ([i, p], i),
        "flistxattr": ([i, p, z, i], ctypes.c_ssize_t),
        "fgetxattr": ([i, ctypes.c_char_p, p, z, ctypes.c_uint32, i], ctypes.c_ssize_t),
        "fsetxattr": ([i, ctypes.c_char_p, p, z, ctypes.c_uint32, i], i),
        "fremovexattr": ([i, ctypes.c_char_p, i], i),
        "acl_get_fd_np": ([i, i], p),
        "acl_set_fd_np": ([i, p, i], i),
        "acl_init": ([i], p),
        "acl_free": ([p], i),
        "acl_valid": ([p], i),
        "acl_size": ([p], ctypes.c_ssize_t),
        "acl_copy_ext": ([p, p, ctypes.c_ssize_t], ctypes.c_ssize_t),
        "acl_get_entry": ([p, i, ctypes.POINTER(p)], i),
        "acl_create_entry": ([ctypes.POINTER(p), ctypes.POINTER(p)], i),
        "acl_get_tag_type": ([p, ctypes.POINTER(i)], i),
        "acl_set_tag_type": ([p, i], i),
        "acl_get_qualifier": ([p], p),
        "acl_set_qualifier": ([p, p], i),
        "acl_get_permset_mask_np": ([p, ctypes.POINTER(ctypes.c_uint64)], i),
        "acl_set_permset_mask_np": ([p, ctypes.c_uint64], i),
        "acl_get_flagset_np": ([p, ctypes.POINTER(p)], i),
        "acl_get_flag_np": ([p, i], i),
        "acl_add_flag_np": ([p, i], i),
    }
    for name, (args, result) in prototypes.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result
    return lib


def _ok(result):
    if result < 0:
        raise FileAttributesError("NATIVE_ATTRIBUTES_FAILED: native attribute operation failed")
    return result


def _acl_bits(obj, masks):
    flagset, bits = ctypes.c_void_p(), 0
    _ok(_native().acl_get_flagset_np(obj, ctypes.byref(flagset)))
    for mask in masks:
        if _ok(_native().acl_get_flag_np(flagset, mask)):
            bits |= mask
    return bits


def _add_acl_bits(obj, bits, masks):
    flagset = ctypes.c_void_p()
    _ok(_native().acl_get_flagset_np(obj, ctypes.byref(flagset)))
    for mask in masks:
        if bits & mask:
            _ok(_native().acl_add_flag_np(flagset, mask))


def _acl_build(entries, flags):
    lib, acl = _native(), ctypes.c_void_p(_native().acl_init(len(entries)))
    if not acl.value:
        raise FileAttributesError("NATIVE_ATTRIBUTES_FAILED: ACL allocation failed")
    try:
        _add_acl_bits(acl, flags, _ACL_FLAGS)
        for item in entries:
            entry = ctypes.c_void_p()
            _ok(lib.acl_create_entry(ctypes.byref(acl), ctypes.byref(entry)))
            _ok(lib.acl_set_tag_type(entry, item.tag))
            qualifier = ctypes.create_string_buffer(item.qualifier)
            _ok(lib.acl_set_qualifier(entry, qualifier))
            _ok(lib.acl_set_permset_mask_np(entry, item.permissions))
            _add_acl_bits(entry, item.flags, _ENTRY_FLAGS)
        _ok(lib.acl_valid(acl))
        return acl
    except BaseException:
        lib.acl_free(acl)
        raise


def _acl_export(acl):
    size = _ok(_native().acl_size(acl))
    if not 0 < size <= MAX_ACL_BYTES:
        raise FileAttributesError("ATTRIBUTE_BUDGET: ACL exceeds the bounded budget")
    buf = ctypes.create_string_buffer(size)
    if _ok(_native().acl_copy_ext(buf, acl, size)) != size:
        raise FileAttributesError("NATIVE_ATTRIBUTES_FAILED: ACL export was incomplete")
    return buf.raw


def _get_acl(fd):
    lib = _native()
    ctypes.set_errno(0)
    acl = lib.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() in {errno.ENOENT, getattr(errno, "ENOATTR", errno.ENODATA)}:
            return None, 0
        raise FileAttributesError("NATIVE_ATTRIBUTES_FAILED: ACL could not be read")
    try:
        _ok(lib.acl_valid(acl))
        exported, entries = _acl_export(acl), []
        flags = _acl_bits(acl, _ACL_FLAGS)
        for index in range(MAX_ACL_ENTRIES + 1):
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = lib.acl_get_entry(acl, 0 if index == 0 else -1, ctypes.byref(entry))
            if result < 0:
                if ctypes.get_errno() != errno.EINVAL:
                    _ok(result)
                break  # Darwin uses -1/EINVAL for end, not POSIX's return 0.
            if index == MAX_ACL_ENTRIES:
                raise FileAttributesError("ATTRIBUTE_BUDGET: ACL entry budget exceeded")
            tag, permissions = ctypes.c_int(), ctypes.c_uint64()
            _ok(lib.acl_get_tag_type(entry, ctypes.byref(tag)))
            _ok(lib.acl_get_permset_mask_np(entry, ctypes.byref(permissions)))
            qualifier = lib.acl_get_qualifier(entry)
            if not qualifier:
                raise FileAttributesError(
                    "NATIVE_ATTRIBUTES_FAILED: ACL qualifier could not be read"
                )
            try:
                identity = ctypes.string_at(qualifier, 16)
            finally:
                lib.acl_free(qualifier)
            entries.append(
                ACLEntry(tag.value, identity, permissions.value, _acl_bits(entry, _ENTRY_FLAGS))
            )
        rebuilt = _acl_build(entries, flags)
        try:
            if _acl_export(rebuilt) != exported:
                raise FileAttributesError(
                    "UNSUPPORTED_ATTRIBUTES: ACL semantics cannot be represented"
                )
        finally:
            lib.acl_free(rebuilt)
        return (tuple(entries), flags) if entries or flags else (None, 0)
    finally:
        lib.acl_free(acl)


def _set_acl(fd, acl, flags):
    value = _acl_build(acl or (), flags)
    try:
        _ok(_native().acl_set_fd_np(fd, value, _ACL_TYPE_EXTENDED))
    finally:
        _native().acl_free(value)


def _get_xattrs(fd):
    if _platform() == "linux":
        names = os.listxattr(fd)
        if len(names) > MAX_XATTRS:
            raise FileAttributesError("ATTRIBUTE_BUDGET: xattr count budget exceeded")
        values, total = [], 0
        for name in sorted(names):
            encoded = _name(name)
            value = os.getxattr(fd, name)
            total += len(encoded) + 1 + len(value)
            if len(value) > MAX_XATTR_BYTES or total > MAX_ATTRIBUTE_BYTES:
                raise FileAttributesError("ATTRIBUTE_BUDGET: xattr bytes budget exceeded")
            values.append((name, value))
        return tuple(values)
    # XATTR_NODEFAULT is not accepted by public xattr calls on macOS (EINVAL).
    # Require a native APFS/HFS volume above, not an AppleDouble fallback.
    lib, options = _native(), _XATTR_SHOWCOMPRESSION
    size = _ok(lib.flistxattr(fd, None, 0, options))
    if size > MAX_NAMES_BYTES:
        raise FileAttributesError("ATTRIBUTE_BUDGET: xattr name budget exceeded")
    buf = ctypes.create_string_buffer(max(1, size))
    if _ok(lib.flistxattr(fd, buf, size, options)) != size:
        raise FileAttributesError("ATTRIBUTES_CHANGED: xattr names changed during capture")
    names = buf.raw[:size].split(b"\0") if size else [b""]
    if names[-1] or len(names) - 1 > MAX_XATTRS:
        raise FileAttributesError("ATTRIBUTE_BUDGET: invalid or oversized xattr names")
    values, total = [], size
    for encoded in sorted(names[:-1]):
        name = encoded.decode("utf-8")
        _name(name)
        length = _ok(lib.fgetxattr(fd, encoded, None, 0, 0, options))
        total += length
        if length > MAX_XATTR_BYTES or total > MAX_ATTRIBUTE_BYTES:
            raise FileAttributesError("ATTRIBUTE_BUDGET: xattr bytes budget exceeded")
        value = ctypes.create_string_buffer(max(1, length))
        if _ok(lib.fgetxattr(fd, encoded, value, length, 0, options)) != length:
            raise FileAttributesError("ATTRIBUTES_CHANGED: xattr value changed during capture")
        values.append((name, value.raw[:length]))
    return tuple(values)


def _set_xattr(fd, name, value):
    if _platform() == "linux":
        os.setxattr(fd, name, value)
    else:
        buf = ctypes.create_string_buffer(value)
        _ok(_native().fsetxattr(fd, name.encode(), buf, len(value), 0, 0))


def _remove_xattr(fd, name):
    if _platform() == "linux":
        os.removexattr(fd, name)
    else:
        _ok(_native().fremovexattr(fd, name.encode(), 0))


def _capture_once(fd, info):
    platform = _platform()
    acl, flags = _get_acl(fd) if platform == "darwin" else (None, 0)
    attrs = FileAttributes(
        platform,
        info.st_uid,
        info.st_gid,
        stat.S_IMODE(info.st_mode),
        acl=acl,
        acl_flags=flags,
        xattrs=_get_xattrs(fd),
    )
    _validate(attrs)
    return attrs


def _capture(fd, *, directory=False):
    try:
        before = _checked_info(fd, directory=directory)
        first = _capture_once(fd, before)
        middle = _checked_info(fd, directory=directory)
        second = _capture_once(fd, middle)
        after = _checked_info(fd, directory=directory)
        if (
            first != second
            or _signature(before) != _signature(middle)
            or _signature(middle) != _signature(after)
        ):
            raise FileAttributesError("ATTRIBUTES_CHANGED: attributes changed during capture")
        return second
    except FileAttributesError:
        raise
    except Exception:
        raise FileAttributesError(
            "ATTRIBUTES_UNAVAILABLE: complete fd attributes could not be captured"
        ) from None


def capture_file_attributes(fd: int) -> FileAttributes:
    """Read twice with stable fstat; never cache or change source attributes.

    Read-only proof accepts link counts 0/1/2 (held unlinked/new-link fds).
    No data-fork reads. Inaccessible APIs/attributes, unsupported flags, resource
    forks, compression and budget excess are failures, never absent attributes.
    """
    return _capture(fd)


def capture_directory_attributes(fd: int) -> FileAttributes:
    """Read-only S_ISDIR proof with the same UID/GID/ACL/xattr budgets.

    The caller binds the directory's identity and registration; compare this
    immutable record for rollback conflicts. No directory mutator is provided.
    Directory link counts are not file hardlink counts and are not restricted.
    """
    return _capture(fd, directory=True)


def verify_file_attributes(fd: int, attrs: FileAttributes) -> None:
    """Require all current values to equal the expected immutable snapshot."""
    _validate(attrs)
    if capture_file_attributes(fd) != attrs:
        raise FileAttributesError(
            "ATTRIBUTES_CHANGED: current attributes do not match the expected values"
        )


def _apply(fd, attrs, before, *, empty):
    if attrs.platform != _platform() or attrs.uid != before.st_uid:
        raise FileAttributesError("UNSUPPORTED_ATTRIBUTES: platform or owner cannot be changed")
    current = capture_file_attributes(fd)
    if not empty and (
        current.mode != 0o600
        or current.acl is not None
        or current.acl_flags
        or any(name in _POSIX_ACLS for name, _ in current.xattrs)
    ):
        raise FileAttributesError("UNSAFE_TEMP: prepared content must still be in private staging")
    if _signature(before) != _signature(_checked_info(fd, temp=True, empty=empty)):
        raise FileAttributesError(
            "ATTRIBUTES_CHANGED: temporary attributes changed before application"
        )
    if before.st_gid != attrs.gid:
        os.fchown(fd, -1, attrs.gid)
    if attrs.platform == "darwin":
        _set_acl(fd, None, 0)
    else:
        for name, _ in current.xattrs:
            if name in _POSIX_ACLS:
                _remove_xattr(fd, name)
    os.fchmod(fd, 0o600)
    desired, existing = dict(attrs.xattrs), dict(current.xattrs)
    for name, _ in current.xattrs:
        if name not in desired and (attrs.platform != "linux" or name not in _POSIX_ACLS):
            _remove_xattr(fd, name)
    # Apply ordinary metadata before ACLs which may deny metadata access.
    for name, value in attrs.xattrs:
        if name not in _POSIX_ACLS and existing.get(name) != value:
            _set_xattr(fd, name, value)
    if attrs.platform == "darwin":
        _set_acl(fd, attrs.acl, attrs.acl_flags)
    else:
        for name, value in attrs.xattrs:
            if name in _POSIX_ACLS:
                _set_xattr(fd, name, value)
    os.fchmod(fd, attrs.mode)
    os.fsync(fd)
    after = _checked_info(fd, temp=True, empty=empty)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise FileAttributesError(
            "ATTRIBUTES_CHANGED: temporary identity or content metadata changed"
        )
    verify_file_attributes(fd, attrs)


def apply_file_attributes(fd: int, attrs: FileAttributes) -> None:
    """Apply only to the caller's registered empty temp, then fsync and verify.

    Requires a writable owned single-link regular fd; no chmod-to-open fallback.
    Preserve UID, explicitly set the intended GID, replace ACL/xattrs exactly and
    finish at the intended mode. No timestamp restoration or file-body writes.
    Any failure leaves the temp for the caller; there is no automatic undo.
    """
    try:
        _validate(attrs)
        before = _checked_info(fd, temp=True)
        _apply(fd, attrs, before, empty=True)
    except FileAttributesError:
        raise
    except Exception:
        raise FileAttributesError(
            "ATTRIBUTES_APPLY_FAILED: temporary attributes could not be verified"
        ) from None


def _read_prepared(fd, identity, size, sha256):
    before = _checked_info(fd, temp=True, empty=False)
    if (before.st_dev, before.st_ino) != identity or before.st_size != size:
        raise FileAttributesError("PREPARED_CHANGED: temporary identity or size does not match")
    digest, offset = hashlib.sha256(), 0
    while offset < size:
        chunk = os.pread(fd, min(64 * 1024, size - offset), offset)
        if not chunk:
            raise FileAttributesError("PREPARED_CHANGED: temporary full read was incomplete")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(fd, 1, size) or digest.hexdigest() != sha256:
        raise FileAttributesError("PREPARED_CHANGED: temporary full content does not match")
    after = _checked_info(fd, temp=True, empty=False)
    if _signature(before) != _signature(after):
        raise FileAttributesError("PREPARED_CHANGED: temporary changed during full verification")
    return after


def apply_prepared_file_attributes(
    fd: int, attrs: FileAttributes, *, identity: tuple[int, int], size: int, sha256: str
) -> None:
    """Finish the journaled private temp *after* its complete body was written.

    Require private 0600/no-ACL owned single-link writable fd, exact dev/inode, bounded complete
    SHA/size and stable full metadata before any mutation. Verify body again
    after applying attributes, using pread without moving the caller's offset.
    The caller proves source/parent and durable registration; no pathname API or
    timestamp restoration is introduced. Failures retain the temp, never undo it.
    """
    try:
        _validate(attrs)
        if (
            type(identity) is not tuple
            or len(identity) != 2
            or any(type(part) is not int or part < 0 for part in identity)
            or not _integer(size, MAX_FILE_BYTES)
            or type(sha256) is not str
            or len(sha256) != 64
            or any(c not in "0123456789abcdef" for c in sha256)
        ):
            _invalid()
        before = _read_prepared(fd, identity, size, sha256)
        _apply(fd, attrs, before, empty=False)
        _read_prepared(fd, identity, size, sha256)
        verify_file_attributes(fd, attrs)
    except FileAttributesError:
        raise
    except Exception:
        raise FileAttributesError(
            "ATTRIBUTES_APPLY_FAILED: prepared attributes could not be verified"
        ) from None


def ensure_private_staging(fd: int) -> FileAttributes:
    """After inode journaling, save creation attributes and establish private 0600.

    Accept only an empty owned single-link writable temp. Remove Darwin extended
    ACL or Linux POSIX ACL xattrs, not other xattrs; verify private mode and all
    retained values. Return the *pre-change* attributes for later restoration.
    The caller must persist them before writing the body and retain failed temps.
    """
    try:
        _checked_info(fd, temp=True)
        creation = capture_file_attributes(fd)
        private = replace(
            creation,
            mode=0o600,
            acl=None,
            acl_flags=0,
            xattrs=tuple(
                (name, value)
                for name, value in creation.xattrs
                if creation.platform != "linux" or name not in _POSIX_ACLS
            ),
        )
        apply_file_attributes(fd, private)
        return creation
    except FileAttributesError:
        raise
    except Exception:
        raise FileAttributesError(
            "PRIVATE_STAGING_FAILED: private temporary attributes could not be verified"
        ) from None

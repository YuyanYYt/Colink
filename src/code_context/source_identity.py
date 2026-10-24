"""Persistent volume identity, separate from runtime dev/inode race checks."""

import ctypes
import os
import struct
import sys
from functools import lru_cache


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _darwin_getattr():
    # Bind this immutable system entry point once, independently of optional
    # port/process inspection libraries. No directory identities are cached.
    try:
        function = ctypes.CDLL(None, use_errno=True).fgetattrlist
    except (OSError, AttributeError):
        raise OSError("persistent volume identity unavailable") from None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function


def _darwin_volume_uuid(fd):
    # ATTR_VOL_UUID from sys/attr.h. Read from the already pinned directory FD.
    function = _darwin_getattr()
    attributes = _AttrList(5, 0, 0, 0x00040000, 0, 0, 0)
    output = ctypes.create_string_buffer(20)
    if function(fd, ctypes.byref(attributes), output, len(output), 0) != 0:
        raise OSError("persistent volume identity unavailable")
    if struct.unpack("=I", output.raw[:4])[0] != 20 or output.raw[4:] == bytes(16):
        raise OSError("persistent volume identity unavailable")
    return "uuid:" + output.raw[4:].hex()


def volume_identity(fd):
    if sys.platform == "darwin":
        return _darwin_volume_uuid(fd)
    identity = os.fstatvfs(fd).f_fsid
    if not identity:
        raise OSError("persistent volume identity unavailable")
    return "fsid:" + str(identity)


def _volume_for_directory(device, path, inode, mode):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_mode) != (device, inode, mode):
            raise OSError("source directory changed during identity capture")
        return volume_identity(fd)
    finally:
        os.close(fd)


def stable_source_identities(root, root_fd, root_identity, ancestors):
    volume = volume_identity(root_fd)
    result = []
    for path, (device, inode, mode) in zip(reversed(root.parents), ancestors, strict=True):
        # Darwin firmlinks can expose equal st_dev values on different APFS
        # volumes (notably / versus /Users). Query each pinned ancestor itself.
        ancestor_volume = _volume_for_directory(device, str(path), inode, mode)
        result.append((ancestor_volume, inode, mode))
    return (volume, *root_identity[1:]), tuple(result)

"""Save generated database credentials with the native macOS Keychain API."""

import ctypes
import re
import sys

from code_context.source_access import SourceError

SERVICE = "local.colink.database"


def _store(reference: str, secret: bytes) -> int:
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    pointer = ctypes.c_void_p
    core.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
    core.CFStringCreateWithCString.restype = pointer
    core.CFDataCreate.argtypes = [pointer, pointer, ctypes.c_long]
    core.CFDataCreate.restype = pointer
    core.CFDictionaryCreate.argtypes = [
        pointer,
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.c_long,
        pointer,
        pointer,
    ]
    core.CFDictionaryCreate.restype = pointer
    core.CFRelease.argtypes = [pointer]
    core.CFRelease.restype = None
    security.SecItemAdd.argtypes = [pointer, ctypes.POINTER(pointer)]
    security.SecItemAdd.restype = ctypes.c_int32

    def constant(name):
        return pointer.in_dll(security, name).value

    owned = []
    try:
        service = core.CFStringCreateWithCString(None, SERVICE.encode(), 0x08000100)
        account = core.CFStringCreateWithCString(None, reference.encode(), 0x08000100)
        owned.extend([service, account])
        buffer = ctypes.create_string_buffer(secret)
        data = core.CFDataCreate(None, ctypes.cast(buffer, pointer), len(secret))
        owned.append(data)
        if not all(owned):
            raise SourceError("DATABASE_CREDENTIAL_STORE_FAILED")
        keys = (pointer * 4)(
            constant("kSecClass"),
            constant("kSecAttrService"),
            constant("kSecAttrAccount"),
            constant("kSecValueData"),
        )
        values = (pointer * 4)(constant("kSecClassGenericPassword"), service, account, data)
        query = core.CFDictionaryCreate(None, keys, values, 4, None, None)
        owned.append(query)
        if not query:
            raise SourceError("DATABASE_CREDENTIAL_STORE_FAILED")
        return int(security.SecItemAdd(query, None))
    finally:
        for item in reversed(owned):
            if item:
                core.CFRelease(item)


def store_secret(reference: str, password: str) -> None:
    """Add a fresh credential; never replace an existing credential on a retry."""
    if (
        not isinstance(reference, str)
        or re.fullmatch(r"colink-db-[a-f0-9]{32}", reference) is None
        or not isinstance(password, str)
        or len(password) > 4096
        or "\x00" in password
    ):
        raise SourceError("INVALID_DATABASE_CREDENTIAL")
    if sys.platform != "darwin":
        raise SourceError("DATABASE_KEYCHAIN_UNAVAILABLE")
    try:
        encoded = password.encode("utf-8")
        if len(encoded) > 4096:
            raise SourceError("INVALID_DATABASE_CREDENTIAL")
        result = _store(reference, encoded)
    except SourceError:
        raise
    except Exception:
        raise SourceError("DATABASE_CREDENTIAL_STORE_FAILED") from None
    if result:
        raise SourceError("DATABASE_CREDENTIAL_STORE_FAILED")


def _read(reference: str) -> bytes:
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    pointer = ctypes.c_void_p
    core.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
    core.CFStringCreateWithCString.restype = pointer
    core.CFDictionaryCreate.argtypes = [
        pointer,
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.c_long,
        pointer,
        pointer,
    ]
    core.CFDictionaryCreate.restype = pointer
    core.CFDataGetLength.argtypes = [pointer]
    core.CFDataGetLength.restype = ctypes.c_long
    core.CFDataGetBytePtr.argtypes = [pointer]
    core.CFDataGetBytePtr.restype = pointer
    core.CFRelease.argtypes = [pointer]
    core.CFRelease.restype = None
    security.SecItemCopyMatching.argtypes = [pointer, ctypes.POINTER(pointer)]
    security.SecItemCopyMatching.restype = ctypes.c_int32

    def constant(name):
        return pointer.in_dll(security, name).value

    owned = []
    try:
        service = core.CFStringCreateWithCString(None, SERVICE.encode(), 0x08000100)
        account = core.CFStringCreateWithCString(None, reference.encode(), 0x08000100)
        owned.extend([service, account])
        if not all(owned):
            raise SourceError("DATABASE_CREDENTIAL_UNAVAILABLE")
        keys = (pointer * 4)(
            constant("kSecClass"),
            constant("kSecAttrService"),
            constant("kSecAttrAccount"),
            constant("kSecReturnData"),
        )
        values = (pointer * 4)(
            constant("kSecClassGenericPassword"),
            service,
            account,
            pointer.in_dll(core, "kCFBooleanTrue").value,
        )
        query = core.CFDictionaryCreate(None, keys, values, 4, None, None)
        owned.append(query)
        if not query:
            raise SourceError("DATABASE_CREDENTIAL_UNAVAILABLE")
        result = pointer()
        status = security.SecItemCopyMatching(query, ctypes.byref(result))
        owned.append(result.value)
        if status or not result.value:
            raise SourceError("DATABASE_CREDENTIAL_UNAVAILABLE")
        size = core.CFDataGetLength(result)
        if not 0 <= size <= 4096:
            raise SourceError("DATABASE_CREDENTIAL_UNAVAILABLE")
        return ctypes.string_at(core.CFDataGetBytePtr(result), size)
    finally:
        for item in reversed(owned):
            if item:
                core.CFRelease(item)


def read_secret(reference: str) -> str:
    """Read without spawning a different executable or printing credential material."""
    if not isinstance(reference, str) or re.fullmatch(r"colink-db-[a-f0-9]{32}", reference) is None:
        raise SourceError("INVALID_DATABASE_CREDENTIAL")
    if sys.platform != "darwin":
        raise SourceError("DATABASE_KEYCHAIN_UNAVAILABLE")
    try:
        return _read(reference).decode("utf-8")
    except SourceError:
        raise
    except Exception:
        raise SourceError("DATABASE_CREDENTIAL_UNAVAILABLE") from None

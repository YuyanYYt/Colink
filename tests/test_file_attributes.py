import base64
import copy
import ctypes
import errno
import hashlib
import json
import os
import stat
import struct
import sys
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

import code_context.file_attributes as attributes
from code_context.file_attributes import (
    ACLEntry,
    FileAttributes,
    FileAttributesError,
    apply_file_attributes,
    apply_prepared_file_attributes,
    capture_directory_attributes,
    capture_file_attributes,
    ensure_private_staging,
    verify_file_attributes,
)
from code_context.source_access import SourceError


@pytest.fixture
def opened(tmp_path):
    path = tmp_path / f".colink-write-{uuid4().hex}.tmp"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    # The caller's durable inode registration precedes every mutating API.
    registered = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
    try:
        yield fd, path, registered
    finally:
        os.close(fd)


def descriptor_attrs(**kwargs):
    return FileAttributes("darwin", os.geteuid(), os.getegid(), 0o640, **kwargs)


def resign(record):
    result = copy.deepcopy(record)
    result.pop("sha256", None)
    raw = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def entry(seed=1, *, tag=1, permissions=2, flags=0):
    return ACLEntry(tag, bytes([seed]) * 16, permissions, flags)


def posix_acl(entries=3):
    values = [(1, 6, 0xFFFFFFFF), (4, 4, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
    if entries > 3:
        values[1:1] = [(2, 4, index + 1000) for index in range(entries - 3)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *value) for value in values)


def test_capture_native_fd_no_body_read_and_frozen_record(opened, monkeypatch):
    fd, path, registered = opened
    os.write(fd, "\ufeff中文\r\n正文独特token\n".encode())
    os.fchmod(fd, 0o640)
    before = os.fstat(fd)
    monkeypatch.setattr(os, "read", lambda *args: pytest.fail("must not read the data fork"))
    monkeypatch.setattr(os, "lseek", lambda *args: pytest.fail("must not seek the data fork"))
    attrs = capture_file_attributes(fd)
    assert (attrs.uid, attrs.gid, attrs.mode) == (before.st_uid, before.st_gid, 0o640)
    assert attrs.flags == 0
    assert FileAttributes.from_record(json.loads(json.dumps(attrs.to_record()))) == attrs
    assert len(attrs.sha256) == 64
    assert "正文独特token" not in json.dumps(attrs.to_record(), ensure_ascii=False)
    assert registered == (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
    assert before.st_mtime_ns == path.stat().st_mtime_ns
    with pytest.raises(FrozenInstanceError):
        attrs.mode = 0o777


@pytest.mark.parametrize("mode", [0o400, 0o600, 0o640, 0o755, 0o777])
def test_native_empty_temp_apply_exact_mode_gid_xattrs_and_mtime(opened, mode, monkeypatch):
    fd, path, registered = opened
    creation = ensure_private_staging(fd)
    old_time = 1_000_000_000_123_456_789
    os.utime(fd, ns=(old_time, old_time))
    wanted = replace(
        creation,
        mode=mode,
        xattrs=tuple(sorted((*creation.xattrs, ("user.colink-test", b"\0\xffbinary")))),
    )
    monkeypatch.setattr(os, "write", lambda *args: pytest.fail("attributes must not write body"))
    monkeypatch.setattr(os, "utime", lambda *args, **kw: pytest.fail("must not restore times"))
    apply_file_attributes(fd, wanted)
    assert capture_file_attributes(fd) == wanted
    assert dict(attributes._get_xattrs(fd))["user.colink-test"] == b"\0\xffbinary"
    assert (path.stat().st_dev, path.stat().st_ino) == registered
    assert path.stat().st_size == 0
    assert path.stat().st_mtime_ns == old_time
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_native_apply_removes_extra_xattr_but_does_not_touch_source(tmp_path, opened):
    source = tmp_path / "source.py"
    source.write_bytes(b"original\n")
    source.chmod(0o640)
    src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    fd, _, _ = opened
    try:
        attributes._set_xattr(src, "user.colink-source", b"source metadata")
        expected = capture_file_attributes(src)
        attributes._set_xattr(fd, "user.colink-extra", b"extra")
        apply_file_attributes(fd, expected)
        assert capture_file_attributes(fd) == expected
        assert "user.colink-extra" not in dict(attributes._get_xattrs(fd))
        assert capture_file_attributes(src) == expected
        assert source.read_bytes() == b"original\n"
    finally:
        os.close(src)


@pytest.mark.parametrize("mode", [0, 0o200])
def test_native_unverifiable_final_mode_is_not_relaxed(opened, mode):
    fd, _, _ = opened
    wanted = replace(capture_file_attributes(fd), mode=mode)
    with pytest.raises(FileAttributesError):
        apply_file_attributes(fd, wanted)
    assert stat.S_IMODE(os.fstat(fd).st_mode) == mode


def test_managed_xattr_that_reappears_is_not_silently_ignored(opened):
    fd, path, _ = opened
    creation = capture_file_attributes(fd)
    if "com.apple.provenance" not in dict(creation.xattrs):
        pytest.skip("no kernel-managed provenance on this fd")
    wanted = replace(creation, xattrs=())
    with pytest.raises(FileAttributesError):
        apply_file_attributes(fd, wanted)
    assert path.exists()
    assert "com.apple.provenance" in dict(capture_file_attributes(fd).xattrs)


@pytest.mark.skipif(sys.platform != "darwin", reason="actual Apple extended ACL")
def test_native_ordered_acl_uuid_deny_allow_inherited_flags_roundtrip(opened):
    fd, _, _ = opened
    wanted = replace(
        capture_file_attributes(fd),
        mode=0o640,
        acl=(entry(7, tag=2, permissions=4), entry(8, permissions=2, flags=16)),
    )
    apply_file_attributes(fd, wanted)
    actual = capture_file_attributes(fd)
    assert actual == wanted
    assert FileAttributes.from_record(actual.to_record()) == wanted
    swapped = replace(wanted, acl=tuple(reversed(wanted.acl)))
    assert swapped.sha256 != wanted.sha256
    with pytest.raises(FileAttributesError, match="ATTRIBUTES_CHANGED"):
        verify_file_attributes(fd, swapped)


@pytest.mark.skipif(sys.platform != "darwin", reason="actual Apple ACL inheritance")
def test_native_private_staging_returns_creation_acl_and_xattrs(tmp_path):
    parent = tmp_path / "inheriting"
    parent.mkdir(mode=0o700)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        attributes._set_acl(directory, (entry(9, permissions=2, flags=32),), 0)
    finally:
        os.close(directory)
    path = parent / f".colink-write-{uuid4().hex}.tmp"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        attributes._set_xattr(fd, "user.colink-created", b"keep")
        creation = capture_file_attributes(fd)
        assert creation.acl and creation.acl[0].flags & 16
        returned = ensure_private_staging(fd)
        assert returned == creation
        assert capture_file_attributes(fd) == replace(creation, mode=0o600, acl=None, acl_flags=0)
        apply_file_attributes(fd, FileAttributes.from_record(returned.to_record()))
        assert capture_file_attributes(fd) == creation
    finally:
        os.close(fd)


def test_capture_detects_external_attribute_change_without_body_change(opened, monkeypatch):
    fd, _, _ = opened
    old = capture_file_attributes(fd)
    original, calls = attributes._capture_once, []

    def raced(descriptor, info):
        value = original(descriptor, info)
        calls.append(1)
        if len(calls) == 1:
            attributes._set_xattr(fd, "user.colink-race", b"external")
        return value

    monkeypatch.setattr(attributes, "_capture_once", raced)
    with pytest.raises(FileAttributesError, match="ATTRIBUTES_CHANGED"):
        capture_file_attributes(fd)
    monkeypatch.setattr(attributes, "_capture_once", original)
    with pytest.raises(FileAttributesError, match="ATTRIBUTES_CHANGED"):
        verify_file_attributes(fd, old)
    assert dict(attributes._get_xattrs(fd))["user.colink-race"] == b"external"


@pytest.mark.parametrize("kind", ["body", "hardlink", "readonly", "directory", "foreign", "flags"])
def test_mutation_rejects_unsafe_fd_before_any_changes(opened, tmp_path, monkeypatch, kind):
    fd, path, _ = opened
    wanted = capture_file_attributes(fd)
    other = None
    if kind == "body":
        os.write(fd, b"do not change")
    elif kind == "hardlink":
        os.link(path, tmp_path / "other")
    elif kind == "readonly":
        other = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        fd = other
    elif kind == "directory":
        other = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        fd = other
    elif kind == "foreign":
        monkeypatch.setattr(os, "geteuid", lambda: wanted.uid + 1)
    else:
        monkeypatch.setattr(attributes, "_file_flags", lambda *_: 2)
    monkeypatch.setattr(os, "fchmod", lambda *args: pytest.fail("must reject before chmod"))
    try:
        for operation in (ensure_private_staging, lambda f: apply_file_attributes(f, wanted)):
            with pytest.raises(FileAttributesError):
                operation(fd)
    finally:
        if other is not None:
            os.close(other)


@pytest.mark.parametrize("fd", [-1, True, False, None, "3", 1.5])
def test_invalid_fd_is_safe_source_error(fd):
    with pytest.raises(FileAttributesError) as error:
        capture_file_attributes(fd)
    assert isinstance(error.value, SourceError)


def test_platform_unsupported_and_cross_platform_are_not_fallbacks(opened, monkeypatch):
    fd, _, _ = opened
    wanted = capture_file_attributes(fd)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(FileAttributesError, match="UNSUPPORTED_ATTRIBUTES"):
        capture_file_attributes(fd)
    with pytest.raises(FileAttributesError, match="UNSUPPORTED_ATTRIBUTES"):
        apply_file_attributes(fd, wanted)


def test_fchown_is_explicit_and_failed_gid_change_does_not_relax_mode(opened, monkeypatch):
    fd, _, _ = opened
    original = capture_file_attributes(fd)
    wanted = replace(original, gid=original.gid + 1, mode=0o777)
    seen = []

    def refuse(descriptor, uid, gid):
        seen.append((descriptor, uid, gid))
        raise PermissionError("private path and metadata token")

    monkeypatch.setattr(os, "fchown", refuse)
    with pytest.raises(FileAttributesError) as error:
        apply_file_attributes(fd, wanted)
    assert seen == [(fd, -1, wanted.gid)]
    assert capture_file_attributes(fd) == original
    assert "token" not in str(error.value)
    assert error.value.__suppress_context__


def test_false_success_native_setter_is_rejected_by_final_verify(opened, monkeypatch):
    fd, _, _ = opened
    original = capture_file_attributes(fd)
    wanted = replace(
        original, xattrs=tuple(sorted((*original.xattrs, ("user.colink-new", b"wanted"))))
    )
    monkeypatch.setattr(attributes, "_set_xattr", lambda *args: None)
    with pytest.raises(FileAttributesError, match="ATTRIBUTES_CHANGED"):
        apply_file_attributes(fd, wanted)


def test_apply_race_makes_nonempty_temp_fail_and_keeps_body(opened, monkeypatch):
    fd, path, _ = opened
    wanted = capture_file_attributes(fd)
    original = os.fsync

    def raced(descriptor):
        os.write(descriptor, b"external content")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", raced)
    with pytest.raises(FileAttributesError, match="UNSAFE_TEMP"):
        apply_file_attributes(fd, wanted)
    assert path.read_bytes() == b"external content"


@pytest.mark.parametrize("failure", ["acl", "xattr", "chmod", "fsync"])
def test_partial_temp_errors_are_not_ignored_or_undone(opened, monkeypatch, failure):
    fd, path, _ = opened
    original = capture_file_attributes(fd)
    wanted = replace(
        original,
        mode=0o640,
        xattrs=tuple(sorted((*original.xattrs, ("user.colink-new", b"value")))),
    )

    def fail(*args):
        raise OSError("secret-token-from-native")

    target = {
        "acl": (attributes, "_set_acl"),
        "xattr": (attributes, "_set_xattr"),
        "chmod": (os, "fchmod"),
        "fsync": (os, "fsync"),
    }[failure]
    monkeypatch.setattr(*target, fail)
    with pytest.raises(FileAttributesError) as error:
        apply_file_attributes(fd, wanted)
    assert "secret-token" not in str(error.value)
    assert path.exists() and path.stat().st_size == 0


def test_record_preserves_acl_order_and_binary_xattrs():
    original = descriptor_attrs(
        acl=(entry(3, tag=2), entry(4, flags=16)),
        acl_flags=1 << 17,
        xattrs=(("user.a", b"\0\xff\xfe"), ("user.中文", b"")),
    )
    assert FileAttributes.from_record(original.to_record()) == original
    assert original.sha256 != replace(original, acl=tuple(reversed(original.acl))).sha256
    assert original.sha256 != replace(original, gid=original.gid + 1).sha256
    assert original.sha256 != replace(original, xattrs=(("user.a", b"changed"),)).sha256


@pytest.mark.parametrize("field", ["version", "uid", "gid", "mode", "flags"])
@pytest.mark.parametrize("value", [True, False, -1, None, "0", 1.5, [], {}])
def test_record_rejects_false_nonintegers_and_invalid_numbers(field, value):
    record = descriptor_attrs().to_record()
    record[field] = value
    with pytest.raises(FileAttributesError):
        FileAttributes.from_record(record)


@pytest.mark.parametrize("value", [None, [], "{}", True, 1])
def test_record_non_dict_rejected(value):
    with pytest.raises(FileAttributesError):
        FileAttributes.from_record(value)


@pytest.mark.parametrize("field", sorted(attributes._RECORD_KEYS))
def test_record_missing_and_unknown_keys(field):
    record = descriptor_attrs().to_record()
    record.pop(field)
    with pytest.raises(FileAttributesError):
        FileAttributes.from_record(record)
    record = descriptor_attrs().to_record()
    record["unknown-secret-input"] = "not echoed"
    with pytest.raises(FileAttributesError) as error:
        FileAttributes.from_record(record)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", 0o1000),
        ("flags", 1),
        ("uid", 0xFFFFFFFF),
        ("gid", 0xFFFFFFFF),
        ("platform", "windows"),
        ("platform", []),
        ("sha256", "A" * 64),
        ("sha256", "0" * 64),
        ("version", 2),
    ],
)
def test_record_rejects_unsupported_or_tampered_values(field, value):
    record = descriptor_attrs().to_record()
    record[field] = value
    with pytest.raises(FileAttributesError):
        FileAttributes.from_record(record)


@pytest.mark.parametrize(
    "name",
    ["", "a\0b", "a" * 128, "中" * 43, "\ud800", "com.apple.ResourceFork", "com.apple.decmpfs"],
)
def test_unsupported_or_invalid_xattr_names_fail_closed(name):
    with pytest.raises(FileAttributesError):
        descriptor_attrs(xattrs=((name, b"value"),)).to_record()


@pytest.mark.parametrize("value", ["!", "AAAA===", "A", "é", "YWJj\n", "YQ== ", True])
def test_record_rejects_noncanonical_or_invalid_base64(value):
    record = descriptor_attrs(xattrs=(("user.a", b"x"),)).to_record()
    record["xattrs"][0]["value"] = value
    with pytest.raises(FileAttributesError):
        FileAttributes.from_record(record)


@pytest.mark.parametrize("kind", ["count", "value", "total", "duplicate", "order", "nested_key"])
def test_xattr_budget_order_and_exact_nested_record(kind):
    record = descriptor_attrs().to_record()
    item = {"name": "user.a", "value": ""}
    if kind == "count":
        record["xattrs"] = [item] * 65
    elif kind == "value":
        record["xattrs"] = [
            {"name": "user.a", "value": base64.b64encode(b"x" * (64 * 1024 + 1)).decode()}
        ]
    elif kind == "total":
        record["xattrs"] = [
            {"name": f"user.{i}", "value": base64.b64encode(b"x" * 65536).decode()}
            for i in range(2)
        ]
    elif kind == "duplicate":
        record["xattrs"] = [item, item]
    elif kind == "order":
        record["xattrs"] = [{"name": "user.z", "value": ""}, item]
    else:
        record["xattrs"] = [{**item, "extra": True}]
    with pytest.raises(FileAttributesError):
        FileAttributes.from_record(resign(record))


@pytest.mark.parametrize(
    "field,value",
    [
        ("tag", True),
        ("tag", 0),
        ("tag", 3),
        ("permissions", True),
        ("permissions", 1),
        ("permissions", 1 << 21),
        ("flags", True),
        ("flags", 1),
        ("qualifier", "a" * 31),
        ("qualifier", "G" * 32),
        ("qualifier", "A" * 32),
    ],
)
def test_acl_entry_strict_values(field, value):
    record = descriptor_attrs(acl=(entry(),)).to_record()
    record["acl"]["entries"][0][field] = value
    with pytest.raises(FileAttributesError):
        FileAttributes.from_record(resign(record))


def test_acl_budget_unknown_keys_unknown_flags_and_linux_darwin_acl():
    baseline = descriptor_attrs(acl=(entry(),)).to_record()
    for modifier in [
        lambda r: r["acl"]["entries"].extend([r["acl"]["entries"][0]] * 128),
        lambda r: r["acl"].update(extra=1),
        lambda r: r["acl"].update(flags=2),
        lambda r: r["acl"]["entries"][0].update(extra=1),
        lambda r: r.update(platform="linux"),
    ]:
        record = copy.deepcopy(baseline)
        modifier(record)
        with pytest.raises(FileAttributesError):
            FileAttributes.from_record(resign(record))


def test_exact_attribute_total_and_count_boundaries():
    attrs = descriptor_attrs(xattrs=(("user.a", b"x" * 65536), ("user.b", b"y" * (65536 - 14))))
    assert FileAttributes.from_record(attrs.to_record()) == attrs
    with pytest.raises(FileAttributesError, match="ATTRIBUTE_BUDGET"):
        replace(attrs, xattrs=(attrs.xattrs[0], ("user.b", attrs.xattrs[1][1] + b"z"))).to_record()
    sixty_four = tuple((f"user.{i:02}", b"") for i in range(64))
    assert (
        FileAttributes.from_record(descriptor_attrs(xattrs=sixty_four).to_record()).xattrs
        == sixty_four
    )
    assert FileAttributes.from_record(
        descriptor_attrs(acl=tuple(entry() for _ in range(128))).to_record()
    ).acl


def test_linux_posix_acl_bytes_are_bounded_and_retained():
    raw = posix_acl(128)
    attrs = FileAttributes(
        "linux",
        os.geteuid(),
        os.getegid(),
        0o640,
        xattrs=(("system.posix_acl_access", raw), ("user.binary", b"\xff")),
    )
    assert FileAttributes.from_record(attrs.to_record()) == attrs
    for invalid in [posix_acl(129), b"", b"\x03\0\0\0", raw + b"x"]:
        with pytest.raises(FileAttributesError):
            replace(attrs, xattrs=(("system.posix_acl_access", invalid),)).to_record()


def test_linux_fd_backend_staging_and_apply_without_native_mac_calls(opened, monkeypatch):
    fd, _, _ = opened
    values = {"system.posix_acl_access": posix_acl(), "user.binary": b"\xff"}
    observed = []
    monkeypatch.setattr(attributes, "_platform", lambda: "linux")
    monkeypatch.setattr(attributes, "_file_flags", lambda *args: 0)
    monkeypatch.setattr(
        attributes, "_native", lambda: pytest.fail("must use fd os xattrs on Linux")
    )

    def list_values(descriptor):
        assert descriptor == fd
        return list(values)

    def set_value(descriptor, name, value):
        assert descriptor == fd
        observed.append((name, value))
        values[name] = value

    monkeypatch.setattr(os, "listxattr", list_values, raising=False)
    monkeypatch.setattr(os, "getxattr", lambda descriptor, name: values[name], raising=False)
    monkeypatch.setattr(os, "setxattr", set_value, raising=False)
    monkeypatch.setattr(os, "removexattr", lambda descriptor, name: values.pop(name), raising=False)
    creation = ensure_private_staging(fd)
    assert creation.platform == "linux" and creation.acl is None
    assert "system.posix_acl_access" not in values
    assert values["user.binary"] == b"\xff"
    apply_file_attributes(fd, creation)
    assert capture_file_attributes(fd) == creation
    assert ("system.posix_acl_access", posix_acl()) in observed


def test_native_capture_rejects_resource_fork_and_xattr_over_budget(opened, monkeypatch):
    fd, _, _ = opened
    monkeypatch.setattr(attributes, "_get_xattrs", lambda f: (("com.apple.ResourceFork", b"x"),))
    with pytest.raises(FileAttributesError, match="UNSUPPORTED_ATTRIBUTES"):
        capture_file_attributes(fd)
    monkeypatch.setattr(attributes, "_get_xattrs", lambda f: (("user.a", b"x" * 65537),))
    with pytest.raises(FileAttributesError):
        capture_file_attributes(fd)


def test_no_source_paths_or_file_copy_functions_in_layer():
    assert not hasattr(attributes, "copyfile")
    assert not hasattr(attributes, "fcopyfile")
    assert not hasattr(attributes, "shutil")
    assert not hasattr(attributes, "SourceAccess")


def test_missing_native_support_is_a_safe_failure(opened, monkeypatch):
    fd, _, _ = opened
    monkeypatch.setattr(
        attributes, "_native", lambda: (_ for _ in ()).throw(AttributeError("secret-symbol"))
    )
    with pytest.raises(FileAttributesError) as error:
        capture_file_attributes(fd)
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "raw", [b"", b"LF\n", b"CRLF\r\n", "\ufeff中文\n尾".encode(), b"x" * (4 * 1024 * 1024)]
)
def test_prepared_body_then_final_attributes_native_exact_proof(opened, raw, monkeypatch):
    fd, path, registered = opened
    creation = ensure_private_staging(fd)
    desired = replace(creation, mode=0o644)
    assert capture_file_attributes(fd).mode == 0o600
    assert capture_file_attributes(fd).acl is None
    os.write(fd, raw)
    written = os.fstat(fd)
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    monkeypatch.setattr(os, "write", lambda *args: pytest.fail("must not change prepared body"))
    monkeypatch.setattr(os, "utime", lambda *args, **kw: pytest.fail("must not restore old mtime"))
    apply_prepared_file_attributes(
        fd, desired, identity=registered, size=len(raw), sha256=hashlib.sha256(raw).hexdigest()
    )
    assert capture_file_attributes(fd) == desired
    assert path.read_bytes() == raw
    assert os.lseek(fd, 0, os.SEEK_CUR) == offset
    assert os.fstat(fd).st_mtime_ns == written.st_mtime_ns


@pytest.mark.parametrize("kind", ["identity", "size", "sha", "hardlink", "readonly"])
def test_prepared_bad_proof_does_not_change_any_attributes(opened, tmp_path, monkeypatch, kind):
    fd, path, registered = opened
    creation = ensure_private_staging(fd)
    raw = b"prepared original"
    os.write(fd, raw)
    identity, size, digest = registered, len(raw), hashlib.sha256(raw).hexdigest()
    replacement = None
    if kind == "identity":
        identity = (identity[0], identity[1] + 1)
    elif kind == "size":
        size += 1
    elif kind == "sha":
        digest = "0" * 64
    elif kind == "hardlink":
        os.link(path, tmp_path / "external-link")
    else:
        replacement = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        fd = replacement
    before = os.fstat(fd)
    monkeypatch.setattr(os, "fchmod", lambda *args: pytest.fail("must reject before chmod"))
    try:
        with pytest.raises(FileAttributesError):
            apply_prepared_file_attributes(
                fd, replace(creation, mode=0o777), identity=identity, size=size, sha256=digest
            )
        assert os.fstat(fd).st_mode == before.st_mode
        assert path.read_bytes() == raw
    finally:
        if replacement is not None:
            os.close(replacement)


@pytest.mark.parametrize(
    "identity,size,digest",
    [
        ((True, 1), 0, "0" * 64),
        ((1,), 0, "0" * 64),
        ([1, 2], 0, "0" * 64),
        ((-1, 2), 0, "0" * 64),
        ((1, 2), True, "0" * 64),
        ((1, 2), -1, "0" * 64),
        ((1, 2), 4 * 1024 * 1024 + 1, "0" * 64),
        ((1, 2), 0, "A" * 64),
        ((1, 2), 0, "0" * 63),
        ((1, 2), 0, False),
    ],
)
def test_prepared_metadata_schema_is_strict(opened, identity, size, digest):
    fd, _, _ = opened
    attrs = capture_file_attributes(fd)
    with pytest.raises(FileAttributesError, match="INVALID_ATTRIBUTES"):
        apply_prepared_file_attributes(fd, attrs, identity=identity, size=size, sha256=digest)


def test_prepared_full_read_change_before_mutation_is_rejected(opened, monkeypatch):
    fd, path, identity = opened
    creation = ensure_private_staging(fd)
    raw = b"prepared data"
    os.write(fd, raw)
    original, changed = os.pread, []

    def raced(descriptor, size, offset):
        result = original(descriptor, size, offset)
        if not changed:
            changed.append(1)
            os.pwrite(fd, b"external data", 0)
        return result

    monkeypatch.setattr(os, "pread", raced)
    monkeypatch.setattr(os, "fchmod", lambda *args: pytest.fail("must reject unstable full read"))
    with pytest.raises(FileAttributesError):
        apply_prepared_file_attributes(
            fd,
            replace(creation, mode=0o644),
            identity=identity,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    assert path.read_bytes() == b"external data"
    assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600


def test_prepared_full_hash_rechecks_post_attribute_race_and_keeps_material(opened, monkeypatch):
    fd, path, identity = opened
    creation = ensure_private_staging(fd)
    raw = b"prepared data"
    os.write(fd, raw)
    original = os.fsync

    def raced(descriptor):
        os.pwrite(fd, b"external data", 0)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", raced)
    with pytest.raises(FileAttributesError):
        apply_prepared_file_attributes(
            fd,
            replace(creation, mode=0o644),
            identity=identity,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    assert path.exists() and path.read_bytes() == b"external data"


@pytest.mark.parametrize("links", [0, 2])
def test_capture_and_verify_held_unlinked_or_new_link_fd(opened, tmp_path, links):
    fd, path, _ = opened
    expected = capture_file_attributes(fd)
    if links == 0:
        os.unlink(path)
    else:
        os.link(path, tmp_path / "installed.py")
    assert os.fstat(fd).st_nlink == links
    assert capture_file_attributes(fd) == expected
    verify_file_attributes(fd, expected)
    with pytest.raises(FileAttributesError, match="UNSAFE_TEMP"):
        apply_file_attributes(fd, expected)


def test_directory_capture_is_read_only_and_exact_type(tmp_path, opened, monkeypatch):
    directory = tmp_path / "created-directory"
    directory.mkdir(mode=0o750)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        attributes._set_xattr(fd, "user.directory", b"attributes")
        before = os.fstat(fd)
        monkeypatch.setattr(
            os, "fchmod", lambda *args: pytest.fail("directory capture must be read-only")
        )
        attrs = capture_directory_attributes(fd)
        assert attrs.mode == stat.S_IMODE(before.st_mode)
        assert attrs.uid == before.st_uid and attrs.gid == before.st_gid
        assert dict(attrs.xattrs)["user.directory"] == b"attributes"
        assert FileAttributes.from_record(attrs.to_record()) == attrs
        attributes._set_xattr(fd, "user.directory", b"external change")
        assert capture_directory_attributes(fd) != attrs
        with pytest.raises(FileAttributesError):
            capture_file_attributes(fd)
        with pytest.raises(FileAttributesError):
            apply_file_attributes(fd, attrs)
        with pytest.raises(FileAttributesError):
            capture_directory_attributes(opened[0])
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple directory ACL")
def test_directory_capture_preserves_ordered_acl_and_mode(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        wanted = (entry(11, tag=2, flags=32), entry(12, flags=64))
        attributes._set_acl(fd, wanted, 0)
        attrs = capture_directory_attributes(fd)
        assert attrs.acl == wanted
        assert FileAttributes.from_record(attrs.to_record()) == attrs
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple file flags")
def test_actual_nonzero_flags_are_explicitly_unsupported(opened):
    fd, path, _ = opened
    os.chflags(path, 1)  # UF_NODUMP, not immutable; retain the fixture unchanged.
    with pytest.raises(FileAttributesError, match="UNSUPPORTED_ATTRIBUTES"):
        capture_file_attributes(fd)


def test_native_volume_profile_rejects_appledouble_filesystems(opened, monkeypatch):
    def statfs(fd, pointer):
        ctypes.cast(pointer, ctypes.POINTER(attributes._StatFS)).contents.name = b"smbfs"
        return 0

    monkeypatch.setattr(attributes, "_native", lambda: SimpleNamespace(fstatfs=statfs))
    with pytest.raises(FileAttributesError, match="UNSUPPORTED_ATTRIBUTES"):
        capture_file_attributes(opened[0])


@pytest.mark.parametrize("code", [errno.EACCES, errno.ENOTSUP, errno.EINVAL, errno.EIO])
def test_acl_native_errors_are_not_treated_as_no_acl(monkeypatch, code):
    def acl_get(fd, kind):
        ctypes.set_errno(code)
        return None

    monkeypatch.setattr(attributes, "_native", lambda: SimpleNamespace(acl_get_fd_np=acl_get))
    with pytest.raises(FileAttributesError, match="NATIVE_ATTRIBUTES_FAILED"):
        attributes._get_acl(123)


def test_linux_inode_flag_ioctl_failure_is_not_zero_flags(opened, monkeypatch):
    monkeypatch.setattr(attributes, "_platform", lambda: "linux")
    monkeypatch.setattr(
        attributes.fcntl,
        "ioctl",
        lambda *args: (_ for _ in ()).throw(OSError(errno.ENOTTY, "unsafe fallback")),
    )
    with pytest.raises(FileAttributesError, match="ATTRIBUTES_UNAVAILABLE"):
        capture_file_attributes(opened[0])


@pytest.mark.parametrize("kind", ["mode", "acl"])
def test_prepared_external_staging_permissions_are_conflict_not_reset(opened, monkeypatch, kind):
    fd, path, identity = opened
    original = ensure_private_staging(fd)
    raw = b"private staged body"
    os.write(fd, raw)
    if kind == "mode":
        os.fchmod(fd, 0o644)
    else:
        attributes._set_acl(fd, (entry(19, permissions=2),), 0)
    changed = capture_file_attributes(fd)
    monkeypatch.setattr(
        os, "fchmod", lambda *args: pytest.fail("must not reset external permissions")
    )
    with pytest.raises(FileAttributesError, match="UNSAFE_TEMP"):
        apply_prepared_file_attributes(
            fd,
            replace(original, mode=0o644),
            identity=identity,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    assert path.read_bytes() == raw
    assert capture_file_attributes(fd) == changed

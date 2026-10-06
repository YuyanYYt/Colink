import errno
import hashlib
import os
import stat
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

import code_context.file_mutation as mutation
from code_context.file_mutation import (
    FileMutationError,
    PreparedFile,
    commit_file,
    discard_prepared,
    prepare_file,
)
from code_context.policy import MAX_FILE_BYTES
from code_context.scanner import _version
from code_context.source_access import SourceAccess, SourceError


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(path):
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def temp_name():
    return f".colink-write-{uuid4().hex}.tmp"


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "demo.py").write_bytes(b"original\n")
    (root / "demo.py").chmod(0o640)
    return SourceAccess(root)


def prepare(source, path="demo.py", raw=b"new\n", mode=0o640, callback=None, name=None):
    return prepare_file(source, path, raw, mode, name or temp_name(), callback or (lambda _: None))


def temporary(source, prepared):
    return (
        source.root / prepared.path.rsplit("/", 1)[0] / prepared.temp_name
        if "/" in prepared.path
        else source.root / prepared.temp_name
    )


def assert_before(error):
    assert isinstance(error.value, SourceError)
    assert error.value.may_have_committed is False


def assert_pending(error):
    assert error.value.may_have_committed is True
    assert str(error.value).startswith("MUTATION_PENDING:")


@pytest.mark.parametrize("mode", [0, 0o200, 0o400, 0o600, 0o640, 0o755, 0o777])
@pytest.mark.parametrize("raw", [b"", b"LF\n", b"CRLF\r\n", "\ufeff中文\r\n尾".encode()])
def test_prepare_exact_owned_mode_bytes_and_immediate_creation_record(source, mode, raw):
    recorded = []
    name = temp_name()

    def on_created(prepared):
        path = source.root / name
        info = path.stat()
        assert info.st_size == 0
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert prepared.temp_identity == identity(path)
        assert prepared.parent_identity == identity(source.root)
        assert prepared.sha256 == sha(raw)
        assert prepared.size == len(raw)
        assert prepared.mode == mode
        recorded.append(prepared)

    prepared = prepare(source, raw=raw, mode=mode, callback=on_created, name=name)
    assert isinstance(prepared, PreparedFile)
    assert recorded == [prepared]
    path = temporary(source, prepared)
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert path.stat().st_size == len(raw)
    assert path.stat().st_nlink == 1
    assert path.stat().st_uid == os.geteuid()
    assert prepared.source_id == source.source_id
    assert (source.root / "demo.py").read_bytes() == b"original\n"
    with pytest.raises(FrozenInstanceError):
        prepared.size = 1


@pytest.mark.parametrize(
    "raw",
    [
        b"x\x00y",
        b"\xff",
        b"x" * (MAX_FILE_BYTES + 1),
        b"-----BEGIN " + b"PRIVATE KEY-----",  # Synthetic header, not a real key.
        None,
        "not bytes",
        bytearray(b"x"),
    ],
)
def test_prepare_content_policy_rejection_creates_nothing(source, raw):
    name, called = temp_name(), []
    with pytest.raises(FileMutationError) as error:
        prepare_file(source, "demo.py", raw, 0o600, name, called.append)
    assert_before(error)
    assert not called
    assert not (source.root / name).exists()
    assert (source.root / "demo.py").read_bytes() == b"original\n"


@pytest.mark.parametrize(
    "mode", [True, False, -1, 0o1000, 0o1644, 0o2644, 0o4644, 0o10000, "600", 0.0, None]
)
def test_prepare_rejects_bool_noninteger_and_special_modes(source, mode):
    name = temp_name()
    with pytest.raises(FileMutationError, match="INVALID_PREPARE") as error:
        prepare(source, mode=mode, name=name)
    assert_before(error)
    assert not (source.root / name).exists()


@pytest.mark.parametrize(
    "name",
    [
        "temp.tmp",
        ".colink-write-a.tmp",
        ".colink-write-" + "A" * 32 + ".tmp",
        ".colink-write-" + "a" * 31 + ".tmp",
        ".colink-write-" + "a" * 33 + ".tmp",
        "../" + ".colink-write-" + "a" * 32 + ".tmp",
        ".colink-write-" + "a" * 32 + ".tmp\n",
        "",
        None,
    ],
)
def test_only_exact_temp_basename_is_accepted(source, name):
    with pytest.raises(FileMutationError, match="INVALID_PREPARE") as error:
        prepare_file(source, "demo.py", b"new", 0o600, name, lambda _: None)
    assert_before(error)
    assert (source.root / "demo.py").read_bytes() == b"original\n"


@pytest.mark.parametrize(
    "path", ["../other.py", "/absolute.py", ".env", "missing/demo.py", "a/../demo.py", None]
)
def test_prepare_reuses_source_path_policy_and_never_creates_parents(source, path):
    with pytest.raises(FileMutationError) as error:
        prepare(source, path=path)
    assert_before(error)
    assert not (source.root / "missing").exists()
    assert list(source.root.iterdir()) == [source.root / "demo.py"]


def test_prepare_does_not_open_the_target_and_supports_exact_max_size(source, monkeypatch):
    opening = mutation.os.open
    names = []

    def record(name, *args, **kwargs):
        names.append(name)
        return opening(name, *args, **kwargs)

    monkeypatch.setattr(mutation.os, "open", record)
    raw = b"x" * MAX_FILE_BYTES
    prepared = prepare(source, raw=raw)
    assert "demo.py" not in names
    assert temporary(source, prepared).read_bytes() == raw


@pytest.mark.parametrize("symlink", [False, True])
def test_exclusive_temp_collision_never_truncates_or_follows(source, symlink):
    name = temp_name()
    path = source.root / name
    if symlink:
        path.symlink_to("demo.py")
    else:
        path.write_bytes(b"existing temp")
    with pytest.raises(FileMutationError) as error:
        prepare(source, name=name)
    assert_before(error)
    assert path.is_symlink() if symlink else path.read_bytes() == b"existing temp"
    assert (source.root / "demo.py").read_bytes() == b"original\n"


@pytest.mark.parametrize("failure", ["callback", "chmod", "write", "file_fsync", "directory_fsync"])
def test_prepare_failures_preserve_registered_temp_and_hide_inputs(source, monkeypatch, failure):
    name, recorded = temp_name(), []

    def fail(*_):
        raise OSError("private raw and path must not leak")

    def on_created(prepared):
        recorded.append(prepared)
        if failure == "callback":
            fail()

    if failure == "chmod":
        monkeypatch.setattr(mutation.os, "fchmod", fail)
    elif failure == "write":
        monkeypatch.setattr(mutation, "_write_all", fail)
    elif "fsync" in failure:
        fsync = mutation.os.fsync

        def selected(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            if directory == (failure == "directory_fsync"):
                fail()
            fsync(fd)

        monkeypatch.setattr(mutation.os, "fsync", selected)
    with pytest.raises(FileMutationError) as error:
        prepare(source, callback=on_created, name=name)
    assert_before(error)
    assert len(recorded) == 1
    assert (source.root / name).is_file()
    assert identity(source.root / name) == recorded[0].temp_identity
    assert "private raw" not in str(error.value)
    assert name not in str(error.value)
    assert (source.root / "demo.py").read_bytes() == b"original\n"


def test_prepare_handles_partial_writes_and_rejects_zero_progress(source, monkeypatch):
    write = mutation.os.write
    monkeypatch.setattr(mutation.os, "write", lambda fd, raw: write(fd, raw[:2]))
    prepared = prepare(source, raw="中文\r\n".encode())
    assert temporary(source, prepared).read_bytes() == "中文\r\n".encode()
    recorded = []
    monkeypatch.setattr(mutation.os, "write", lambda *_: 0)
    with pytest.raises(FileMutationError, match="TEMP_WRITE_FAILED") as error:
        prepare(source, callback=recorded.append)
    assert_before(error)
    assert temporary(source, recorded[0]).read_bytes() == b""


def test_prepare_full_read_detects_corruption(source, monkeypatch):
    read = mutation._read_opened
    recorded = []

    def corrupt(parent, name, fd, **kwargs):
        (source.root / name).write_bytes(b"changed\n")
        return read(parent, name, fd, **kwargs)

    monkeypatch.setattr(mutation, "_read_opened", corrupt)
    with pytest.raises(FileMutationError, match="TEMP_CHANGED") as error:
        prepare(source, callback=recorded.append)
    assert_before(error)
    assert temporary(source, recorded[0]).read_bytes() == b"changed\n"


@pytest.mark.parametrize("raw", [b"", b"new\n", b"new\r\n", "\ufeff中文\r\n尾".encode()])
@pytest.mark.parametrize("mode", [0o400, 0o600, 0o640, 0o755])
def test_existing_commit_uses_real_exchange_and_preserves_displaced_object(source, raw, mode):
    expected = source.read("demo.py")
    old_identity = expected.version[:2]
    prepared = prepare(source, raw=raw, mode=mode)
    result = commit_file(source, prepared, expected)
    target, temp = source.root / "demo.py", temporary(source, prepared)
    assert target.read_bytes() == raw
    assert temp.read_bytes() == expected.content.encode()
    assert identity(target) == prepared.temp_identity
    assert identity(temp) == old_identity
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert stat.S_IMODE(temp.stat().st_mode) == expected.mode
    assert target.stat().st_nlink == temp.stat().st_nlink == 1
    assert result == source.read("demo.py")


def test_new_file_installs_with_link_and_rereads_final_ctime_after_explicit_discard(source):
    prepared = prepare(source, path="new.py", raw="\ufeff中文\r\n".encode())
    result = commit_file(source, prepared, None)
    target, temp = source.root / "new.py", temporary(source, prepared)
    assert identity(target) == identity(temp) == prepared.temp_identity
    assert target.stat().st_nlink == temp.stat().st_nlink == 2
    assert result == source.read("new.py")
    discard_prepared(source, prepared, prepared.sha256, prepared.temp_identity)
    assert not temp.exists()
    assert target.stat().st_nlink == 1
    final = source.read("new.py")
    assert final.content == "\ufeff中文\r\n"
    assert final.version == _version(target.stat())
    assert final.sha256 == result.sha256
    discard_prepared(source, prepared, prepared.sha256, prepared.temp_identity)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_new_file_never_overwrites_existing_entries(source, kind):
    path = source.root / "new.py"
    if kind == "file":
        path.write_bytes(b"occupied")
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to("demo.py")
    prepared = prepare(source, path="new.py")
    with pytest.raises(FileMutationError, match="TARGET_EXISTS") as error:
        commit_file(source, prepared, None)
    assert_before(error)
    assert temporary(source, prepared).read_bytes() == b"new\n"
    assert (source.root / "demo.py").read_bytes() == b"original\n"
    if kind == "file":
        assert path.read_bytes() == b"occupied"
    elif kind == "directory":
        assert path.is_dir()
    else:
        assert path.is_symlink()


def test_target_created_in_check_to_link_window_is_not_overwritten(source, monkeypatch):
    prepared = prepare(source, path="new.py")
    link = mutation.os.link

    def competing(*args, **kwargs):
        (source.root / "new.py").write_bytes(b"external")
        return link(*args, **kwargs)

    monkeypatch.setattr(mutation.os, "link", competing)
    with pytest.raises(FileMutationError) as error:
        commit_file(source, prepared, None)
    assert_before(error)
    assert (source.root / "new.py").read_bytes() == b"external"
    assert temporary(source, prepared).stat().st_nlink == 1


@pytest.mark.parametrize("change", ["body", "metadata", "inode"])
def test_late_expected_check_reads_actual_bytes_and_full_version(source, change):
    expected = source.read("demo.py")
    prepared = prepare(source)
    target = source.root / "demo.py"
    if change == "body":
        target.write_bytes(b"external\n")
    elif change == "metadata":
        target.chmod(0o600)
        target.chmod(expected.mode)
    else:
        target.rename(source.root / "preserved-original.py")
        target.write_bytes(expected.content.encode())
        target.chmod(expected.mode)
    with pytest.raises(FileMutationError, match="SOURCE_CHANGED") as error:
        commit_file(source, prepared, expected)
    assert_before(error)
    assert temporary(source, prepared).read_bytes() == b"new\n"
    assert identity(target) != prepared.temp_identity


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "special_mode"])
def test_existing_commit_rejects_unsafe_target_without_touching_it(source, kind):
    expected, prepared = source.read("demo.py"), prepare(source)
    target = source.root / "demo.py"
    if kind == "hardlink":
        os.link(target, source.root / "alias.py")
    elif kind == "special_mode":
        target.chmod(0o2640)
    else:
        target.rename(source.root / "saved.py")
        if kind == "symlink":
            target.symlink_to("saved.py")
        elif kind == "directory":
            target.mkdir()
        else:
            os.mkfifo(target)
    before = target.stat(follow_symlinks=False)
    with pytest.raises(FileMutationError, match="UNSAFE_FILE") as error:
        commit_file(source, prepared, expected)
    assert_before(error)
    assert _version(target.stat(follow_symlinks=False)) == _version(before)
    assert temporary(source, prepared).read_bytes() == b"new\n"


def test_nonowned_target_is_rejected_before_swap(source, monkeypatch):
    expected, prepared = source.read("demo.py"), prepare(source)
    inspect = mutation._stat

    def foreign(parent, name):
        info = inspect(parent, name)
        if name == "demo.py":
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(mutation, "_stat", foreign)
    with pytest.raises(FileMutationError, match="UNSAFE_FILE") as error:
        commit_file(source, prepared, expected)
    assert_before(error)
    assert (source.root / "demo.py").read_bytes() == b"original\n"


@pytest.mark.parametrize("change", ["body", "same_body_new_inode", "mode", "symlink", "hardlink"])
def test_changed_prepared_temp_is_rejected_before_target_write(source, change):
    expected, prepared = source.read("demo.py"), prepare(source)
    path = temporary(source, prepared)
    if change == "body":
        path.write_bytes(b"tampered\n")
    elif change == "mode":
        path.chmod(0o600)
    elif change == "hardlink":
        os.link(path, source.root / "temp-alias.py")
    else:
        path.rename(source.root / "saved-temp.py")
        if change == "symlink":
            path.symlink_to("demo.py")
        else:
            path.write_bytes(b"new\n")
            path.chmod(prepared.mode)
    with pytest.raises(FileMutationError) as error:
        commit_file(source, prepared, expected)
    assert_before(error)
    assert (source.root / "demo.py").read_bytes() == b"original\n"
    assert path.exists()


@pytest.mark.parametrize("change", ["body", "same_body_new_inode", "mode", "symlink", "oversized"])
def test_check_to_swap_external_changes_leave_two_objects_and_pending(source, monkeypatch, change):
    expected, prepared = source.read("demo.py"), prepare(source)
    target, exchange = source.root / "demo.py", mutation._exchange

    def racing(parent, temp, name):
        if change == "body":
            target.write_bytes(b"external\n")
        elif change == "mode":
            target.chmod(0o600)
        elif change == "oversized":
            target.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        else:
            target.rename(source.root / "saved-original.py")
            if change == "symlink":
                target.symlink_to("saved-original.py")
            else:
                target.write_bytes(expected.content.encode())
                target.chmod(expected.mode)
        exchange(parent, temp, name)

    monkeypatch.setattr(mutation, "_exchange", racing)
    with pytest.raises(FileMutationError) as error:
        commit_file(source, prepared, expected)
    assert_pending(error)
    assert target.read_bytes() == b"new\n"
    displaced = temporary(source, prepared)
    assert displaced.exists()
    if change == "body":
        assert displaced.read_bytes() == b"external\n"
    elif change == "same_body_new_inode":
        assert displaced.read_bytes() == expected.content.encode()
        assert identity(displaced) != expected.version[:2]
    elif change == "mode":
        assert stat.S_IMODE(displaced.stat().st_mode) == 0o600
    elif change == "symlink":
        assert displaced.is_symlink()
        assert (source.root / "saved-original.py").read_bytes() == expected.content.encode()
    else:
        assert displaced.stat().st_size == MAX_FILE_BYTES + 1


def test_post_swap_installed_file_edit_is_pending_without_automatic_reverse(source, monkeypatch):
    expected, prepared = source.read("demo.py"), prepare(source)
    exchange = mutation._exchange

    def change_installed(*args):
        exchange(*args)
        (source.root / "demo.py").write_bytes(b"external after swap")

    monkeypatch.setattr(mutation, "_exchange", change_installed)
    with pytest.raises(FileMutationError) as error:
        commit_file(source, prepared, expected)
    assert_pending(error)
    assert (source.root / "demo.py").read_bytes() == b"external after swap"
    assert temporary(source, prepared).read_bytes() == expected.content.encode()


@pytest.mark.parametrize("new", [False, True])
def test_directory_fsync_failure_after_install_is_pending(source, monkeypatch, new):
    path = "new.py" if new else "demo.py"
    expected = None if new else source.read(path)
    prepared = prepare(source, path=path)

    def fail(_):
        raise OSError("private fsync path")

    monkeypatch.setattr(mutation.os, "fsync", fail)
    with pytest.raises(FileMutationError) as error:
        commit_file(source, prepared, expected)
    assert_pending(error)
    assert (source.root / path).read_bytes() == b"new\n"
    assert temporary(source, prepared).exists()
    assert "private fsync" not in str(error.value)


@pytest.mark.parametrize("new", [False, True])
def test_exception_at_native_return_boundary_after_install_is_pending(source, monkeypatch, new):
    path = "new.py" if new else "demo.py"
    expected = None if new else source.read(path)
    prepared = prepare(source, path=path)
    function = mutation.os.link if new else mutation._exchange

    def install_then_fail(*args, **kwargs):
        function(*args, **kwargs)
        raise RuntimeError("private native return failure")

    monkeypatch.setattr(
        mutation.os if new else mutation, "link" if new else "_exchange", install_then_fail
    )
    with pytest.raises(FileMutationError) as error:
        commit_file(source, prepared, expected)
    assert_pending(error)
    assert (source.root / path).read_bytes() == b"new\n"
    assert temporary(source, prepared).exists()
    assert "private native" not in str(error.value)


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("scope", ["parent", "source"])
def test_context_exit_replacement_after_install_is_pending(source, monkeypatch, new, scope):
    (source.root / "src").mkdir()
    (source.root / "src/demo.py").write_bytes(b"original\n")
    path = "src/new.py" if new else "src/demo.py"
    expected = None if new else source.read(path)
    prepared = prepare(source, path=path)
    original = mutation.os.link if new else mutation._exchange
    moved = source.root / "moved" if scope == "parent" else source.root.parent / "moved-project"

    def replace_context(*args, **kwargs):
        original(*args, **kwargs)
        current = source.root / "src" if scope == "parent" else source.root
        current.rename(moved)
        current.mkdir()

    monkeypatch.setattr(
        mutation.os if new else mutation, "link" if new else "_exchange", replace_context
    )
    with pytest.raises(FileMutationError) as error:
        commit_file(source, prepared, expected)
    assert_pending(error)
    parent = moved if scope == "parent" else moved / "src"
    assert (parent / path.rsplit("/", 1)[-1]).read_bytes() == b"new\n"
    assert (parent / prepared.temp_name).exists()


@pytest.mark.parametrize("operation", ["commit", "discard"])
@pytest.mark.parametrize("scope", ["parent", "source", "other_source", "parent_symlink"])
def test_binding_changes_before_mutation_are_rejected(source, tmp_path, operation, scope):
    (source.root / "src").mkdir()
    (source.root / "src/demo.py").write_bytes(b"original\n")
    expected = source.read("src/demo.py")
    prepared = prepare(source, path="src/demo.py")
    old_parent = source.root / "src"
    active = source
    if scope in {"parent", "parent_symlink"}:
        old_parent.rename(source.root / "saved-src")
        old_parent = source.root / "saved-src"
        if scope == "parent":
            (source.root / "src").mkdir()
        else:
            (source.root / "src").symlink_to("saved-src", target_is_directory=True)
    elif scope == "source":
        moved = source.root.parent / "saved-source"
        source.root.rename(moved)
        source.root.mkdir()
        old_parent = moved / "src"
    else:
        other = tmp_path / "other-project"
        other.mkdir()
        active = SourceAccess(other)
    with pytest.raises(FileMutationError) as error:
        if operation == "commit":
            commit_file(active, prepared, expected)
        else:
            discard_prepared(active, prepared, prepared.sha256, prepared.temp_identity)
    assert_before(error)
    assert (old_parent / prepared.temp_name).read_bytes() == b"new\n"
    assert (old_parent / "demo.py").read_bytes() == b"original\n"


def test_read_time_external_edit_fails_before_swap(source, monkeypatch):
    expected, prepared = source.read("demo.py"), prepare(source)
    read = mutation.os.read
    changed = False

    def during_read(fd, amount):
        nonlocal changed
        raw = read(fd, amount)
        if not changed and identity(temporary(source, prepared)) == (
            os.fstat(fd).st_dev,
            os.fstat(fd).st_ino,
        ):
            changed = True
            temporary(source, prepared).write_bytes(b"external during read")
        return raw

    monkeypatch.setattr(mutation.os, "read", during_read)
    with pytest.raises(FileMutationError, match="FILE_CHANGED") as error:
        commit_file(source, prepared, expected)
    assert_before(error)
    assert (source.root / "demo.py").read_bytes() == b"original\n"


@pytest.mark.parametrize("new", [False, True])
def test_unsupported_exchange_or_link_failure_never_uses_replace(source, monkeypatch, new):
    prepared = prepare(source, path="new.py" if new else "demo.py")
    expected = None if new else source.read("demo.py")

    def unsupported(*_, **__):
        raise FileMutationError("UNSUPPORTED_ATOMIC_SWAP: unavailable")

    def forbidden(*_, **__):
        pytest.fail("unsafe replace fallback")

    monkeypatch.setattr(mutation, "_exchange", unsupported)
    monkeypatch.setattr(mutation.os, "replace", forbidden)
    if new:
        monkeypatch.setattr(mutation.os, "link", unsupported)
    with pytest.raises(FileMutationError, match="UNSUPPORTED_ATOMIC_SWAP") as error:
        commit_file(source, prepared, expected)
    assert_before(error)
    assert (source.root / "demo.py").read_bytes() == b"original\n"
    assert temporary(source, prepared).read_bytes() == b"new\n"


@pytest.mark.parametrize("platform,symbol", [("darwin", "renameatx_np"), ("linux", "renameat2")])
def test_ctypes_exchange_uses_exact_five_argument_abi_and_flag_two(monkeypatch, platform, symbol):
    class Function:
        def __call__(self, *args):
            self.args = args
            return 0

    function = Function()
    seen = []
    monkeypatch.setattr(mutation.sys, "platform", platform)

    def library(name, **kwargs):
        seen.append((name, kwargs))
        return SimpleNamespace(**{symbol: function})

    monkeypatch.setattr(mutation.ctypes, "CDLL", library)
    mutation._exchange(7, ".colink-write-" + "a" * 32 + ".tmp", "中文.py")
    assert seen == [(None, {"use_errno": True})]
    assert function.args == (
        7,
        (".colink-write-" + "a" * 32 + ".tmp").encode(),
        7,
        "中文.py".encode(),
        2,
    )
    assert len(function.argtypes) == 5
    assert function.argtypes == [
        mutation.ctypes.c_int,
        mutation.ctypes.c_char_p,
        mutation.ctypes.c_int,
        mutation.ctypes.c_char_p,
        mutation.ctypes.c_uint,
    ]
    assert function.restype is mutation.ctypes.c_int


@pytest.mark.parametrize("failure", ["platform", "symbol", "enosys", "enotsup", "einval", "eperm"])
def test_missing_or_failed_native_exchange_is_explicit(monkeypatch, failure):
    class Function:
        def __call__(self, *_):
            return -1

    monkeypatch.setattr(
        mutation.sys, "platform", "unsupported" if failure == "platform" else "darwin"
    )
    library = SimpleNamespace() if failure == "symbol" else SimpleNamespace(renameatx_np=Function())
    monkeypatch.setattr(mutation.ctypes, "CDLL", lambda *_, **__: library)
    code = {"enosys": errno.ENOSYS, "enotsup": errno.ENOTSUP, "einval": errno.EINVAL}.get(
        failure, errno.EPERM
    )
    monkeypatch.setattr(mutation.ctypes, "get_errno", lambda: code)
    match = "ATOMIC_SWAP_FAILED" if failure == "eperm" else "UNSUPPORTED_ATOMIC_SWAP"
    with pytest.raises(FileMutationError, match=match) as error:
        mutation._exchange(7, "temp", "target")
    assert_before(error)


@pytest.mark.parametrize("committed", [False, True])
def test_discard_only_explicit_verified_temp_not_target(source, committed):
    expected, prepared = source.read("demo.py"), prepare(source)
    if committed:
        commit_file(source, prepared, expected)
    expected_sha = expected.sha256 if committed else prepared.sha256
    expected_identity = expected.version[:2] if committed else prepared.temp_identity
    discard_prepared(source, prepared, expected_sha, expected_identity)
    assert not temporary(source, prepared).exists()
    assert (source.root / "demo.py").read_bytes() == (b"new\n" if committed else b"original\n")
    discard_prepared(source, prepared, expected_sha, expected_identity)


@pytest.mark.parametrize(
    "change",
    ["body", "same_body_new_inode", "symlink", "third_link", "wrong_hash", "wrong_identity"],
)
def test_discard_rejects_unknown_or_changed_temp_and_preserves_it(source, change):
    prepared = prepare(source)
    path = temporary(source, prepared)
    expected_sha, expected_identity = prepared.sha256, prepared.temp_identity
    if change == "body":
        path.write_bytes(b"external")
    elif change in {"same_body_new_inode", "symlink"}:
        path.rename(source.root / "saved-temp.py")
        if change == "symlink":
            path.symlink_to("demo.py")
        else:
            path.write_bytes(b"new\n")
            path.chmod(prepared.mode)
    elif change == "third_link":
        os.link(path, source.root / "alias-one.py")
        os.link(path, source.root / "alias-two.py")
    elif change == "wrong_hash":
        expected_sha = sha(b"unknown")
    else:
        expected_identity = (expected_identity[0], expected_identity[1] + 1)
    with pytest.raises(FileMutationError) as error:
        discard_prepared(source, prepared, expected_sha, expected_identity)
    assert_before(error)
    assert path.exists()
    assert (source.root / "demo.py").read_bytes() == b"original\n"


def test_discard_two_links_requires_its_own_new_target_link(source):
    prepared = prepare(source)
    path = temporary(source, prepared)
    os.link(path, source.root / "unrelated.py")
    with pytest.raises(FileMutationError) as error:
        discard_prepared(source, prepared, prepared.sha256, prepared.temp_identity)
    assert_before(error)
    assert path.exists()
    assert (source.root / "unrelated.py").read_bytes() == b"new\n"


def test_discard_cannot_remove_a_two_link_displaced_original(source):
    expected, prepared = source.read("demo.py"), prepare(source)
    commit_file(source, prepared, expected)
    path = temporary(source, prepared)
    os.link(path, source.root / "external-original-alias.py")
    with pytest.raises(FileMutationError) as error:
        discard_prepared(source, prepared, expected.sha256, expected.version[:2])
    assert_before(error)
    assert path.read_bytes() == expected.content.encode()


def test_discard_rejects_replaced_installed_target_link(source):
    prepared = prepare(source, path="new.py")
    commit_file(source, prepared, None)
    target = source.root / "new.py"
    target.rename(source.root / "external-moved.py")
    target.write_bytes(b"new\n")
    target.chmod(prepared.mode)
    with pytest.raises(FileMutationError) as error:
        discard_prepared(source, prepared, prepared.sha256, prepared.temp_identity)
    assert_before(error)
    assert temporary(source, prepared).exists()
    assert target.read_bytes() == b"new\n"


def test_discard_checks_stable_version_again_immediately_before_unlink(source, monkeypatch):
    prepared = prepare(source)
    read = mutation._read_named

    def change_after_read(*args, **kwargs):
        actual = read(*args, **kwargs)
        temporary(source, prepared).chmod(0o600)
        return actual

    monkeypatch.setattr(mutation, "_read_named", change_after_read)
    with pytest.raises(FileMutationError, match="TEMP_CHANGED") as error:
        discard_prepared(source, prepared, prepared.sha256, prepared.temp_identity)
    assert_before(error)
    assert temporary(source, prepared).exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("temp_name", "demo.py"),
        ("temp_name", "../temp"),
        ("temp_identity", (1,)),
        ("parent_identity", (True, 1)),
        ("size", True),
        ("size", -1),
        ("mode", 0o1640),
        ("sha256", "invalid"),
    ],
)
@pytest.mark.parametrize("operation", ["commit", "discard"])
def test_invalid_prepared_metadata_never_mutates_entries(source, field, value, operation):
    expected, prepared = source.read("demo.py"), prepare(source)
    invalid = replace(prepared, **{field: value})
    with pytest.raises(FileMutationError, match="INVALID_PREPARED") as error:
        if operation == "commit":
            commit_file(source, invalid, expected)
        else:
            discard_prepared(source, invalid, prepared.sha256, prepared.temp_identity)
    assert_before(error)
    assert temporary(source, prepared).read_bytes() == b"new\n"
    assert (source.root / "demo.py").read_bytes() == b"original\n"


@pytest.mark.parametrize("expected", [False, {}, "invalid"])
def test_invalid_expected_document_rejects_before_commit(source, expected):
    prepared = prepare(source)
    with pytest.raises(FileMutationError, match="INVALID_EXPECTED") as error:
        commit_file(source, prepared, expected)
    assert_before(error)
    assert (source.root / "demo.py").read_bytes() == b"original\n"


@pytest.mark.parametrize(
    "expected_sha,expected_identity",
    [(None, (1, 2)), ("bad", (1, 2)), ("a" * 64, (1, 2, 3)), ("a" * 64, (False, 2))],
)
def test_invalid_discard_authority_cannot_remove_temp(source, expected_sha, expected_identity):
    prepared = prepare(source)
    with pytest.raises(FileMutationError, match="INVALID_DISCARD") as error:
        discard_prepared(source, prepared, expected_sha, expected_identity)
    assert_before(error)
    assert temporary(source, prepared).exists()


def test_errors_never_echo_path_content_or_underlying_callback_exception(source):
    raw, path = b"private content marker", "private-path-marker.py"

    def fail(_):
        raise RuntimeError(f"{path}: {raw!r}")

    with pytest.raises(FileMutationError) as error:
        prepare(source, path=path, raw=raw, callback=fail)
    assert_before(error)
    assert path not in str(error.value)
    assert raw.decode() not in str(error.value)
    assert error.value.__suppress_context__

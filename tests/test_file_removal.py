import os
import stat
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

import code_context.file_removal as mutation
from code_context.directory_mutation import DirectoryMutationError
from code_context.file_mutation import FileMutationError
from code_context.file_removal import (
    FileRemovalError,
    RemovedFile,
    discard_removed_file_temp,
    remove_created_file,
)
from code_context.policy import MAX_FILE_BYTES
from code_context.source_access import SourceAccess, SourceError


def identity(path):
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def temporary_name():
    return f".colink-write-{uuid4().hex}.tmp"


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "new.py").write_bytes(b"task created\n")
    (root / "new.py").chmod(0o640)
    (root / "unrelated.py").write_bytes(b"untouched\n")
    return SourceAccess(root)


def temporary(source, receipt):
    return (source.root / receipt.path).parent / receipt.temp_name


def assert_before(error):
    assert isinstance(error.value, (FileMutationError, SourceError))
    assert isinstance(error.value, FileRemovalError)
    assert error.value.may_have_committed is False


def assert_pending(error):
    assert isinstance(error.value, FileRemovalError)
    assert error.value.may_have_committed is True
    assert str(error.value).startswith("FILE_REMOVAL_PENDING:")


def isolate(source, path="new.py"):
    """Model a durably recorded receipt followed by a crashed move callback."""
    expected, name, recorded = source.read(path), temporary_name(), []

    def crash(receipt):
        recorded.append(receipt)
        raise RuntimeError("simulated journal interruption")

    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, name, crash)
    assert_pending(error)
    assert len(recorded) == 1
    assert not (source.root / path).exists()
    assert temporary(source, recorded[0]).read_bytes() == expected.content.encode()
    return recorded[0]


@pytest.mark.parametrize("raw", [b"", b"LF\n", b"CRLF\r\n", "\ufeff中文\r\n尾".encode()])
@pytest.mark.parametrize("mode", [0o400, 0o600, 0o640, 0o755])
def test_real_macos_exclusive_move_callback_and_verified_removal(source, raw, mode):
    target = source.root / "new.py"
    target.write_bytes(raw)
    target.chmod(mode)
    expected, recorded, name = source.read("new.py"), [], temporary_name()

    def on_moved(receipt):
        assert isinstance(receipt, RemovedFile)
        assert receipt.path == expected.path
        assert receipt.temp_name == name
        assert receipt.parent_identity == identity(source.root)
        assert receipt.file_identity == expected.version[:2] == identity(source.root / name)
        assert receipt.mode == mode
        assert receipt.sha256 == expected.sha256
        assert receipt.size == len(raw)
        assert receipt.source_id == source.source_id
        assert not target.exists()
        assert (source.root / name).read_bytes() == raw
        recorded.append(receipt)

    result = remove_created_file(source, expected, name, on_moved)
    assert result == recorded[0]
    assert not target.exists()
    assert not temporary(source, result).exists()
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"
    with pytest.raises(FrozenInstanceError):
        result.size = 1
    assert discard_removed_file_temp(source, result) is None


def test_nested_chinese_path_and_exact_max_file_size(source):
    (source.root / "src").mkdir()
    path = "src/中文.py"
    (source.root / path).write_bytes(b"x" * MAX_FILE_BYTES)
    expected, recorded = source.read(path), []
    receipt = remove_created_file(source, expected, temporary_name(), recorded.append)
    assert receipt.parent_identity == identity(source.root / "src")
    assert receipt.size == MAX_FILE_BYTES
    assert recorded == [receipt]
    assert not (source.root / path).exists()
    assert not temporary(source, receipt).exists()


def test_callback_crash_preserves_registered_file_and_safe_restart_cleanup(source):
    receipt = isolate(source)
    assert temporary(source, receipt).is_file()
    assert discard_removed_file_temp(source, receipt) is None
    assert not temporary(source, receipt).exists()
    assert discard_removed_file_temp(source, receipt) is None
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


@pytest.mark.parametrize("change", ["body", "inode", "metadata"])
def test_actual_full_hash_and_version_are_checked_before_move(source, change):
    expected, name, called = source.read("new.py"), temporary_name(), []
    target = source.root / expected.path
    if change == "body":
        target.write_bytes(b"external body\n")
    elif change == "inode":
        target.rename(source.root / "saved-original.py")
        target.write_bytes(expected.content.encode())
        target.chmod(expected.mode)
    else:
        target.chmod(0o600)
        target.chmod(expected.mode)
    with pytest.raises(FileRemovalError, match="SOURCE_CHANGED") as error:
        remove_created_file(source, expected, name, called.append)
    assert_before(error)
    assert not called
    assert target.exists()
    assert not (source.root / name).exists()


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "directory", "fifo", "special_mode", "missing"]
)
def test_unsafe_source_target_is_rejected_without_delete(source, kind):
    expected, name = source.read("new.py"), temporary_name()
    target = source.root / expected.path
    if kind == "hardlink":
        os.link(target, source.root / "alias.py")
    elif kind == "special_mode":
        target.chmod(0o2640)
    else:
        target.rename(source.root / "saved-original.py")
        if kind == "symlink":
            target.symlink_to("unrelated.py")
        elif kind == "directory":
            target.mkdir()
        elif kind == "fifo":
            os.mkfifo(target)
    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, name, lambda _: None)
    assert_before(error)
    assert not (source.root / name).exists()
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"
    if kind == "hardlink":
        assert (
            target.read_bytes()
            == (source.root / "alias.py").read_bytes()
            == expected.content.encode()
        )


def test_nonowned_file_is_rejected_before_move(source, monkeypatch):
    expected, name = source.read("new.py"), temporary_name()
    monkeypatch.setattr(mutation.os, "geteuid", lambda: source.root.stat().st_uid + 1)
    with pytest.raises(FileRemovalError, match="UNSAFE_FILE") as error:
        remove_created_file(source, expected, name, lambda _: None)
    assert_before(error)
    assert (source.root / expected.path).read_bytes() == expected.content.encode()
    assert not (source.root / name).exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
@pytest.mark.parametrize("race", [False, True])
def test_occupied_temp_is_never_overwritten_adopted_or_cleaned(source, monkeypatch, kind, race):
    expected, name, called = source.read("new.py"), temporary_name(), []
    temp, rename = source.root / name, mutation._rename_excl

    def create():
        if kind == "file":
            temp.write_bytes(b"external temp")
        elif kind == "directory":
            temp.mkdir()
        else:
            temp.symlink_to("unrelated.py")

    if race:

        def competing(*args):
            create()
            rename(*args)

        monkeypatch.setattr(mutation, "_rename_excl", competing)
    else:
        create()
    with pytest.raises(FileRemovalError, match="DESTINATION_EXISTS") as error:
        remove_created_file(source, expected, name, called.append)
    assert_before(error)
    assert not called
    assert (source.root / expected.path).read_bytes() == expected.content.encode()
    assert temp.exists()
    if kind == "file":
        assert temp.read_bytes() == b"external temp"
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


@pytest.mark.parametrize("change", ["body", "inode", "mode", "symlink", "hardlink", "oversized"])
def test_move_window_external_changes_preserve_captured_object_pending(source, monkeypatch, change):
    expected, name, recorded = source.read("new.py"), temporary_name(), []
    target, rename = source.root / expected.path, mutation._rename_excl

    def race(*args):
        if change == "body":
            target.write_bytes(b"external captured content\n")
        elif change == "mode":
            target.chmod(0o600)
        elif change == "hardlink":
            os.link(target, source.root / "external-alias.py")
        elif change == "oversized":
            target.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        else:
            target.rename(source.root / "saved-original.py")
            if change == "inode":
                target.write_bytes(expected.content.encode())
                target.chmod(expected.mode)
            else:
                target.symlink_to("unrelated.py")
        rename(*args)

    monkeypatch.setattr(mutation, "_rename_excl", race)
    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, name, recorded.append)
    assert_pending(error)
    assert len(recorded) == 1
    assert recorded[0].file_identity == expected.version[:2]
    assert not target.exists()
    temp = source.root / name
    assert temp.exists()
    if change == "body":
        assert temp.read_bytes() == b"external captured content\n"
    elif change == "inode":
        assert temp.read_bytes() == expected.content.encode()
        assert identity(temp) != expected.version[:2]
        assert (source.root / "saved-original.py").read_bytes() == expected.content.encode()
    elif change == "symlink":
        assert temp.is_symlink()
    elif change == "mode":
        assert stat.S_IMODE(temp.stat().st_mode) == 0o600
    elif change == "oversized":
        assert temp.stat().st_size == MAX_FILE_BYTES + 1
    else:
        assert temp.stat().st_nlink == 2
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_target_recreated_after_move_is_untouched_and_capture_is_preserved(source, kind):
    expected, name, recorded = source.read("new.py"), temporary_name(), []

    def create_target(receipt):
        recorded.append(receipt)
        target = source.root / receipt.path
        if kind == "file":
            target.write_bytes(b"external new target")
        elif kind == "directory":
            target.mkdir()
        else:
            target.symlink_to("unrelated.py")

    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, name, create_target)
    assert_pending(error)
    assert len(recorded) == 1
    assert (source.root / name).read_bytes() == expected.content.encode()
    target = source.root / expected.path
    assert target.exists()
    if kind == "file":
        assert target.read_bytes() == b"external new target"
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


@pytest.mark.parametrize("failure", ["native_return", "move_fsync", "unlink_fsync"])
def test_post_move_failure_always_pending_and_never_reverses(source, monkeypatch, failure):
    expected, name, recorded = source.read("new.py"), temporary_name(), []
    if failure == "native_return":
        rename = mutation._rename_excl

        def fail(*args):
            rename(*args)
            raise RuntimeError("private syscall path")

        monkeypatch.setattr(mutation, "_rename_excl", fail)
    else:
        fsync, count = mutation.os.fsync, 0

        def fail(fd):
            nonlocal count
            count += 1
            if count == (1 if failure == "move_fsync" else 2):
                raise OSError("private fsync path")
            fsync(fd)

        monkeypatch.setattr(mutation.os, "fsync", fail)
    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, name, recorded.append)
    assert_pending(error)
    assert not (source.root / expected.path).exists()
    assert (
        (source.root / name).exists()
        if failure != "unlink_fsync"
        else not (source.root / name).exists()
    )
    assert len(recorded) == (0 if failure == "native_return" else 1)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("scope", ["parent", "source"])
def test_source_parent_replaced_after_move_preserves_isolated_material(source, tmp_path, scope):
    (source.root / "src").mkdir()
    path = "src/new.py"
    (source.root / path).write_bytes(b"nested task file")
    expected, name, recorded = source.read(path), temporary_name(), []
    saved = source.root / "saved-src" if scope == "parent" else tmp_path / "saved-source"

    def replace_context(receipt):
        recorded.append(receipt)
        current = (source.root / path).parent if scope == "parent" else source.root
        current.rename(saved)
        current.mkdir()

    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, name, replace_context)
    assert_pending(error)
    assert len(recorded) == 1
    parent = saved if scope == "parent" else saved / "src"
    assert (parent / name).read_bytes() == expected.content.encode()
    assert not (parent / "new.py").exists()


@pytest.mark.parametrize("scope", ["parent", "source", "parent_symlink"])
def test_pre_move_source_or_parent_substitution_cannot_remove_other_object(source, tmp_path, scope):
    (source.root / "src").mkdir()
    (source.root / "src/new.py").write_bytes(b"nested task file")
    expected, name = source.read("src/new.py"), temporary_name()
    if scope == "source":
        saved = tmp_path / "saved-source"
        source.root.rename(saved)
        source.root.mkdir()
        parent = saved / "src"
    else:
        parent = source.root / "saved-src"
        (source.root / "src").rename(parent)
        if scope == "parent":
            (source.root / "src").mkdir()
            (source.root / "src/new.py").write_bytes(b"external")
        else:
            (source.root / "src").symlink_to("unrelated.py")
    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, name, lambda _: None)
    assert_before(error)
    assert (parent / "new.py").read_bytes() == expected.content.encode()
    assert not (parent / name).exists()


@pytest.mark.parametrize("change", ["body", "inode", "mode", "symlink", "hardlink", "directory"])
def test_restart_cleanup_preserves_unknown_or_changed_isolated_temp(source, change):
    receipt = isolate(source)
    path = temporary(source, receipt)
    if change == "body":
        path.write_bytes(b"external modified capture")
    elif change == "mode":
        path.chmod(0o600)
    elif change == "hardlink":
        os.link(path, source.root / "external-alias.py")
    else:
        path.rename(source.root / "saved-capture.py")
        if change == "inode":
            path.write_bytes(b"task created\n")
            path.chmod(receipt.mode)
        elif change == "symlink":
            path.symlink_to("unrelated.py")
        else:
            path.mkdir()
    with pytest.raises(FileRemovalError) as error:
        discard_removed_file_temp(source, receipt)
    assert_pending(error)
    assert path.exists()
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_restart_missing_temp_is_not_success_when_target_reappeared(source, missing, kind):
    receipt = isolate(source)
    if missing:
        discard_removed_file_temp(source, receipt)
    target = source.root / receipt.path
    if kind == "file":
        target.write_bytes(b"external new target")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to("unrelated.py")
    with pytest.raises(FileRemovalError) as error:
        discard_removed_file_temp(source, receipt)
    assert_pending(error)
    assert target.exists()
    assert temporary(source, receipt).exists() is not missing
    if kind == "file":
        assert target.read_bytes() == b"external new target"


@pytest.mark.parametrize("scope", ["parent", "source", "other_source", "parent_symlink"])
def test_restart_receipt_source_parent_bindings_are_not_transferable(source, tmp_path, scope):
    (source.root / "src").mkdir()
    (source.root / "src/new.py").write_bytes(b"nested task file")
    receipt, active = isolate(source, "src/new.py"), source
    parent = source.root / "src"
    if scope == "source":
        saved = tmp_path / "saved-source"
        source.root.rename(saved)
        source.root.mkdir()
        parent = saved / "src"
    elif scope == "other_source":
        other = tmp_path / "other-project"
        other.mkdir()
        active = SourceAccess(other)
    else:
        parent.rename(source.root / "saved-src")
        parent = source.root / "saved-src"
        if scope == "parent":
            (source.root / "src").mkdir()
        else:
            (source.root / "src").symlink_to("saved-src", target_is_directory=True)
    with pytest.raises(FileRemovalError) as error:
        discard_removed_file_temp(active, receipt)
    assert_before(error) if scope == "other_source" else assert_pending(error)
    assert (parent / receipt.temp_name).read_bytes() == b"nested task file"


@pytest.mark.parametrize("phase", ["after_read", "after_last_check"])
def test_stable_temp_checks_detect_observable_edit_and_preserve_capture(source, monkeypatch, phase):
    receipt = isolate(source)
    path = temporary(source, receipt)
    if phase == "after_read":
        read = mutation._read_opened

        def change(*args, **kwargs):
            actual = read(*args, **kwargs)
            path.write_bytes(b"changed after full read")
            return actual

        monkeypatch.setattr(mutation, "_read_opened", change)
    else:
        absent, calls = mutation._target_absent, 0

        def change(parent, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                (source.root / receipt.path).write_bytes(b"external late target")
            absent(parent, target)

        monkeypatch.setattr(mutation, "_target_absent", change)
    with pytest.raises(FileRemovalError) as error:
        discard_removed_file_temp(source, receipt)
    assert_pending(error)
    assert path.exists()
    if phase == "after_read":
        assert path.read_bytes() == b"changed after full read"
    else:
        assert (source.root / receipt.path).read_bytes() == b"external late target"


@pytest.mark.parametrize(
    "phase", ["unlink_error", "unlink_return_error", "new_target_after_unlink"]
)
def test_unlink_boundary_outcomes_pending_and_external_target_never_touched(
    source, monkeypatch, phase
):
    receipt, unlink = isolate(source), mutation.os.unlink
    path = temporary(source, receipt)

    def change(*args, **kwargs):
        if phase == "unlink_error":
            raise OSError("private unlink pathname")
        unlink(*args, **kwargs)
        if phase == "unlink_return_error":
            raise RuntimeError("private unlink pathname")
        (source.root / receipt.path).write_bytes(b"external late target")

    monkeypatch.setattr(mutation.os, "unlink", change)
    with pytest.raises(FileRemovalError) as error:
        discard_removed_file_temp(source, receipt)
    assert_pending(error)
    assert path.exists() if phase == "unlink_error" else not path.exists()
    if phase == "new_target_after_unlink":
        assert (source.root / receipt.path).read_bytes() == b"external late target"
    assert "private unlink" not in str(error.value)


def test_cleanup_held_fd_postcondition_does_not_claim_success_for_noop_unlink(source, monkeypatch):
    receipt = isolate(source)
    monkeypatch.setattr(mutation.os, "unlink", lambda *_, **__: None)
    with pytest.raises(FileRemovalError) as error:
        discard_removed_file_temp(source, receipt)
    assert_pending(error)
    assert temporary(source, receipt).read_bytes() == b"task created\n"


def test_cleanup_read_time_change_is_detected_without_source_body_cache(source, monkeypatch):
    receipt = isolate(source)
    path, read, changed = temporary(source, receipt), mutation.os.read, False

    def change(fd, size):
        nonlocal changed
        chunk = read(fd, size)
        if not changed and (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == receipt.file_identity:
            changed = True
            path.write_bytes(b"external during read")
        return chunk

    monkeypatch.setattr(mutation.os, "read", change)
    with pytest.raises(FileRemovalError) as error:
        discard_removed_file_temp(source, receipt)
    assert_pending(error)
    assert path.read_bytes() == b"external during read"


@pytest.mark.parametrize(
    "field,value",
    [
        ("temp_name", "new.py"),
        ("temp_name", "../temp"),
        ("parent_identity", (True, 1)),
        ("file_identity", (1, 2, 3)),
        ("mode", True),
        ("mode", 0o1640),
        ("size", -1),
        ("size", False),
        ("sha256", "bad"),
        ("path", None),
    ],
)
def test_invalid_receipt_cannot_delete_anything(source, field, value):
    receipt = isolate(source)
    with pytest.raises(FileRemovalError, match="INVALID_RECEIPT") as error:
        discard_removed_file_temp(source, replace(receipt, **{field: value}))
    assert_before(error)
    assert temporary(source, receipt).read_bytes() == b"task created\n"


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", None),
        ("content", None),
        ("content", "mismatch"),
        ("content", "\ud800"),
        ("mode", True),
        ("mode", 0o1640),
        ("size", True),
        ("size", MAX_FILE_BYTES + 1),
        ("sha256", "bad"),
        ("version", (1, 2)),
        ("version", (True, 2, 3, 4, 5, 6)),
    ],
)
def test_invalid_expected_document_is_rejected_before_move(source, field, value):
    expected, name = source.read("new.py"), temporary_name()
    with pytest.raises(FileRemovalError, match="INVALID_EXPECTED") as error:
        remove_created_file(source, replace(expected, **{field: value}), name, lambda _: None)
    assert_before(error)
    assert (source.root / expected.path).read_bytes() == expected.content.encode()
    assert not (source.root / name).exists()


@pytest.mark.parametrize("expected", [None, {}, False, "invalid"])
def test_expected_must_be_source_document(source, expected):
    with pytest.raises(FileRemovalError, match="INVALID_EXPECTED") as error:
        remove_created_file(source, expected, temporary_name(), lambda _: None)
    assert_before(error)
    assert (source.root / "new.py").exists()


@pytest.mark.parametrize(
    "name",
    [
        "temp",
        ".colink-write-a.tmp",
        ".colink-write-" + "A" * 32 + ".tmp",
        ".colink-write-" + "a" * 31 + ".tmp",
        ".colink-write-" + "a" * 33 + ".tmp",
        ".colink-write-" + "a" * 32 + ".tmp\n",
        "../temp",
        None,
    ],
)
def test_only_exact_private_temp_basename_is_accepted(source, name):
    expected = source.read("new.py")
    with pytest.raises(FileRemovalError, match="INVALID_REMOVE") as error:
        remove_created_file(source, expected, name, lambda _: None)
    assert_before(error)
    assert (source.root / expected.path).read_bytes() == expected.content.encode()


@pytest.mark.parametrize("path", ["../elsewhere.py", "/absolute.py", ".env", "missing/new.py", ""])
def test_source_path_policy_and_existing_ancestors_are_not_bypassed(source, path):
    expected = replace(source.read("new.py"), path=path)
    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, temporary_name(), lambda _: None)
    assert_before(error)
    assert (source.root / "new.py").read_bytes() == b"task created\n"
    assert not (source.root / "missing").exists()


def test_unavailable_safe_noreplace_is_explicit_and_never_uses_replace(source, monkeypatch):
    expected, name = source.read("new.py"), temporary_name()

    def unsupported(*_):
        raise DirectoryMutationError("UNSUPPORTED_ATOMIC_NOREPLACE: not available")

    def forbidden(*_, **__):
        pytest.fail("unsafe rename or replace fallback")

    monkeypatch.setattr(mutation, "_rename_excl", unsupported)
    monkeypatch.setattr(mutation.os, "rename", forbidden)
    monkeypatch.setattr(mutation.os, "replace", forbidden)
    with pytest.raises(FileRemovalError, match="UNSUPPORTED_ATOMIC_NOREPLACE") as error:
        remove_created_file(source, expected, name, lambda _: None)
    assert_before(error)
    assert (source.root / expected.path).read_bytes() == expected.content.encode()
    assert not (source.root / name).exists()


def test_errors_hide_input_and_callback_content_without_claiming_cleanup(source):
    path = "private-path-marker.py"
    (source.root / path).write_bytes(b"private content marker\n")
    expected, name, recorded = source.read(path), temporary_name(), []

    def fail(receipt):
        recorded.append(receipt)
        raise RuntimeError(f"{path}: {expected.content}")

    with pytest.raises(FileRemovalError) as error:
        remove_created_file(source, expected, name, fail)
    assert_pending(error)
    assert path not in str(error.value)
    assert expected.content not in str(error.value)
    assert name not in str(error.value)
    assert error.value.__suppress_context__
    assert temporary(source, recorded[0]).read_bytes() == expected.content.encode()

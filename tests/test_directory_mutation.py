import errno
import os
import stat
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

import code_context.directory_mutation as mutation
from code_context.directory_mutation import (
    DirectoryMutationError,
    DirectoryReceipt,
    PreparedDirectory,
    commit_directory,
    discard_directory_temp,
    prepare_directory,
    remove_created_directory,
)
from code_context.file_mutation import FileMutationError
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
    (root / "unrelated.py").write_bytes(b"untouched\n")
    return SourceAccess(root)


def prepare(source, path="created", mode=0o755, name=None, callback=None):
    return prepare_directory(
        source, path, mode, name or temporary_name(), callback or (lambda _: None)
    )


def temporary(source, prepared):
    return (source.root / prepared.path).parent / prepared.temp_name


def install(source, path="created", mode=0o755):
    return commit_directory(source, prepare(source, path, mode))


def assert_before(error):
    assert isinstance(error.value, (FileMutationError, SourceError))
    assert isinstance(error.value, DirectoryMutationError)
    assert error.value.may_have_committed is False


def assert_pending(error):
    assert error.value.may_have_committed is True
    assert str(error.value).startswith("DIRECTORY_PENDING:")


@pytest.mark.parametrize("mode", [0o500, 0o700, 0o750, 0o755, 0o777])
def test_prepare_private_creation_immediate_registration_exact_mode_and_frozen(source, mode):
    recorded, name = [], temporary_name()

    def on_created(prepared):
        path = source.root / name
        assert path.is_dir()
        assert not list(path.iterdir())
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert prepared.directory_identity == identity(path)
        assert prepared.parent_identity == identity(source.root)
        assert prepared.mode == mode
        assert not (source.root / prepared.path).exists()
        recorded.append(prepared)

    prepared = prepare(source, mode=mode, name=name, callback=on_created)
    assert isinstance(prepared, PreparedDirectory)
    assert recorded == [prepared]
    path = temporary(source, prepared)
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert path.stat().st_uid == os.geteuid()
    assert identity(path) == prepared.directory_identity
    assert prepared.source_id == source.source_id
    assert not list(path.iterdir())
    assert source.manifest()["watch_directories"] == [""]
    with pytest.raises(FrozenInstanceError):
        prepared.mode = 0o700


@pytest.mark.parametrize("mode", [0, 0o200])
def test_unreadable_modes_are_not_rejected_as_parameters_or_widened(source, mode):
    recorded = []
    try:
        prepared = prepare(source, mode=mode, callback=recorded.append)
        assert prepared == recorded[0]
    except DirectoryMutationError as exc:
        assert exc.may_have_committed is False
        assert not str(exc).startswith("INVALID_PREPARE:")
    assert len(recorded) == 1
    assert stat.S_IMODE(temporary(source, recorded[0]).stat().st_mode) == mode


@pytest.mark.parametrize(
    "mode", [True, False, -1, 0o1000, 0o1644, 0o2644, 0o4644, "755", 0.0, None]
)
def test_invalid_or_special_modes_create_nothing(source, mode):
    name = temporary_name()
    with pytest.raises(DirectoryMutationError, match="INVALID_PREPARE") as error:
        prepare(source, mode=mode, name=name)
    assert_before(error)
    assert not (source.root / name).exists()


@pytest.mark.parametrize(
    "name",
    [
        "temp",
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
def test_exact_lowercase_temp_basename_is_mandatory(source, name):
    with pytest.raises(DirectoryMutationError, match="INVALID_PREPARE") as error:
        prepare_directory(source, "created", 0o755, name, lambda _: None)
    assert_before(error)


@pytest.mark.parametrize(
    "path", ["../elsewhere", "/absolute", ".git", "node_modules", "missing/child", "", None]
)
def test_directory_policy_and_existing_ancestors_are_required(source, path):
    with pytest.raises(DirectoryMutationError) as error:
        prepare(source, path=path)
    assert_before(error)
    assert list(source.root.iterdir()) == [source.root / "unrelated.py"]


def test_directory_ignore_rules_are_applied_with_directory_flag(source):
    (source.root / ".gitignore").write_text("blocked/\n")
    with pytest.raises(DirectoryMutationError) as error:
        prepare(source, path="blocked")
    assert_before(error)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_mkdir_temp_collision_never_adopts_overwrites_or_follows(source, kind):
    name, called = temporary_name(), []
    path = source.root / name
    if kind == "directory":
        path.mkdir()
        (path / "external.py").write_bytes(b"external")
    elif kind == "file":
        path.write_bytes(b"external")
    else:
        path.symlink_to("unrelated.py")
    old_identity = identity(path)
    with pytest.raises(DirectoryMutationError) as error:
        prepare(source, name=name, callback=called.append)
    assert_before(error)
    assert not called
    assert identity(path) == old_identity
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


@pytest.mark.parametrize("failure", ["callback", "chmod", "temp_fsync", "parent_fsync"])
def test_prepare_failure_preserves_registered_empty_temp_and_safe_error(
    source, monkeypatch, failure
):
    recorded, name = [], temporary_name()

    def fail(*_):
        raise OSError("private input path")

    def callback(prepared):
        recorded.append(prepared)
        if failure == "callback":
            fail()

    if failure == "chmod":
        monkeypatch.setattr(mutation.os, "fchmod", fail)
    elif "fsync" in failure:
        fsync = mutation.os.fsync

        def selective(fd):
            parent = (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == identity(source.root)
            if parent == (failure == "parent_fsync"):
                fail()
            fsync(fd)

        monkeypatch.setattr(mutation.os, "fsync", selective)
    with pytest.raises(DirectoryMutationError) as error:
        prepare(source, name=name, callback=callback)
    assert_before(error)
    assert len(recorded) == 1
    assert identity(source.root / name) == recorded[0].directory_identity
    assert not list((source.root / name).iterdir())
    assert "private input" not in str(error.value)
    assert name not in str(error.value)


def test_registered_pre_chmod_failure_can_be_explicitly_discarded(source):
    recorded = []

    def fail(prepared):
        recorded.append(prepared)
        raise RuntimeError("durable callback unavailable")

    with pytest.raises(DirectoryMutationError):
        prepare(source, callback=fail)
    prepared = recorded[0]
    assert stat.S_IMODE(temporary(source, prepared).stat().st_mode) == 0o700
    discard_directory_temp(source, prepared)
    assert not temporary(source, prepared).exists()


@pytest.mark.parametrize("change", ["content", "inode", "symlink"])
def test_prepare_callback_substitution_or_contents_are_preserved(source, change):
    recorded = []

    def callback(prepared):
        recorded.append(prepared)
        path = temporary(source, prepared)
        if change == "content":
            (path / "external.py").write_bytes(b"external")
        else:
            path.rename(source.root / "saved-temp")
            if change == "inode":
                path.mkdir()
            else:
                path.symlink_to("saved-temp", target_is_directory=True)

    with pytest.raises(DirectoryMutationError) as error:
        prepare(source, callback=callback)
    assert_before(error)
    path = temporary(source, recorded[0])
    assert path.exists()
    if change == "content":
        assert (path / "external.py").read_bytes() == b"external"
    else:
        assert identity(source.root / "saved-temp") == recorded[0].directory_identity


@pytest.mark.parametrize("path", ["created", "中文目录", "src/child"])
def test_real_native_commit_returns_verified_frozen_receipt(source, path):
    (source.root / "src").mkdir()
    prepared = prepare(source, path=path)
    receipt = commit_directory(source, prepared)
    assert isinstance(receipt, DirectoryReceipt)
    assert receipt.path == path
    assert receipt.parent_identity == prepared.parent_identity
    assert receipt.directory_identity == identity(source.root / path) == prepared.directory_identity
    assert receipt.mode == stat.S_IMODE((source.root / path).stat().st_mode) == prepared.mode
    assert receipt.source_id == source.source_id
    assert not temporary(source, prepared).exists()
    assert not list((source.root / path).iterdir())
    with pytest.raises(FrozenInstanceError):
        receipt.mode = 0o700
    discard_directory_temp(source, prepared)
    assert (source.root / path).is_dir()


@pytest.mark.parametrize(
    "kind", ["empty_directory", "nonempty_directory", "file", "symlink", "fifo"]
)
def test_existing_target_is_never_adopted_or_overwritten(source, kind):
    target = source.root / "created"
    if "directory" in kind:
        target.mkdir()
        if kind == "nonempty_directory":
            (target / "external.py").write_bytes(b"external")
    elif kind == "file":
        target.write_bytes(b"external")
    elif kind == "symlink":
        target.symlink_to("unrelated.py")
    else:
        os.mkfifo(target)
    old_identity, prepared = identity(target), prepare(source)
    with pytest.raises(DirectoryMutationError, match="DESTINATION_EXISTS") as error:
        commit_directory(source, prepared)
    assert_before(error)
    assert identity(target) == old_identity
    assert identity(temporary(source, prepared)) == prepared.directory_identity
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


def test_directory_hardlinks_are_not_supported_or_treated_as_single_link_files(source):
    prepared = prepare(source)
    directory = temporary(source, prepared)
    with pytest.raises(OSError):
        os.link(directory, source.root / "alias")
    assert not (source.root / "alias").exists()
    receipt = commit_directory(source, prepared)
    assert receipt.directory_identity == identity(source.root / "created")


def test_destination_race_is_atomically_rejected_without_adoption(source, monkeypatch):
    prepared, rename = prepare(source), mutation._rename_excl
    external = []

    def competing(*args):
        target = source.root / "created"
        target.mkdir()
        external.append(identity(target))
        rename(*args)

    monkeypatch.setattr(mutation, "_rename_excl", competing)
    with pytest.raises(DirectoryMutationError, match="DESTINATION_EXISTS") as error:
        commit_directory(source, prepared)
    assert_before(error)
    assert identity(source.root / "created") == external[0]
    assert temporary(source, prepared).is_dir()


@pytest.mark.parametrize("change", ["contents", "inode", "mode", "symlink", "file"])
def test_changed_temp_rejects_before_commit_and_is_not_removed(source, change):
    prepared = prepare(source)
    path = temporary(source, prepared)
    if change == "contents":
        (path / "external.py").write_bytes(b"external")
    elif change == "mode":
        path.chmod(0o750)
    else:
        path.rename(source.root / "saved-temp")
        if change == "inode":
            path.mkdir(mode=prepared.mode)
        elif change == "symlink":
            path.symlink_to("saved-temp", target_is_directory=True)
        else:
            path.write_bytes(b"external")
    with pytest.raises(DirectoryMutationError) as error:
        commit_directory(source, prepared)
    assert_before(error)
    assert path.exists()
    assert not (source.root / "created").exists()


@pytest.mark.parametrize("change", ["contents", "inode", "symlink"])
def test_check_to_commit_move_substitution_or_contents_are_pending(source, monkeypatch, change):
    prepared, rename = prepare(source), mutation._rename_excl

    def race(*args):
        path = temporary(source, prepared)
        if change == "contents":
            (path / "external.py").write_bytes(b"external")
        else:
            path.rename(source.root / "saved-temp")
            if change == "inode":
                path.mkdir(mode=prepared.mode)
            else:
                path.symlink_to("saved-temp", target_is_directory=True)
        rename(*args)

    monkeypatch.setattr(mutation, "_rename_excl", race)
    with pytest.raises(DirectoryMutationError) as error:
        commit_directory(source, prepared)
    assert_pending(error)
    assert (source.root / "created").exists()
    if change == "contents":
        assert (source.root / "created/external.py").read_bytes() == b"external"
    else:
        assert identity(source.root / "saved-temp") == prepared.directory_identity


@pytest.mark.parametrize("failure", ["fsync", "native_return", "new_contents"])
def test_commit_post_move_failures_are_pending_without_reverse(source, monkeypatch, failure):
    prepared, rename = prepare(source), mutation._rename_excl
    if failure == "fsync":

        def fail(_):
            raise OSError("private path")

        monkeypatch.setattr(mutation.os, "fsync", fail)
    else:

        def after(*args):
            rename(*args)
            if failure == "native_return":
                raise RuntimeError("private path")
            (source.root / "created/external.py").write_bytes(b"external")

        monkeypatch.setattr(mutation, "_rename_excl", after)
    with pytest.raises(DirectoryMutationError) as error:
        commit_directory(source, prepared)
    assert_pending(error)
    assert (source.root / "created").is_dir()
    assert not temporary(source, prepared).exists()
    assert "private path" not in str(error.value)


@pytest.mark.parametrize("scope", ["parent", "source", "other_source", "parent_symlink"])
@pytest.mark.parametrize("operation", ["commit", "discard"])
def test_source_parent_bindings_reject_changes_before_operation(source, tmp_path, scope, operation):
    (source.root / "src").mkdir()
    prepared = prepare(source, "src/child")
    parent, active = source.root / "src", source
    if scope in {"parent", "parent_symlink"}:
        parent.rename(source.root / "saved-src")
        parent = source.root / "saved-src"
        if scope == "parent":
            (source.root / "src").mkdir()
        else:
            (source.root / "src").symlink_to("saved-src", target_is_directory=True)
    elif scope == "source":
        moved = tmp_path / "saved-source"
        source.root.rename(moved)
        source.root.mkdir()
        parent = moved / "src"
    else:
        other = tmp_path / "other-project"
        other.mkdir()
        active = SourceAccess(other)
    with pytest.raises(DirectoryMutationError) as error:
        if operation == "commit":
            commit_directory(active, prepared)
        else:
            discard_directory_temp(active, prepared)
    assert_before(error)
    assert identity(parent / prepared.temp_name) == prepared.directory_identity
    assert not (parent / "child").exists()


@pytest.mark.parametrize("scope", ["parent", "source"])
@pytest.mark.parametrize("operation", ["commit", "remove"])
def test_parent_source_context_exit_after_move_is_pending(
    source, tmp_path, monkeypatch, scope, operation
):
    (source.root / "src").mkdir()
    if operation == "commit":
        prepared = prepare(source, "src/child")
    else:
        receipt = install(source, "src/child")
    moved = source.root / "saved-src" if scope == "parent" else tmp_path / "saved-source"
    rename, name = mutation._rename_excl, temporary_name()
    recorded = []

    def replace_context(*args):
        rename(*args)
        current = source.root / "src" if scope == "parent" else source.root
        current.rename(moved)
        current.mkdir()

    monkeypatch.setattr(mutation, "_rename_excl", replace_context)
    with pytest.raises(DirectoryMutationError) as error:
        if operation == "commit":
            commit_directory(source, prepared)
        else:
            remove_created_directory(
                source, "src/child", receipt.directory_identity, receipt.mode, name, recorded.append
            )
    assert_pending(error)
    parent = moved if scope == "parent" else moved / "src"
    assert (parent / "child").is_dir() if operation == "commit" else not (parent / "child").exists()
    if operation == "remove":
        assert len(recorded) == 1
        assert identity(parent / name) == receipt.directory_identity


def test_nonowned_directory_is_rejected_before_namespace_changes(source, monkeypatch):
    prepared = prepare(source)
    inspect = mutation._stat

    def foreign(parent, name):
        info = inspect(parent, name)
        if info is not None and name == prepared.temp_name:
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(mutation, "_stat", foreign)
    with pytest.raises(DirectoryMutationError, match="UNSAFE_DIRECTORY") as error:
        commit_directory(source, prepared)
    assert_before(error)
    assert temporary(source, prepared).exists()


def test_discard_removes_only_registered_empty_temp_and_is_idempotent(source):
    prepared = prepare(source)
    discard_directory_temp(source, prepared)
    assert not temporary(source, prepared).exists()
    assert discard_directory_temp(source, prepared) is None
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


@pytest.mark.parametrize("change", ["contents", "inode", "mode", "symlink", "file"])
def test_discard_unknown_or_changed_temp_is_preserved(source, change):
    prepared = prepare(source)
    path = temporary(source, prepared)
    if change == "contents":
        (path / "external.py").write_bytes(b"external")
    elif change == "mode":
        path.chmod(0o750)
    else:
        path.rename(source.root / "saved-temp")
        if change == "inode":
            path.mkdir(mode=prepared.mode)
        elif change == "symlink":
            path.symlink_to("saved-temp", target_is_directory=True)
        else:
            path.write_bytes(b"external")
    with pytest.raises(DirectoryMutationError) as error:
        discard_directory_temp(source, prepared)
    assert_before(error)
    assert path.exists()


def test_discard_content_in_final_rmdir_window_is_not_deleted(source, monkeypatch):
    prepared, rmdir = prepare(source), mutation.os.rmdir
    path = temporary(source, prepared)

    def race(*args, **kwargs):
        (path / "external.py").write_bytes(b"external")
        rmdir(*args, **kwargs)

    monkeypatch.setattr(mutation.os, "rmdir", race)
    with pytest.raises(DirectoryMutationError) as error:
        discard_directory_temp(source, prepared)
    assert_before(error)
    assert (path / "external.py").read_bytes() == b"external"


def test_cleanup_exception_after_actual_rmdir_is_pending(source, monkeypatch):
    prepared, rmdir = prepare(source), mutation.os.rmdir

    def remove_then_fail(*args, **kwargs):
        rmdir(*args, **kwargs)
        raise RuntimeError("private rmdir path")

    monkeypatch.setattr(mutation.os, "rmdir", remove_then_fail)
    with pytest.raises(DirectoryMutationError) as error:
        discard_directory_temp(source, prepared)
    assert_pending(error)
    assert not temporary(source, prepared).exists()
    assert "private rmdir" not in str(error.value)


def test_remove_isolates_records_verifies_and_removes_only_created_empty_directory(source):
    receipt, recorded, name = install(source), [], temporary_name()

    def on_moved(prepared):
        assert isinstance(prepared, PreparedDirectory)
        assert prepared.path == receipt.path
        assert prepared.directory_identity == receipt.directory_identity
        assert prepared.parent_identity == receipt.parent_identity
        assert prepared.source_id == receipt.source_id
        assert not (source.root / receipt.path).exists()
        assert identity(source.root / name) == receipt.directory_identity
        recorded.append(prepared)

    assert (
        remove_created_directory(
            source, receipt.path, receipt.directory_identity, receipt.mode, name, on_moved
        )
        is None
    )
    assert len(recorded) == 1
    assert not (source.root / receipt.path).exists()
    assert not (source.root / name).exists()
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"
    assert discard_directory_temp(source, recorded[0]) is None


@pytest.mark.parametrize("change", ["contents", "identity", "mode", "symlink", "file", "missing"])
def test_remove_preconditions_reject_without_moving_or_deleting(source, change):
    receipt, name, recorded = install(source), temporary_name(), []
    target = source.root / receipt.path
    expected_identity, expected_mode = receipt.directory_identity, receipt.mode
    if change == "contents":
        (target / "external.py").write_bytes(b"external")
    elif change == "identity":
        expected_identity = (expected_identity[0], expected_identity[1] + 1)
    elif change == "mode":
        expected_mode = 0o750
    else:
        target.rename(source.root / "saved-created")
        if change == "symlink":
            target.symlink_to("saved-created", target_is_directory=True)
        elif change == "file":
            target.write_bytes(b"external")
    with pytest.raises(DirectoryMutationError) as error:
        remove_created_directory(
            source, receipt.path, expected_identity, expected_mode, name, recorded.append
        )
    assert_before(error)
    assert not recorded
    assert not (source.root / name).exists()
    if change == "contents":
        assert (target / "external.py").read_bytes() == b"external"


@pytest.mark.parametrize("race", [False, True])
def test_remove_never_overwrites_occupied_isolation_temp(source, monkeypatch, race):
    receipt, name, rename = install(source), temporary_name(), mutation._rename_excl
    if race:

        def competing(*args):
            (source.root / name).mkdir()
            rename(*args)

        monkeypatch.setattr(mutation, "_rename_excl", competing)
    else:
        (source.root / name).mkdir()
    with pytest.raises(DirectoryMutationError, match="DESTINATION_EXISTS") as error:
        remove_created_directory(
            source, receipt.path, receipt.directory_identity, receipt.mode, name, lambda _: None
        )
    assert_before(error)
    assert identity(source.root / receipt.path) == receipt.directory_identity
    assert (source.root / name).is_dir()


@pytest.mark.parametrize("change", ["contents", "inode", "symlink", "file"])
def test_remove_check_to_move_race_preserves_isolated_actual_object_pending(
    source, monkeypatch, change
):
    receipt, name, recorded = install(source), temporary_name(), []
    target, rename = source.root / receipt.path, mutation._rename_excl

    def race(*args):
        if change == "contents":
            (target / "external.py").write_bytes(b"external")
        else:
            target.rename(source.root / "saved-created")
            if change == "inode":
                target.mkdir(mode=receipt.mode)
            elif change == "symlink":
                target.symlink_to("unrelated.py")
            else:
                target.write_bytes(b"external")
        rename(*args)

    monkeypatch.setattr(mutation, "_rename_excl", race)
    with pytest.raises(DirectoryMutationError) as error:
        remove_created_directory(
            source, receipt.path, receipt.directory_identity, receipt.mode, name, recorded.append
        )
    assert_pending(error)
    assert len(recorded) == 1
    assert recorded[0].directory_identity == receipt.directory_identity
    assert not target.exists()
    isolated = source.root / name
    assert isolated.exists()
    if change == "contents":
        assert (isolated / "external.py").read_bytes() == b"external"
    elif change == "file":
        assert isolated.read_bytes() == b"external"
    else:
        assert identity(source.root / "saved-created") == receipt.directory_identity
    assert (source.root / "unrelated.py").read_bytes() == b"untouched\n"


@pytest.mark.parametrize("phase", ["callback", "post_inspect", "rmdir"])
def test_remove_contents_appearing_after_isolation_are_preserved_pending(
    source, monkeypatch, phase
):
    receipt, name, recorded = install(source), temporary_name(), []
    path = source.root / name

    def on_moved(prepared):
        recorded.append(prepared)
        if phase == "callback":
            (path / "external.py").write_bytes(b"external")

    if phase == "post_inspect":
        inspect = mutation._inspect

        def after(parent, basename, *args):
            actual = inspect(parent, basename, *args)
            if basename == name:
                (path / "external.py").write_bytes(b"external")
            return actual

        monkeypatch.setattr(mutation, "_inspect", after)
    elif phase == "rmdir":
        rmdir = mutation.os.rmdir

        def during(*args, **kwargs):
            (path / "external.py").write_bytes(b"external")
            rmdir(*args, **kwargs)

        monkeypatch.setattr(mutation.os, "rmdir", during)
    with pytest.raises(DirectoryMutationError) as error:
        remove_created_directory(
            source, receipt.path, receipt.directory_identity, receipt.mode, name, on_moved
        )
    assert_pending(error)
    assert len(recorded) == 1
    assert (path / "external.py").read_bytes() == b"external"


def test_on_moved_journal_failure_is_pending_and_empty_material_is_kept(source):
    receipt, name, recorded = install(source), temporary_name(), []

    def fail(prepared):
        recorded.append(prepared)
        raise RuntimeError("private journal source/path")

    with pytest.raises(DirectoryMutationError) as error:
        remove_created_directory(
            source, receipt.path, receipt.directory_identity, receipt.mode, name, fail
        )
    assert_pending(error)
    assert len(recorded) == 1
    assert identity(source.root / name) == receipt.directory_identity
    assert "private journal" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("temp_name", "created"),
        ("temp_name", "../temp"),
        ("directory_identity", (1,)),
        ("parent_identity", (True, 1)),
        ("mode", True),
        ("mode", 0o1755),
        ("path", None),
    ],
)
@pytest.mark.parametrize("operation", ["commit", "discard"])
def test_malformed_prepared_bindings_reject_without_deletion(source, field, value, operation):
    prepared = prepare(source)
    invalid = replace(prepared, **{field: value})
    with pytest.raises(DirectoryMutationError, match="INVALID_PREPARED") as error:
        if operation == "commit":
            commit_directory(source, invalid)
        else:
            discard_directory_temp(source, invalid)
    assert_before(error)
    assert temporary(source, prepared).is_dir()


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_identity", (True, 1)),
        ("expected_identity", (1, 2, 3)),
        ("expected_mode", False),
        ("expected_mode", 0o1755),
        ("temp_name", "invalid"),
        ("on_moved", None),
    ],
)
def test_remove_invalid_authority_is_rejected(source, field, value):
    receipt = install(source)
    arguments = dict(
        source=source,
        path=receipt.path,
        expected_identity=receipt.directory_identity,
        expected_mode=receipt.mode,
        temp_name=temporary_name(),
        on_moved=lambda _: None,
    )
    arguments[field] = value
    with pytest.raises(DirectoryMutationError, match="INVALID_REMOVE") as error:
        remove_created_directory(**arguments)
    assert_before(error)
    assert identity(source.root / receipt.path) == receipt.directory_identity


@pytest.mark.parametrize(
    "platform,symbol,flag", [("darwin", "renameatx_np", 4), ("linux", "renameat2", 1)]
)
def test_native_five_argument_abi_and_noreplace_flags(monkeypatch, platform, symbol, flag):
    class Function:
        def __call__(self, *args):
            self.args = args
            return 0

    function, seen = Function(), []
    monkeypatch.setattr(mutation.sys, "platform", platform)

    def library(name, **kwargs):
        seen.append((name, kwargs))
        return SimpleNamespace(**{symbol: function})

    monkeypatch.setattr(mutation.ctypes, "CDLL", library)
    mutation._rename_excl(7, "临时", "目录")
    assert seen == [(None, {"use_errno": True})]
    assert function.args == (7, "临时".encode(), 7, "目录".encode(), flag)
    assert function.argtypes == [
        mutation.ctypes.c_int,
        mutation.ctypes.c_char_p,
        mutation.ctypes.c_int,
        mutation.ctypes.c_char_p,
        mutation.ctypes.c_uint,
    ]
    assert function.restype is mutation.ctypes.c_int


@pytest.mark.parametrize(
    "failure", ["platform", "symbol", "enosys", "enotsup", "einval", "exists", "permission"]
)
def test_unsupported_or_failed_noreplace_has_no_unsafe_fallback(monkeypatch, failure):
    class Function:
        def __call__(self, *_):
            return -1

    monkeypatch.setattr(
        mutation.sys, "platform", "unsupported" if failure == "platform" else "darwin"
    )
    native = SimpleNamespace() if failure == "symbol" else SimpleNamespace(renameatx_np=Function())
    monkeypatch.setattr(mutation.ctypes, "CDLL", lambda *_, **__: native)
    code = {
        "enosys": errno.ENOSYS,
        "enotsup": errno.ENOTSUP,
        "einval": errno.EINVAL,
        "exists": errno.EEXIST,
    }.get(failure, errno.EPERM)
    monkeypatch.setattr(mutation.ctypes, "get_errno", lambda: code)
    match = (
        "DESTINATION_EXISTS"
        if failure == "exists"
        else "ATOMIC_MOVE_FAILED"
        if failure == "permission"
        else "UNSUPPORTED_ATOMIC_NOREPLACE"
    )
    with pytest.raises(DirectoryMutationError, match=match) as error:
        mutation._rename_excl(7, "temp", "target")
    assert_before(error)


def test_missing_native_primitive_preserves_prepared_directory_and_never_plain_renames(
    source, monkeypatch
):
    prepared = prepare(source)

    def unsupported(*_):
        raise DirectoryMutationError("UNSUPPORTED_ATOMIC_NOREPLACE: unavailable")

    def forbidden(*_, **__):
        pytest.fail("unsafe plain rename fallback")

    monkeypatch.setattr(mutation, "_rename_excl", unsupported)
    monkeypatch.setattr(mutation.os, "rename", forbidden)
    monkeypatch.setattr(mutation.os, "replace", forbidden)
    with pytest.raises(DirectoryMutationError, match="UNSUPPORTED_ATOMIC_NOREPLACE") as error:
        commit_directory(source, prepared)
    assert_before(error)
    assert temporary(source, prepared).is_dir()
    assert not (source.root / "created").exists()

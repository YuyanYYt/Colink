"""Real local Git repositories; original user repositories are never modified."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_context.execution_git import GitCoordinator, GitError
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess
from code_context.write_coordinator import WriteCoordinator

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native macOS Git isolation")


def git(root, *args, input=None):
    result = subprocess.run(
        ["/usr/bin/git", *args],
        cwd=root,
        input=input,
        capture_output=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@localhost",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@localhost",
        },
    )
    return result.stdout


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main", "--template=")
    (root / "a.py").write_text("first\n")
    (root / "b.py").write_text("second\n")
    git(root, "add", "--", "a.py", "b.py")
    git(root, "commit", "-m", "origin")
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    writes = WriteCoordinator(store, lambda _: source, control_alive=lambda: True)
    writes.enable(["project"])
    epoch = [1]

    def authorized(project):
        if not epoch[0]:
            raise GitError("EXECUTION_DISABLED: enable locally")
        return {"epoch": epoch[0], "source_id": source.source_id}

    coordinator = GitCoordinator(tmp_path / "git-data", lambda _: source, writes, authorized)
    yield root, source, store, writes, coordinator, epoch
    coordinator.close()
    writes.close()
    store.close()


def task_edit(parts, *, paths=None, path="a.py", old="first", new="changed"):
    _, source, _, writes, _, _ = parts
    task = writes.begin_write_task("project", writes.status()["next_task_request_id"], paths=paths)[
        "task_id"
    ]
    document = source.read(path)
    writes.apply_edit(
        "project",
        task,
        "edit_file_0001",
        path,
        document.sha256,
        {"kind": "replace_fragment", "old_text": old, "new_text": new},
    )
    return task


def plan(parts, task, *, paths=None):
    return parts[4].git_plan("project", task, paths=paths or ["a.py"], message="Task changes")


def test_only_task_files_commit_and_other_staged_dirty_survive(parts):
    root, _, _, _, coordinator, _ = parts
    (root / "b.py").write_text("existing staged\n")
    git(root, "add", "b.py")
    (root / "b.py").write_text("existing staged\nplus dirty\n")
    old = git(root, "rev-parse", "HEAD").strip()
    staged = git(root, "show", ":b.py")
    task = task_edit(parts)
    prepared = plan(parts, task)
    result = coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert result["state"] == "completed" and not result["duplicate"]
    assert result["reflog_recorded"]
    assert git(root, "reflog", "-1", "--format=%H").decode().strip() == result["commit_id"]
    assert git(root, "rev-parse", "HEAD").decode().strip() == result["commit_id"]
    assert git(root, "rev-parse", "HEAD^1").strip() == old
    assert git(root, "show", "HEAD:a.py") == b"changed\n"
    assert git(root, "show", "HEAD:b.py") == b"second\n"
    assert git(root, "show", ":b.py") == staged
    assert (root / "b.py").read_text() == "existing staged\nplus dirty\n"
    assert git(root, "diff", "--name-only") == b"b.py\n"
    assert git(root, "diff", "--cached", "--name-only") == b"b.py\n"


@pytest.mark.parametrize("staged", [False, True])
def test_same_file_previous_dirty_or_staged_refuses_without_commit(parts, staged):
    root, _, _, _, _, _ = parts
    (root / "a.py").write_text("prior dirty first\n")
    if staged:
        git(root, "add", "a.py")
    head = git(root, "rev-parse", "HEAD")
    task = task_edit(parts)
    with pytest.raises(GitError, match="GIT_TASK_ORIGIN_CONFLICT"):
        plan(parts, task)
    assert git(root, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("change", ["head", "index", "source", "epoch"])
def test_plan_binds_head_index_source_and_local_grant(parts, change):
    root, _, _, _, coordinator, epoch = parts
    task = task_edit(parts)
    prepared = plan(parts, task)
    if change == "head":
        git(root, "commit", "--allow-empty", "-m", "external")
    elif change == "index":
        (root / "b.py").write_text("external staged\n")
        git(root, "add", "b.py")
    elif change == "source":
        (root / "a.py").write_text("external source\n")
    else:
        epoch[0] += 1
    head = git(root, "rev-parse", "HEAD")
    with pytest.raises(Exception, match="CONFLICT|EXPIRED"):
        coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert git(root, "rev-parse", "HEAD") == head


def test_idempotent_retry_cannot_commit_twice_or_reuse_request(parts):
    root, _, _, _, coordinator, _ = parts
    task = task_edit(parts)
    first_plan = plan(parts, task)
    other_plan = plan(parts, task)
    first = coordinator.git_commit("project", first_plan["git_plan_id"], "commit_req_0001")
    second = coordinator.git_commit("project", first_plan["git_plan_id"], "commit_req_0001")
    assert second["duplicate"] and second["commit_id"] == first["commit_id"]
    assert git(root, "rev-list", "--count", "HEAD") == b"2\n"
    with pytest.raises(GitError, match="REQUEST_ID_CONFLICT"):
        coordinator.git_commit("project", other_plan["git_plan_id"], "commit_req_0001")


def test_hooks_filters_includes_helpers_and_user_config_are_never_loaded(parts, monkeypatch):
    root, _, _, _, coordinator, _ = parts
    marker = root.parent / "executed-program"
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    hook.chmod(0o755)
    git(root, "config", "filter.evil.clean", f"touch '{marker}'; cat")
    git(root, "config", "filter.evil.required", "true")
    git(root, "config", "core.fsmonitor", str(hook))
    git(root, "config", "core.hooksPath", str(hook.parent))
    git(root, "config", "commit.gpgSign", "true")
    git(root, "config", "gpg.program", str(hook))
    (root / ".gitattributes").write_text("a.py filter=evil\n")
    secret_config = root.parent / "unread-user-config"
    secret_config.write_text("deliberately invalid config\n")
    git(root, "config", "include.path", str(secret_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(secret_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(secret_config))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(hook))
    task = task_edit(parts)
    prepared = plan(parts, task)
    result = coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert result["state"] == "completed"
    assert not marker.exists()
    # Query through trusted plumbing, since ordinary Git deliberately fails on
    # the hostile include now present in the fixture repository.
    assert (root / ".git" / "refs" / "heads" / "main").read_text().strip() == result["commit_id"]


@pytest.mark.parametrize("phase", ["after_ref", "after_index"])
def test_restart_returns_existing_commit_and_recovers_partial_metadata(parts, monkeypatch, phase):
    root, source, _, writes, coordinator, _ = parts
    task = task_edit(parts)
    prepared = plan(parts, task)

    def interrupted(*args):
        raise RuntimeError("injected crash")

    if phase == "after_ref":
        monkeypatch.setattr(coordinator, "_after_ref_install", interrupted)
    else:
        real_save = coordinator._save_receipt

        def save(request, plan_id, receipt):
            if receipt["state"] == "completed":
                interrupted()
            real_save(request, plan_id, receipt)

        monkeypatch.setattr(coordinator, "_save_receipt", save)
    with pytest.raises(RuntimeError, match="injected crash"):
        coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    committed = git(root, "rev-parse", "HEAD").decode().strip()
    coordinator.close()
    restarted = GitCoordinator(
        coordinator.root,
        lambda _: source,
        writes,
        lambda _: {"source_id": source.source_id, "epoch": 1},
    )
    try:
        result = restarted.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
        assert result["commit_id"] == committed and result["duplicate"]
        assert git(root, "rev-list", "--count", "HEAD") == b"2\n"
        assert git(root, "diff", "--cached", "--name-only") == b""
        assert not (root / ".git" / "index.lock").exists()
    finally:
        restarted.close()


@pytest.mark.parametrize("phase", ["temp_created", "object_written", "object_linked"])
def test_restart_recovers_journaled_object_temporary_without_creating_second_commit(
    parts, monkeypatch, phase
):
    root, source, _, writes, coordinator, _ = parts
    task = task_edit(parts)
    prepared = plan(parts, task)
    old = git(root, "rev-parse", "HEAD")

    def interrupted():
        raise RuntimeError("injected object-install crash")

    method = {
        "temp_created": "_after_object_temp_created",
        "object_written": "_after_object_write",
        "object_linked": "_after_object_link",
    }[phase]
    monkeypatch.setattr(coordinator, method, interrupted)
    with pytest.raises(RuntimeError, match="injected object-install crash"):
        coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert git(root, "rev-parse", "HEAD") == old
    row = coordinator.db.execute(
        "SELECT data FROM receipts WHERE request='commit_req_0001'"
    ).fetchone()
    receipt = __import__("json").loads(row[0])
    expected_commit = receipt["commit_id"]
    intent = receipt["object_intent"]
    temporary = root / ".git" / "objects" / intent["prefix"] / intent["temporary"]
    assert temporary.exists() and intent["identity"]
    coordinator.close()
    restarted = GitCoordinator(
        coordinator.root,
        lambda _: source,
        writes,
        lambda _: {"source_id": source.source_id, "epoch": 1},
    )
    try:
        result = restarted.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
        assert result["duplicate"] and result["commit_id"] == expected_commit
        assert not temporary.exists()
        assert git(root, "rev-list", "--count", "HEAD") == b"2\n"
        assert git(root, "show", "HEAD:a.py") == b"changed\n"
    finally:
        restarted.close()


def test_object_recovery_refuses_replaced_temporary_and_preserves_unknown_file(parts, monkeypatch):
    root, source, _, writes, coordinator, _ = parts
    task = task_edit(parts)
    prepared = plan(parts, task)
    head = git(root, "rev-parse", "HEAD")

    def interrupted():
        raise RuntimeError("injected object-install crash")

    monkeypatch.setattr(coordinator, "_after_object_write", interrupted)
    with pytest.raises(RuntimeError):
        coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    row = coordinator.db.execute(
        "SELECT data FROM receipts WHERE request='commit_req_0001'"
    ).fetchone()
    intent = __import__("json").loads(row[0])["object_intent"]
    temporary = root / ".git" / "objects" / intent["prefix"] / intent["temporary"]
    # Preserve the app's old inode as test evidence; install a distinct unknown
    # regular inode at the recorded name without touching user repositories.
    temporary.rename(temporary.with_suffix(".retained"))
    temporary.write_text("unknown replacement\n")
    coordinator.close()
    restarted = GitCoordinator(
        coordinator.root,
        lambda _: source,
        writes,
        lambda _: {"source_id": source.source_id, "epoch": 1},
    )
    try:
        with pytest.raises(GitError, match="GIT_OBJECT_RECOVERY_CONFLICT"):
            restarted.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
        assert temporary.read_text() == "unknown replacement\n"
        assert git(root, "rev-parse", "HEAD") == head
    finally:
        restarted.close()


@pytest.mark.parametrize("unsafe", ["objects_symlink", "alternates", "hardlink", "git_alias"])
def test_unsafe_repository_objects_and_metadata_rejected(parts, unsafe):
    root, _, _, _, _, _ = parts
    task = task_edit(parts)
    if unsafe == "objects_symlink":
        (root / ".git" / "objects" / "foreign").symlink_to(root.parent)
    elif unsafe == "alternates":
        (root / ".git" / "objects" / "info" / "alternates").write_text(str(root.parent))
    elif unsafe == "hardlink":
        os.link(root / ".git" / "HEAD", root / ".git" / "objects" / "foreign")
    else:
        (root / ".git").rename(root / "git-original")
        (root / ".git").symlink_to(root / "git-original", target_is_directory=True)
    with pytest.raises(GitError, match="UNSAFE"):
        plan(parts, task)


def test_new_and_deleted_task_files_are_committed(parts):
    root, source, _, writes, coordinator, _ = parts
    task = writes.begin_write_task(
        "project", writes.status()["next_task_request_id"], paths=["a.py", "new.py"]
    )["task_id"]
    writes.create_file("project", task, "create_new_0001", "new.py", "created\n")
    writes.delete_file("project", task, "delete_old_0001", "a.py", source.read("a.py").sha256)
    prepared = plan(parts, task, paths=["a.py", "new.py"])
    coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert git(root, "ls-tree", "--name-only", "HEAD") == b"b.py\nnew.py\n"
    assert git(root, "diff", "--cached", "--name-only") == b""


def test_foreign_index_lock_is_preserved(parts):
    root, _, _, _, coordinator, _ = parts
    task = task_edit(parts)
    prepared = plan(parts, task)
    lock = root / ".git" / "index.lock"
    lock.write_bytes(b"foreign lock")
    old_head = git(root, "rev-parse", "HEAD")
    with pytest.raises(GitError, match="GIT_LOCKED"):
        coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert lock.read_bytes() == b"foreign lock"
    assert git(root, "rev-parse", "HEAD") == old_head


def test_errors_do_not_echo_repository_text_or_message_credentials(parts):
    _, _, _, _, coordinator, _ = parts
    task = task_edit(parts)
    with pytest.raises(GitError) as error:
        coordinator.git_plan("project", task, paths=["a.py"], message="sk-proj-" + "a" * 30)
    assert "sk-proj-" not in str(error.value)


def test_coordinator_state_private_and_single_owner(parts):
    _, source, _, writes, coordinator, _ = parts
    assert (coordinator.root / "git.sqlite3").stat().st_mode & 0o077 == 0
    with pytest.raises(GitError, match="GIT_ALREADY_OPEN"):
        GitCoordinator(coordinator.root, lambda _: source, writes, lambda _: None)


def test_unborn_repository_first_commit(parts, tmp_path):
    _, _, _, writes, coordinator, _ = parts
    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init", "-b", "main", "--template=")
    source = SourceAccess(root)
    writes.disable()
    writes.source_provider = lambda _: source
    writes.enable(["project"])
    coordinator.source_for = lambda _: source
    coordinator.authorize = lambda _: {"source_id": source.source_id, "epoch": 1}
    task = writes.begin_write_task(
        "project", writes.status()["next_task_request_id"], paths=["new.py"]
    )["task_id"]
    writes.create_file("project", task, "create_file_0001", "new.py", "first commit\n")
    prepared = plan(parts, task, paths=["new.py"])
    coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert git(root, "rev-list", "--count", "HEAD") == b"1\n"


def test_plan_records_hashes_not_file_content(parts):
    _, _, _, _, coordinator, _ = parts
    task = task_edit(parts)
    prepared = plan(parts, task)
    row = coordinator.db.execute(
        "SELECT data FROM plans WHERE id=?", (prepared["git_plan_id"],)
    ).fetchone()[0]
    assert "changed\\n" not in row
    assert hashlib.sha256(b"changed\n").hexdigest() in row
    assert Path(coordinator.root).is_dir()


@pytest.mark.parametrize("kind", ["rename", "edit_rename_edit", "continuous", "binary"])
def test_move_commit_proves_original_path_and_preserves_other_stage(parts, kind):
    root, source, _, writes, coordinator, _ = parts
    src, dst = "a.py", "renamed.py"
    if kind == "binary":
        src, dst = "logo.png", "renamed.png"
        (root / src).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 24)
        git(root, "add", src)
        git(root, "commit", "-m", "asset origin")
    task = writes.begin_write_task(
        "project", writes.status()["next_task_request_id"], paths=[src, dst, "final.py"]
    )["task_id"]
    if kind == "edit_rename_edit":
        before = source.read(src)
        writes.apply_edit(
            "project",
            task,
            "edit_first_0001",
            src,
            before.sha256,
            {"kind": "replace_fragment", "old_text": "first", "new_text": "middle"},
        )
    writes.move_path("project", task, "move_file_0001", src, dst)
    if kind == "edit_rename_edit":
        before = source.read(dst)
        writes.apply_edit(
            "project",
            task,
            "edit_second_0001",
            dst,
            before.sha256,
            {"kind": "replace_fragment", "old_text": "middle", "new_text": "final"},
        )
    if kind == "continuous":
        writes.move_path("project", task, "move_file_0002", dst, "final.py")
        dst = "final.py"
    prepared = plan(parts, task, paths=[dst])
    assert {item["path"] for item in prepared["files"]} == {src, dst}
    result = coordinator.git_commit("project", prepared["git_plan_id"], "commit_move_0001")
    assert result["state"] == "completed"
    names = git(root, "ls-tree", "--name-only", "HEAD").splitlines()
    assert dst.encode() in names and src.encode() not in names
    assert git(root, "show", "HEAD:" + dst) == (root / dst).read_bytes()
    assert git(root, "diff", "--cached", "--name-only") == b""


def test_move_with_original_dirty_file_refuses_git_commit(parts):
    root, _, _, writes, _, _ = parts
    (root / "a.py").write_text("prior dirty first\n")
    task = writes.begin_write_task(
        "project", writes.status()["next_task_request_id"], paths=["a.py", "renamed.py"]
    )["task_id"]
    writes.move_path("project", task, "move_file_0001", "a.py", "renamed.py")
    with pytest.raises(GitError, match="GIT_TASK_ORIGIN_CONFLICT"):
        plan(parts, task, paths=["renamed.py"])


def test_native_git_sandbox_cannot_write_project_or_control_data(parts, monkeypatch):
    root, source, _, _, coordinator, _ = parts
    from code_context.execution_git import _Repository

    original_popen = subprocess.Popen
    sentinel = coordinator.root / "control-sentinel"
    sentinel.write_text("preserve\n")
    script = 'printf changed > "$1"; printf changed > "$2"; exit 0'

    def bounded_probe(command, **kwargs):
        command = command[:3] + [
            "/bin/sh",
            "-c",
            script,
            "probe",
            str(root / "a.py"),
            str(sentinel),
        ]
        return original_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", bounded_probe)
    coordinator._git(_Repository(source), ["probe"])
    assert (root / "a.py").read_text() == "first\n"
    assert sentinel.read_text() == "preserve\n"


def test_nested_repository_changes_are_not_adopted(parts):
    root, source, _, writes, coordinator, _ = parts
    nested = root / "nested"
    nested.mkdir()
    (nested / "inner.py").write_text("before\n")
    git(root, "add", "nested/inner.py")
    git(root, "commit", "-m", "nested file before nested repository")
    git(nested, "init", "--template=")
    task = writes.begin_write_task(
        "project", writes.status()["next_task_request_id"], paths=["nested/inner.py"]
    )["task_id"]
    before = source.read("nested/inner.py")
    writes.apply_edit(
        "project",
        task,
        "edit_nested_0001",
        "nested/inner.py",
        before.sha256,
        {"kind": "replace_fragment", "old_text": "before", "new_text": "after"},
    )
    with pytest.raises(GitError, match="GIT_NESTED_PROJECT"):
        coordinator.git_plan("project", task, paths=["nested/inner.py"], message="nested")


def test_moved_then_deleted_file_commits_original_deletion(parts):
    root, source, _, writes, coordinator, _ = parts
    task = writes.begin_write_task(
        "project", writes.status()["next_task_request_id"], paths=["a.py", "renamed.py"]
    )["task_id"]
    writes.move_path("project", task, "move_file_0001", "a.py", "renamed.py")
    writes.delete_file(
        "project", task, "delete_file_0001", "renamed.py", source.read("renamed.py").sha256
    )
    prepared = plan(parts, task, paths=["renamed.py"])
    coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert git(root, "ls-tree", "--name-only", "HEAD") == b"b.py\n"


def test_moved_directory_files_and_assets_commit_without_origin_drift(parts):
    root, _, _, writes, coordinator, _ = parts
    folder = root / "src"
    folder.mkdir()
    (folder / "app.py").write_text("app\n")
    (folder / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 24)
    git(root, "add", "src")
    git(root, "commit", "-m", "directory origin")
    task = writes.begin_write_task(
        "project", writes.status()["next_task_request_id"], paths=["src", "renamed"]
    )["task_id"]
    writes.move_path("project", task, "move_directory_0001", "src", "renamed")
    prepared = plan(parts, task, paths=["renamed/app.py", "renamed/logo.png"])
    coordinator.git_commit("project", prepared["git_plan_id"], "commit_req_0001")
    assert git(root, "ls-tree", "--name-only", "HEAD") == b"a.py\nb.py\nrenamed\n"
    assert git(root, "show", "HEAD:renamed/logo.png") == (root / "renamed/logo.png").read_bytes()

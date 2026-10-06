import errno
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import code_context.scanner as scanner_module
from code_context.models import FileChange, content_hash
from code_context.policy import EXCLUDED_DIRS
from code_context.scanner import ScanError, Scanner, ScanResult, SourceFile


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    # macOS's default pytest temp root can contain the /var -> /private/var symlink.
    return tmp_path.resolve()


def write(root: Path, path: str, content: str | bytes = "source\n") -> Path:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        destination.write_bytes(content)
    else:
        destination.write_text(content, encoding="utf-8", newline="")
    return destination


def source(path: str, content: str = "source\n") -> SourceFile:
    return SourceFile(path, content, content_hash(content))


def forbid_full_scan(monkeypatch: pytest.MonkeyPatch, scanner: Scanner) -> None:
    def unexpected(previous: ScanResult | None = None) -> ScanResult:
        pytest.fail("an ordinary file refresh must not scan the entire tree")

    monkeypatch.setattr(scanner, "_scan", unexpected)


def test_snapshot_utf8_chinese_paths_and_exact_hash(tmp_path: Path) -> None:
    content = "中文注释\r\nprint('你好')\n"
    write(tmp_path, "源码/入口.py", content)
    write(tmp_path, "empty.txt", "")

    result = Scanner(tmp_path).scan()

    assert result.files == {
        "源码/入口.py": source("源码/入口.py", content),
        "empty.txt": source("empty.txt", ""),
    }
    assert result.skipped == {}
    for item in result.files.values():
        FileChange(op="upsert", path=item.path, content=item.content, sha256=item.sha256)
    with pytest.raises(FrozenInstanceError):
        result.files["empty.txt"].content = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("path", sorted(EXCLUDED_DIRS))
def test_all_mandatory_directories_are_pruned(tmp_path: Path, path: str) -> None:
    write(tmp_path, f"{path}/do-not-read.py")
    write(tmp_path, "main.py")

    result = Scanner(tmp_path).scan()

    assert set(result.files) == {"main.py"}
    assert result.skipped == {path: "mandatory policy exclusion"}


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "key.pem",
        "key.KEY",
        "id_rsa",
        "id_ed25519.pub",
        "credentials.json",
        "secrets.yaml",
        "cache.db",
        "cache.sqlite3",
        "file.pyc",
        ".DS_Store",
        ".colink-write-0123456789abcdef0123456789abcdef.tmp",
        "nested/credentials-folder/source.py",
    ],
)
def test_mandatory_names_are_never_read(
    tmp_path: Path, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, path, "private content")
    scanner = Scanner(tmp_path)
    read_text = scanner._read_text
    reads: list[str] = []

    def record(parent: int, name: str, relative: str) -> tuple[str | None, str | None]:
        reads.append(relative)
        return read_text(parent, name, relative)

    monkeypatch.setattr(scanner, "_read_text", record)
    result = scanner.scan()

    assert not result.files
    assert result.skipped
    assert path not in reads
    assert "private content" not in repr(result)


def test_root_ignore_rules_precedence_negations_and_parent_pruning(tmp_path: Path) -> None:
    write(tmp_path, ".gitignore", "*.tmp\ncache/\nignored/\n!.env\n")
    write(tmp_path, ".codecontextignore", "!keep.tmp\nprivate/\n!ignored/keep.py\n")
    for path in ["drop.tmp", "keep.tmp", "cache/a.py", "ignored/keep.py", "private/a.py", ".env"]:
        write(tmp_path, path)
    write(tmp_path, "nested/.gitignore", "local.py\n")
    write(tmp_path, "nested/local.py")

    result = Scanner(tmp_path).scan()

    assert set(result.files) == {
        ".gitignore",
        ".codecontextignore",
        "keep.tmp",
        "nested/.gitignore",
        "nested/local.py",
    }
    for path in ["drop.tmp", "cache", "ignored", "private"]:
        assert result.skipped[path] == "ignored by root ignore rules"
    assert result.skipped[".env"] == "mandatory policy exclusion"


def test_root_ignore_rules_can_reinclude_a_parent_then_a_child(tmp_path: Path) -> None:
    write(tmp_path, ".gitignore", "cache/\n!cache/\ncache/*\n!cache/keep.py\n")
    write(tmp_path, "cache/keep.py")
    write(tmp_path, "cache/drop.py")

    result = Scanner(tmp_path).scan()

    assert "cache/keep.py" in result.files
    assert result.skipped["cache/drop.py"] == "ignored by root ignore rules"


def test_excluded_state_roots_and_external_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    write(root, "local-state/queue.json", "private queue")
    write(root, "nested/state/mirror.py", "private mirror")
    write(root, "nested/main.py")
    scanner = Scanner(root, (Path("local-state"), root / "nested/state", tmp_path / "external"))
    read_text = scanner._read_text

    def read(parent: int, name: str, path: str) -> tuple[str | None, str | None]:
        assert not path.startswith(("local-state/", "nested/state/"))
        return read_text(parent, name, path)

    monkeypatch.setattr(scanner, "_read_text", read)
    result = scanner.scan()

    assert set(result.files) == {"nested/main.py"}
    assert result.skipped == {"local-state": "excluded root", "nested/state": "excluded root"}
    forbid_full_scan(monkeypatch, scanner)
    assert scanner.refresh(result, {"local-state/queue.json"}).files == result.files


def test_file_directory_and_broken_symlinks_are_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = write(tmp_path, "outside/private.py", "outside content")
    write(root, "main.py")
    (root / "linked.py").symlink_to(outside)
    (root / "linked-dir").symlink_to(outside.parent, target_is_directory=True)
    (root / "broken.py").symlink_to(tmp_path / "missing")

    result = Scanner(root).scan()

    assert set(result.files) == {"main.py"}
    assert result.skipped == {name: "symlink" for name in ["linked.py", "linked-dir", "broken.py"]}
    assert "outside content" not in repr(result)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"hello\x00world", "binary content"),
        (b"\xff\xfe\x80", "invalid UTF-8 text"),
        ("token = '" + "sk-" + "a" * 24 + "'", "possible credential or private key"),
        ("token = '" + "sk-proj-" + "a" * 24 + "'", "possible credential or private key"),
        ("token = '" + "sk-ant-" + "a" * 24 + "'", "possible credential or private key"),
        ("token = '" + "AKIA" + "A" * 16 + "'", "possible credential or private key"),
        ("token = '" + "ghp_" + "a" * 24 + "'", "possible credential or private key"),
        ("token = '" + "github_pat_" + "a" * 24 + "'", "possible credential or private key"),
        ("token = '" + "xoxb-" + "a" * 24 + "'", "possible credential or private key"),
        ("-----BEGIN " + "RSA PRIVATE KEY-----\n", "possible credential or private key"),
    ],
)
def test_binary_invalid_utf8_and_secrets_do_not_enter_results(
    tmp_path: Path, content: str | bytes, reason: str
) -> None:
    write(tmp_path, "blocked.txt", content)

    result = Scanner(tmp_path).scan()

    assert result.files == {}
    assert result.skipped == {"blocked.txt": reason}
    assert content not in result.skipped.values()


def test_non_regular_files_are_not_opened(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")

    assert Scanner(tmp_path).scan().skipped == {"pipe": "not a regular file"}


def test_file_size_bound_and_short_read_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner_module, "MAX_FILE_BYTES", 7)
    write(tmp_path, "accepted.py", "1234567")
    write(tmp_path, "oversized.py", "12345678")
    real_read = os.read
    returned: list[bytes] = []
    requested: list[int] = []

    def read(fd: int, count: int) -> bytes:
        requested.append(count)
        chunk = real_read(fd, min(count, 2))
        returned.append(chunk)
        return chunk

    monkeypatch.setattr(scanner_module.os, "read", read)
    result = Scanner(tmp_path).scan()

    assert set(result.files) == {"accepted.py"}
    assert result.skipped == {"oversized.py": "file exceeds 7 bytes"}
    assert sum(map(len, returned)) == 7
    assert max(requested) == 8


def test_growing_file_read_is_bounded_and_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner_module, "MAX_FILE_BYTES", 7)
    file = write(tmp_path, "growing.py", "1234567")
    real_read = os.read
    returned: list[bytes] = []

    def read(fd: int, count: int) -> bytes:
        if not returned:
            file.write_text("x" * 100, encoding="utf-8")
        chunk = real_read(fd, count)
        returned.append(chunk)
        return chunk

    monkeypatch.setattr(scanner_module.os, "read", read)
    with pytest.raises(ScanError, match="changed during reading"):
        Scanner(tmp_path).scan()
    assert sum(map(len, returned)) == 8


@pytest.mark.parametrize("limit", ["MAX_FILES", "MAX_TOTAL_BYTES"])
def test_snapshot_limits_fail_instead_of_returning_a_partial_snapshot(
    tmp_path: Path, limit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner_module, limit, 1 if limit == "MAX_FILES" else 5)
    write(tmp_path, "a.py", "中")
    write(tmp_path, "b.py", "中")

    with pytest.raises(ScanError, match=limit):
        Scanner(tmp_path).scan()


def test_unchanged_refresh_does_not_hash_scan_or_produce_upserts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, ".gitignore", "*.log\n")
    file = write(tmp_path, "main.py")
    write(tmp_path, "other.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    os.utime(file, None)
    reads: list[str] = []
    read_text = scanner._read_text

    def read(parent: int, name: str, path: str) -> tuple[str | None, str | None]:
        reads.append(path)
        return read_text(parent, name, path)

    def unexpected_hash(content: str) -> str:
        pytest.fail("unchanged content must reuse its hash")

    monkeypatch.setattr(scanner, "_read_text", read)
    monkeypatch.setattr(scanner_module, "content_hash", unexpected_hash)
    forbid_full_scan(monkeypatch, scanner)
    refreshed = scanner.refresh(previous, {"main.py"})

    assert reads == ["main.py"]
    assert refreshed.files == previous.files
    assert all(refreshed.files[path] is old for path, old in previous.files.items())
    upserts = [path for path, item in refreshed.files.items() if item != previous.files.get(path)]
    assert upserts == []


def test_refresh_reads_and_hashes_only_changed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "源码/入口.py", "before")
    write(tmp_path, "unchanged.py", "same")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    write(tmp_path, "源码/入口.py", "更新后")
    hashed: list[str] = []
    real_hash = scanner_module.content_hash

    def hash_content(content: str) -> str:
        hashed.append(content)
        return real_hash(content)

    monkeypatch.setattr(scanner_module, "content_hash", hash_content)
    forbid_full_scan(monkeypatch, scanner)
    refreshed = scanner.refresh(previous, {"源码/入口.py"})

    assert hashed == ["更新后"]
    assert refreshed.files["源码/入口.py"] == source("源码/入口.py", "更新后")
    assert refreshed.files["unchanged.py"] is previous.files["unchanged.py"]
    assert previous.files["源码/入口.py"].content == "before"


def test_file_rename_is_incremental_add_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = write(tmp_path, "旧名字.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    old.rename(tmp_path / "新名字.py")
    forbid_full_scan(monkeypatch, scanner)

    refreshed = scanner.refresh(previous, {"旧名字.py", "新名字.py"})

    assert refreshed.files == {"新名字.py": source("新名字.py")}
    assert refreshed.skipped == {}
    assert set(previous.files) == {"旧名字.py"}


def test_deleted_files_and_skipped_paths_are_removed_incrementally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = write(tmp_path, "main.py")
    binary = write(tmp_path, "binary.dat", b"\x00")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    file.unlink()
    binary.unlink()
    forbid_full_scan(monkeypatch, scanner)

    refreshed = scanner.refresh(previous, {"main.py", "binary.dat"})

    assert refreshed == ScanResult({}, {})


@pytest.mark.parametrize("content", [b"\x00", b"\xff", "sk-" + "a" * 24])
def test_refresh_removes_newly_unsafe_content(
    tmp_path: Path, content: bytes | str, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "main.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    write(tmp_path, "main.py", content)
    forbid_full_scan(monkeypatch, scanner)

    refreshed = scanner.refresh(previous, {"main.py"})

    assert refreshed.files == {}
    assert "main.py" in refreshed.skipped
    write(tmp_path, "main.py", "safe")
    restored = scanner.refresh(refreshed, {"main.py"})
    assert restored.files == {"main.py": source("main.py", "safe")}
    assert restored.skipped == {}


def test_file_replaced_with_symlink_is_removed_incrementally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    file = write(root, "main.py")
    outside = write(tmp_path, "outside.py", "outside content")
    scanner = Scanner(root)
    previous = scanner.scan()
    file.unlink()
    file.symlink_to(outside)
    forbid_full_scan(monkeypatch, scanner)

    refreshed = scanner.refresh(previous, {"main.py"})

    assert refreshed.files == {}
    assert refreshed.skipped == {"main.py": "symlink"}
    assert "outside content" not in repr(refreshed)


@pytest.mark.parametrize(
    "event", ["added", "renamed", "deleted", "file-to-directory", "directory-to-file"]
)
def test_directory_changes_reconcile_all_children(tmp_path: Path, event: str) -> None:
    write(tmp_path, "old/one.py")
    write(tmp_path, "old/two.py")
    write(tmp_path, "main.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    if event == "added":
        write(tmp_path, "new/child.py")
        changed = {"new"}
        expected = {"main.py", "old/one.py", "old/two.py", "new/child.py"}
    elif event == "renamed":
        (tmp_path / "old").rename(tmp_path / "new")
        changed = {"old", "new"}
        expected = {"main.py", "new/one.py", "new/two.py"}
    elif event == "deleted":
        (tmp_path / "old").rename(tmp_path / ".artifacts")
        changed = {"old"}
        expected = {"main.py"}
    elif event == "file-to-directory":
        (tmp_path / "main.py").unlink()
        write(tmp_path, "main.py/child.py")
        changed = {"main.py"}
        expected = {"main.py/child.py", "old/one.py", "old/two.py"}
    else:
        (tmp_path / "old").rename(tmp_path / ".artifacts")
        write(tmp_path, "old", "now a file")
        changed = {"old"}
        expected = {"main.py", "old"}

    assert set(scanner.refresh(previous, changed).files) == expected


def test_empty_directory_events_also_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "empty").mkdir()
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    (tmp_path / "empty").rmdir()
    real_scan = scanner._scan
    calls: list[bool] = []

    def scan(previous: ScanResult | None = None) -> ScanResult:
        calls.append(True)
        return real_scan(previous)

    monkeypatch.setattr(scanner, "_scan", scan)
    assert scanner.refresh(previous, {"empty"}) == ScanResult({}, {})
    assert calls == [True]


@pytest.mark.parametrize("ignore_name", [".gitignore", ".codecontextignore"])
@pytest.mark.parametrize("event", ["added", "modified", "deleted"])
def test_ignore_changes_reconcile_previously_unreported_files(
    tmp_path: Path, ignore_name: str, event: str
) -> None:
    write(tmp_path, "main.py")
    write(tmp_path, "other.py")
    if event != "added":
        write(tmp_path, ignore_name, "other.py\n")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    if event == "deleted":
        (tmp_path / ignore_name).unlink()
    else:
        write(tmp_path, ignore_name, "main.py\n")

    refreshed = scanner.refresh(previous, {ignore_name})

    if event == "deleted":
        assert set(refreshed.files) == {"main.py", "other.py"}
        assert refreshed.skipped == {}
    else:
        assert set(refreshed.files) == {ignore_name, "other.py"}
        assert refreshed.skipped["main.py"] == "ignored by root ignore rules"


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "symlink-ancestor", "traversal"])
def test_unsafe_or_non_directory_roots_are_rejected(tmp_path: Path, kind: str) -> None:
    real = tmp_path / "real"
    real.mkdir()
    if kind == "missing":
        root = tmp_path / "missing"
    elif kind == "file":
        root = write(tmp_path, "file.py")
    elif kind == "symlink":
        root = tmp_path / "linked"
        root.symlink_to(real, target_is_directory=True)
    elif kind == "symlink-ancestor":
        (real / "inner").mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)
        root = linked / "inner"
    else:
        root = real / ".."

    with pytest.raises((ScanError, ValueError), match="real directory|must not contain"):
        Scanner(root)


@pytest.mark.parametrize("excluded", [".", "..", "state/../outside"])
def test_unsafe_excluded_state_roots_are_rejected(tmp_path: Path, excluded: str) -> None:
    with pytest.raises(ValueError, match="excluded root|must not contain"):
        Scanner(tmp_path, (Path(excluded),))


@pytest.mark.parametrize(
    "path", ["../outside", "/outside", "a/../b", "a//b", "./a", "a\\b", "a:b", "a\x00b", ""]
)
def test_unsafe_changed_and_previous_paths_are_rejected(tmp_path: Path, path: str) -> None:
    scanner = Scanner(tmp_path)
    with pytest.raises(ValueError):
        scanner.refresh(ScanResult({}, {}), {path})
    with pytest.raises(ValueError):
        scanner.refresh(ScanResult({path: source(path)}, {}), set())
    with pytest.raises(ValueError):
        scanner.refresh(ScanResult({}, {path: "old skip"}), set())


@pytest.mark.parametrize(
    "previous",
    [
        ScanResult({"a.py": source("b.py")}, {}),
        ScanResult({".env": source(".env")}, {}),
        ScanResult({"a.py": SourceFile("a.py", "safe", "bad hash")}, {}),
        ScanResult({"a.py": source("a.py", "sk-" + "a" * 24)}, {}),
        ScanResult({"a.py": source("a.py")}, {"a.py": "skip"}),
    ],
)
def test_invalid_previous_snapshots_are_rejected(tmp_path: Path, previous: ScanResult) -> None:
    with pytest.raises(ValueError, match="previous snapshot"):
        Scanner(tmp_path).refresh(previous, set())


@pytest.mark.parametrize("path", ["blocked.py", "blocked"])
@pytest.mark.parametrize("operation", ["scan", "refresh"])
def test_permission_errors_abort_without_inferred_deletion(
    tmp_path: Path, path: str, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "blocked.py" if path.endswith(".py") else "blocked/child.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    before = previous.files.copy()
    real_open = os.open

    def denied(file: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if file == path:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(scanner_module.os, "open", denied)
    with pytest.raises(ScanError, match="blocked") as error:
        scanner.scan() if operation == "scan" else scanner.refresh(previous, {path})
    assert isinstance(error.value.__cause__, PermissionError)
    assert previous.files == before


@pytest.mark.parametrize("operation", ["scan", "refresh"])
def test_generic_io_errors_are_not_swallowed(
    tmp_path: Path, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "main.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()

    def broken_read(fd: int, count: int) -> bytes:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(scanner_module.os, "read", broken_read)
    with pytest.raises(ScanError, match="main.py.*Input/output error") as error:
        scanner.scan() if operation == "scan" else scanner.refresh(previous, {"main.py"})
    assert isinstance(error.value.__cause__, OSError)
    assert error.value.__cause__.errno == errno.EIO
    assert previous.files == {"main.py": source("main.py")}


def test_incomplete_directory_listing_aborts_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "folder/one.py")
    write(tmp_path, "folder/two.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    real_scandir = os.scandir
    target_inode = (tmp_path / "folder").stat().st_ino

    class Incomplete:
        def __enter__(self) -> "Incomplete":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def __iter__(self):
            raise PermissionError(errno.EACCES, "Directory listing interrupted")
            yield  # pragma: no cover

    def scandir(fd: int):
        return Incomplete() if os.fstat(fd).st_ino == target_inode else real_scandir(fd)

    monkeypatch.setattr(scanner_module.os, "scandir", scandir)
    with pytest.raises(ScanError, match="incomplete directory scan.*folder"):
        scanner.refresh(previous, {"folder"})
    assert set(previous.files) == {"folder/one.py", "folder/two.py"}


def test_directory_vanishing_between_listing_and_stat_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "empty"
    directory.mkdir()
    scanner = Scanner(tmp_path)
    real_stat = scanner._stat

    def vanished(fd: int, name: str, path: str):
        if path == "empty":
            directory.rmdir()
        return real_stat(fd, name, path)

    monkeypatch.setattr(scanner, "_stat", vanished)
    with pytest.raises(ScanError, match="incomplete directory scan.*vanished"):
        scanner.scan()


def test_disappearing_file_is_an_ordinary_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = write(tmp_path, "main.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    read_text = scanner._read_text

    def disappear(parent: int, name: str, path: str) -> tuple[str | None, str | None]:
        if path == "main.py":
            file.unlink()
        return read_text(parent, name, path)

    monkeypatch.setattr(scanner, "_read_text", disappear)
    assert scanner.refresh(previous, {"main.py"}) == ScanResult({}, {})


def test_root_is_revalidated_even_for_empty_refresh(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    scanner = Scanner(root)
    previous = scanner.scan()
    root.rmdir()

    with pytest.raises(ScanError, match="project root must exist"):
        scanner.refresh(previous, set())


@pytest.mark.parametrize("content", [b"\xff", b"\x00", "sk-" + "a" * 24])
def test_unreadable_ignore_content_fails_closed(tmp_path: Path, content: bytes | str) -> None:
    write(tmp_path, ".gitignore", content)

    with pytest.raises(ScanError, match="cannot load root ignore rules"):
        Scanner(tmp_path).scan()


def test_symlink_ignore_file_is_rejected_without_reading_target(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = write(tmp_path, "outside.txt", "outside content")
    (root / ".gitignore").symlink_to(outside)

    with pytest.raises(ScanError, match="ignore rules.*symlink") as error:
        Scanner(root).scan()
    assert "outside content" not in str(error.value)


@pytest.mark.parametrize("limit", ["MAX_FILES", "MAX_TOTAL_BYTES"])
def test_refresh_limit_errors_preserve_previous_snapshot(
    tmp_path: Path, limit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "one.py", "123")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    write(tmp_path, "two.py", "123")
    monkeypatch.setattr(scanner_module, limit, 1 if limit == "MAX_FILES" else 5)
    forbid_full_scan(monkeypatch, scanner)

    with pytest.raises(ScanError, match=limit):
        scanner.refresh(previous, {"two.py"})
    assert previous.files == {"one.py": source("one.py", "123")}


def test_refresh_limits_use_final_size_not_intermediate_update_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "1")
    write(tmp_path, "z.py", "1234")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    write(tmp_path, "a.py", "1234")
    write(tmp_path, "z.py", "1")
    monkeypatch.setattr(scanner_module, "MAX_TOTAL_BYTES", 5)
    forbid_full_scan(monkeypatch, scanner)

    assert len(scanner.refresh(previous, {"a.py", "z.py"}).files) == 2


def test_watch_filter_is_safe_and_keeps_ignore_and_deletion_events(tmp_path: Path) -> None:
    write(tmp_path, ".gitignore", ".gitignore\n.codecontextignore\n*.log\nignored/\n")
    scanner = Scanner(tmp_path, (Path("state"),))
    scanner.scan()

    for path in ["main.py", "源码/入口.py", ".gitignore", ".codecontextignore", "deleted.py"]:
        assert scanner.watch_filter(3, str(tmp_path / path))
    for path in [".env", ".git/config", "state/db.json", "run.log", "ignored/child.py"]:
        assert not scanner.watch_filter(1, str(tmp_path / path))
    for path in [str(tmp_path.parent / "outside.py"), "../outside.py", "a\\b", "a:b", ""]:
        assert not scanner.watch_filter(1, path)


@pytest.mark.parametrize("replacement", ["missing", "symlink", "file"])
def test_leaf_event_reconciles_all_sources_under_a_replaced_parent(
    tmp_path: Path, replacement: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    write(root, "folder/one.py")
    write(root, "folder/two.py")
    scanner = Scanner(root)
    previous = scanner.scan()
    outside = tmp_path / "moved"
    (root / "folder").rename(outside)
    if replacement == "symlink":
        (root / "folder").symlink_to(outside, target_is_directory=True)
    elif replacement == "file":
        write(root, "folder", "replacement file")

    refreshed = scanner.refresh(previous, {"folder/one.py"})

    assert "folder/one.py" not in refreshed.files
    assert "folder/two.py" not in refreshed.files
    if replacement == "file":
        assert refreshed.files == {"folder": source("folder", "replacement file")}
    elif replacement == "symlink":
        assert refreshed.skipped == {"folder": "symlink"}
    else:
        assert refreshed == ScanResult({}, {})


def test_full_reconciliation_reuses_hashes_for_unchanged_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "main.py")
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    (tmp_path / "new-empty").mkdir()

    def unexpected_hash(content: str) -> str:
        pytest.fail("unchanged content must reuse its hash during reconciliation too")

    monkeypatch.setattr(scanner_module, "content_hash", unexpected_hash)
    refreshed = scanner.refresh(previous, {"new-empty"})

    assert refreshed.files["main.py"] is previous.files["main.py"]


@pytest.mark.parametrize("path", [".env", "ignored.py"])
def test_deleted_excluded_file_skips_are_removed(tmp_path: Path, path: str) -> None:
    write(tmp_path, ".gitignore", "ignored.py\n")
    file = write(tmp_path, path)
    scanner = Scanner(tmp_path)
    previous = scanner.scan()
    assert path in previous.skipped
    file.unlink()

    assert path not in scanner.refresh(previous, {path}).skipped


def test_events_under_pruned_roots_do_not_create_phantom_child_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "state/db.json")
    scanner = Scanner(tmp_path, (Path("state"),))
    previous = scanner.scan()
    forbid_full_scan(monkeypatch, scanner)

    refreshed = scanner.refresh(previous, {"state/db.json", "state/missing.json"})

    assert refreshed == previous


def test_constructor_validates_root_without_scanning_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("constructing a Scanner must not scan files or load ignore rules")

    monkeypatch.setattr(Scanner, "_walk", unexpected)
    monkeypatch.setattr(Scanner, "_read_text", unexpected)

    assert Scanner(tmp_path).root == tmp_path


def test_symlink_replacement_between_stat_and_open_does_not_read_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    file = write(root, "main.py")
    outside = write(tmp_path, "outside.py", "outside content")
    scanner = Scanner(root)
    previous = scanner.scan()
    real_open = os.open

    def open_file(name: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "main.py":
            file.unlink()
            file.symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(scanner_module.os, "open", open_file)
    refreshed = scanner.refresh(previous, {"main.py"})

    assert refreshed == ScanResult({}, {"main.py": "symlink"})
    assert "outside content" not in repr(refreshed)


def test_directory_symlink_replacement_between_stat_and_open_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    write(root, "folder/child.py")
    scanner = Scanner(root)
    previous = scanner.scan()
    real_open = os.open

    def open_directory(name: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "folder":
            (root / "folder").rename(tmp_path / "outside")
            (root / "folder").symlink_to(tmp_path / "outside", target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(scanner_module.os, "open", open_directory)
    with pytest.raises(ScanError, match="incomplete directory scan"):
        scanner.refresh(previous, {"folder/child.py"})
    assert set(previous.files) == {"folder/child.py"}

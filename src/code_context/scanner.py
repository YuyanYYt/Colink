"""Safe UTF-8 snapshots and incremental refreshes of a local source tree.

Only the root .gitignore and .codecontextignore are loaded. Their rules use Git
semantics, with .codecontextignore applied last; neither can override mandatory
policy exclusions or excluded_roots. Ignored directories are pruned, so a child
cannot be re-included without first re-including its parent directory.
"""

import errno
import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from code_context.models import content_hash
from code_context.policy import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    content_problem,
    excluded_path,
    validate_path,
)

_IGNORE_FILES = (".gitignore", ".codecontextignore")


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: str
    sha256: str


@dataclass
class ScanResult:
    files: dict[str, SourceFile]
    skipped: dict[str, str]


class ScanError(RuntimeError):
    """A snapshot could not be completed safely; keep the previous snapshot."""


def _absolute_path(path: Path, base: Path | None = None) -> Path:
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("root and excluded roots must not contain '..'")
    if base is not None and not path.is_absolute():
        path = base / path
    # abspath is lexical: resolve() would follow symlinks before we could reject them.
    return Path(os.path.abspath(path))


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode


def _version(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _io_error(operation: str, path: str, exc: OSError) -> ScanError:
    return ScanError(f"{operation} {path!r}: {exc.strerror or type(exc).__name__}")


class Scanner:
    """Scan a real directory without following symlinks, including root ancestors.

    Relative excluded_roots are relative to root. External excluded roots have
    no effect. Refresh expects a previous snapshot of this same project; normal
    file events read only those files, while directory/ignore-rule events reconcile
    the entire tree. Results are committed only after every required read succeeds.
    """

    def __init__(self, root: Path, excluded_roots: tuple[Path, ...] = ()) -> None:
        if not (
            hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
            and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd
            and os.scandir in os.supports_fd
        ):
            raise ScanError("safe scanning requires nofollow directory-fd support")
        self.root = _absolute_path(root)
        self._excluded: set[str] = set()
        for excluded in excluded_roots:
            absolute = _absolute_path(excluded, self.root)
            if self.root.is_relative_to(absolute):
                raise ValueError("an excluded root must not contain the project root")
            if absolute.is_relative_to(self.root):
                self._excluded.add(validate_path(absolute.relative_to(self.root).as_posix()))
        self._ignore_spec: GitIgnoreSpec | None = None
        self._directories: set[str] = set()
        with self._root_fd():
            pass

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    @contextmanager
    def _root_fd(self) -> Iterator[int]:
        opened: list[int] = []
        links: list[tuple[int, str, os.stat_result]] = []
        try:
            try:
                current = os.open(self.root.anchor, self._directory_flags())
                opened.append(current)
                for name in self.root.parts[1:]:
                    child = os.open(name, self._directory_flags(), dir_fd=current)
                    opened.append(child)
                    links.append((current, name, os.fstat(child)))
                    current = child
            except OSError as exc:
                raise _io_error(
                    "project root must exist and be a real directory without symlinks:",
                    str(self.root),
                    exc,
                ) from exc
            yield current
            # Also reject root/ancestor replacement while the operation was in progress.
            for parent, name, expected in links:
                try:
                    actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except OSError as exc:
                    raise _io_error(
                        "project root became unavailable:", str(self.root), exc
                    ) from exc
                if _identity(actual) != _identity(expected):
                    raise ScanError("project root or an ancestor changed during scanning")
        finally:
            for fd in reversed(opened):
                os.close(fd)

    @staticmethod
    def _stat(fd: int, name: str, path: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _io_error("cannot inspect", path, exc) from exc

    @contextmanager
    def _directory(
        self, parent: int, name: str, path: str, expected: os.stat_result
    ) -> Iterator[int]:
        try:
            fd = os.open(name, self._directory_flags(), dir_fd=parent)
        except OSError as exc:
            raise _io_error("incomplete directory scan; cannot open", path, exc) from exc
        try:
            if _identity(os.fstat(fd)) != _identity(expected):
                raise ScanError(f"directory {path!r} changed before scanning")
            yield fd
            actual = self._stat(parent, name, path)
            if actual is None or _identity(actual) != _identity(expected):
                raise ScanError(
                    f"incomplete directory scan; directory {path!r} changed or vanished"
                )
        finally:
            os.close(fd)

    @contextmanager
    def _parent_fd(
        self, root_fd: int, path: str, directories: set[str]
    ) -> Iterator[tuple[int | None, str | None]]:
        with ExitStack() as stack:
            current = root_fd
            parts = path.split("/")
            for index, name in enumerate(parts[:-1]):
                parent_path = "/".join(parts[: index + 1])
                info = self._stat(current, name, parent_path)
                if info is None:
                    yield None, None
                    return
                if stat.S_ISLNK(info.st_mode):
                    yield None, "symlink ancestor"
                    return
                if not stat.S_ISDIR(info.st_mode):
                    yield None, "ancestor is not a directory"
                    return
                current = stack.enter_context(self._directory(current, name, parent_path, info))
                directories.add(parent_path)
            yield current, None

    def _path_problem(
        self, path: str, spec: GitIgnoreSpec | None, directory: bool = False
    ) -> str | None:
        if excluded_path(path):
            return "mandatory policy exclusion"
        if any(path == root or path.startswith(root + "/") for root in self._excluded):
            return "excluded root"
        if spec is not None:
            parts = path.split("/")
            parents = ("/".join(parts[:index]) + "/" for index in range(1, len(parts)))
            if any(spec.match_file(parent) for parent in parents):
                return "ignored by root ignore rules"
            if spec.match_file(path + "/" if directory else path):
                return "ignored by root ignore rules"
        return None

    def _read_text(self, parent: int, name: str, path: str) -> tuple[str | None, str | None]:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                actual = self._stat(parent, name, path)
                if actual is not None and stat.S_ISLNK(actual.st_mode):
                    return None, "symlink"
            raise _io_error("cannot read", path, exc) from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                return None, "not a regular file"
            if before.st_size > MAX_FILE_BYTES:
                return None, f"file exceeds {MAX_FILE_BYTES} bytes"
            chunks: list[bytes] = []
            remaining = MAX_FILE_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            actual = self._stat(parent, name, path)
            if actual is None:
                return None, None
            if stat.S_ISLNK(actual.st_mode):
                return None, "symlink"
            if _version(before) != _version(after) or _version(after) != _version(actual):
                raise ScanError(f"file {path!r} changed during reading; retry the scan")
            raw = b"".join(chunks)
            if len(raw) > MAX_FILE_BYTES:
                return None, f"file exceeds {MAX_FILE_BYTES} bytes"
            if len(raw) != after.st_size:
                raise ScanError(f"incomplete read of file {path!r}")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None, "invalid UTF-8 text"
            problem = content_problem(content)
            return (None, problem) if problem else (content, None)
        except OSError as exc:
            raise _io_error("cannot read", path, exc) from exc
        finally:
            os.close(fd)

    def _load_ignore(self, root_fd: int) -> GitIgnoreSpec:
        lines: list[str] = []
        for path in _IGNORE_FILES:
            content, problem = self._read_text(root_fd, path, path)
            if problem:
                raise ScanError(f"cannot load root ignore rules from {path!r}: {problem}")
            if content is not None:
                lines.extend(content.splitlines())
        try:
            return GitIgnoreSpec.from_lines(lines)
        except ValueError as exc:
            raise ScanError("invalid root ignore pattern") from exc

    @staticmethod
    def _put(
        result: ScanResult, path: str, content: str, total: int, previous: SourceFile | None = None
    ) -> int:
        total += len(content.encode("utf-8"))
        if len(result.files) >= MAX_FILES:
            raise ScanError(f"snapshot exceeds MAX_FILES ({MAX_FILES})")
        if total > MAX_TOTAL_BYTES:
            raise ScanError(f"snapshot exceeds MAX_TOTAL_BYTES ({MAX_TOTAL_BYTES})")
        source = (
            previous
            if previous is not None and previous.content == content
            else SourceFile(path, content, content_hash(content))
        )
        result.files[path] = source
        return total

    def _walk(
        self,
        fd: int,
        prefix: str,
        spec: GitIgnoreSpec,
        result: ScanResult,
        directories: set[str],
        total: int,
        previous: ScanResult | None = None,
    ) -> int:
        try:
            with os.scandir(fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise _io_error("incomplete directory scan; cannot list", prefix or ".", exc) from exc
        for entry in entries:
            path = validate_path(f"{prefix}/{entry.name}" if prefix else entry.name)
            problem = self._path_problem(path, None)
            if problem:
                result.skipped[path] = problem
                continue
            info = self._stat(fd, entry.name, path)
            if info is None:
                if entry.is_dir(follow_symlinks=False):
                    raise ScanError(f"incomplete directory scan; directory {path!r} vanished")
                continue
            directory = stat.S_ISDIR(info.st_mode)
            if directory:
                directories.add(path)
            problem = self._path_problem(path, spec, directory)
            if problem:
                result.skipped[path] = problem
            elif stat.S_ISLNK(info.st_mode):
                result.skipped[path] = "symlink"
            elif directory:
                with self._directory(fd, entry.name, path, info) as child:
                    total = self._walk(child, path, spec, result, directories, total, previous)
            elif not stat.S_ISREG(info.st_mode):
                result.skipped[path] = "not a regular file"
            else:
                content, problem = self._read_text(fd, entry.name, path)
                if problem:
                    result.skipped[path] = problem
                elif content is not None:
                    old_source = previous.files.get(path) if previous is not None else None
                    total = self._put(result, path, content, total, old_source)
        return total

    def scan(self) -> ScanResult:
        return self._scan()

    def _scan(self, previous: ScanResult | None = None) -> ScanResult:
        result = ScanResult({}, {})
        directories: set[str] = set()
        with self._root_fd() as root_fd:
            spec = self._load_ignore(root_fd)
            self._walk(root_fd, "", spec, result, directories, 0, previous)
        self._ignore_spec = spec
        self._directories = directories
        return result

    def _validate_previous(self, previous: ScanResult) -> None:
        if previous.files.keys() & previous.skipped.keys():
            raise ValueError("previous snapshot contains both source and skip for the same path")
        for path, source in previous.files.items():
            validate_path(path)
            if not isinstance(source, SourceFile) or source.path != path:
                raise ValueError("previous snapshot source path does not match its key")
            if self._path_problem(path, None) or content_problem(source.content):
                raise ValueError(
                    f"previous snapshot contains excluded or unsafe source at {path!r}"
                )
            if not re.fullmatch(r"[a-f0-9]{64}", source.sha256):
                raise ValueError("previous snapshot contains an invalid SHA-256")
        for path, reason in previous.skipped.items():
            validate_path(path)
            if not isinstance(reason, str):
                raise ValueError("previous skip reasons must be strings")

    def refresh(self, previous: ScanResult, changed_paths: set[str]) -> ScanResult:
        """Re-read changed files, preserving unchanged hashes and SourceFile identity.

        Errors leave previous intact. Missing files are deletions, but unreadable
        files/directories and files that change during a read abort the refresh.
        Directory events, including deleted directories, and root ignore changes
        require full reconciliation. No uploads or persistence occur here.
        """
        self._validate_previous(previous)
        changed = {validate_path(path) for path in changed_paths}
        directories = self._directories.copy()
        for path in previous.files.keys() | previous.skipped.keys():
            parts = path.split("/")
            directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
        reconcile = bool(changed & (set(_IGNORE_FILES) | directories))
        if reconcile:
            return self._scan(previous)

        candidates: dict[str, tuple[str | None, str | None]] = {}
        with self._root_fd() as root_fd:
            spec = self._ignore_spec
            if spec is None:
                spec = self._load_ignore(root_fd)
            for path in sorted(changed):
                problem = self._path_problem(path, spec)
                parts = path.split("/")
                if any(
                    self._path_problem("/".join(parts[:index]), spec, directory=True)
                    for index in range(1, len(parts))
                ):
                    # Pruned trees have a skip at their boundary, not phantom child skips.
                    candidates[path] = None, None
                    continue
                with self._parent_fd(root_fd, path, directories) as (parent, parent_problem):
                    if parent is None:
                        # A leaf event can reveal a deleted or replaced parent. Reconcile
                        # its siblings too, rather than retaining stale source beneath it.
                        reconcile = True
                        break
                    name = path.rsplit("/", 1)[-1]
                    info = self._stat(parent, name, path)
                    if info is None:
                        candidates[path] = None, None
                    elif problem:
                        candidates[path] = None, problem
                    elif stat.S_ISDIR(info.st_mode):
                        reconcile = True
                        break
                    elif stat.S_ISLNK(info.st_mode):
                        candidates[path] = None, "symlink"
                    elif not stat.S_ISREG(info.st_mode):
                        candidates[path] = None, "not a regular file"
                    else:
                        candidates[path] = self._read_text(parent, name, path)
        if reconcile:
            return self._scan(previous)

        result = ScanResult(
            {path: source for path, source in previous.files.items() if path not in changed},
            {path: reason for path, reason in previous.skipped.items() if path not in changed},
        )
        total = sum(len(source.content.encode("utf-8")) for source in result.files.values())
        if len(result.files) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ScanError("previous snapshot exceeds current policy limits")
        for path, (content, problem) in candidates.items():
            if problem:
                result.skipped[path] = problem
            elif content is not None:
                total = self._put(result, path, content, total, previous.files.get(path))
        self._ignore_spec = spec
        self._directories = directories
        return result

    def watch_filter(self, change: object, path: str) -> bool:
        """Conservative watchfiles filter; accept removals and potential symlink changes.

        It never opens a path. scan/refresh perform the authoritative safety checks.
        Root ignore-file events always pass so previously ignored files can reappear.
        """
        try:
            candidate = Path(path)
            if ".." in candidate.parts:
                return False
            if candidate.is_absolute():
                relative = candidate.relative_to(self.root).as_posix()
            else:
                relative = path
            validate_path(relative)
        except (TypeError, ValueError):
            return False
        return relative in _IGNORE_FILES or self._path_problem(relative, self._ignore_spec) is None

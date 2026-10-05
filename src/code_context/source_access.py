"""Bounded access to saved source, with no persisted source-body mirror.

Reuse the scanner's nofollow directory-fd and ignore policy. Metadata enumeration
does not read file bodies. Locks coordinate CoLink only; external editors still
require before/after identity and content validation.
"""

import hashlib
import os
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from code_context.fingerprint_cache import FingerprintCache, SourceFingerprints
from code_context.models import content_hash
from code_context.policy import MAX_FILE_BYTES, MAX_FILES, validate_path
from code_context.scanner import ScanError, Scanner, _identity, _version
from code_context.storage import MirrorError


class SourceError(MirrorError):
    """Safe, content-free source failures exposed through the query boundary."""


@dataclass(frozen=True)
class SourceDocument:
    path: str
    content: str
    sha256: str
    size: int
    mode: int
    version: tuple


class SourceAccess:
    def __init__(self, root: Path, excluded_roots=()):
        try:
            self.scanner = Scanner(root, excluded_roots=tuple(excluded_roots))
            with self.scanner._root_fd() as fd:
                self.root_identity = _identity(os.fstat(fd))
                self.ancestor_identity = self._ancestors()
        except (OSError, ValueError, ScanError):
            raise SourceError(
                "SOURCE_UNAVAILABLE: source must be a real authorized directory"
            ) from None
        self.root = self.scanner.root
        raw = repr((str(self.root), self.root_identity, self.ancestor_identity)).encode()
        self.source_id = hashlib.sha256(raw).hexdigest()
        self.lock = threading.RLock()
        self.metrics = {"body_reads": 0, "metadata_walks": 0}
        self._fingerprints = SourceFingerprints(FingerprintCache(max_entries=4096), self.source_id)
        self._ignore_cache = None

    def attach_fingerprint_cache(self, cache: FingerprintCache):
        """Use a backend-owned metadata budget; never transfer or retain bodies.

        Only source -> cache lock ordering is used. A standalone source retains
        its original 4096-entry default until a backend explicitly attaches it.
        """
        if not isinstance(cache, FingerprintCache):
            raise ValueError("invalid fingerprint metadata cache")
        with self.lock:
            if self._fingerprints.cache is not cache:
                self._fingerprints = SourceFingerprints(cache, self.source_id)

    def _ignore(self, root):
        names = (".gitignore", ".codecontextignore")

        def versions():
            return tuple(
                _version(info) if (info := self.scanner._stat(root, name, name)) else None
                for name in names
            )

        before = versions()
        if self._ignore_cache is not None and before == self._ignore_cache[0]:
            if versions() == before:
                return self._ignore_cache[1]
        spec = self.scanner._load_ignore(root)
        if versions() != before:
            raise SourceError("SOURCE_CHANGED: ignore policy changed while reading")
        self._ignore_cache = (before, spec)
        return spec

    def _ancestors(self):
        root = self.scanner.root
        return tuple(_identity(os.lstat(parent)) for parent in reversed(root.parents))

    @contextmanager
    def root_fd(self):
        try:
            with self.lock, self.scanner._root_fd() as fd:
                if (
                    _identity(os.fstat(fd)) != self.root_identity
                    or self._ancestors() != self.ancestor_identity
                ):
                    raise SourceError("SOURCE_REPLACED: reauthorize the source directory")
                yield fd
        except SourceError:
            raise
        except (OSError, ValueError, ScanError):
            raise SourceError(
                "SOURCE_CHANGED: source became unavailable or changed; retry"
            ) from None

    def ensure_available(self):
        with self.root_fd():
            pass

    @contextmanager
    def parent_fd(self, path: str, *, directory=False):
        """Pin the source and every existing parent; never create parents implicitly."""
        try:
            validate_path(path)
        except ValueError:
            raise SourceError("INVALID_PATH: use a normalized project-relative path") from None
        with self.root_fd() as root:
            spec = self._ignore(root)
            if self.scanner._path_problem(path, spec, directory):
                raise SourceError("PATH_EXCLUDED: path is outside the allowed source policy")
            with self.scanner._parent_fd(root, path, set()) as (parent, problem):
                if parent is None or problem:
                    raise SourceError("INVALID_PARENT: parent must be an existing real directory")
                yield parent, path.rsplit("/", 1)[-1]

    def read(self, path: str) -> SourceDocument:
        with self.parent_fd(path) as (parent, name):
            before = self.scanner._stat(parent, name, path)
            if before is None or not stat.S_ISREG(before.st_mode):
                raise SourceError("FILE_UNAVAILABLE: allowed regular text file not found")
            content, problem = self.scanner._read_text(parent, name, path)
            after = self.scanner._stat(parent, name, path)
            if problem or content is None:
                raise SourceError("FILE_EXCLUDED: file is not an allowed bounded UTF-8 text")
            if after is None or _version(before) != _version(after):
                raise SourceError("SOURCE_CHANGED: file changed while reading; retry")
            self.metrics["body_reads"] += 1
            sha256 = content_hash(content)
            self._fingerprints.put(path, _version(after), sha256)
            return SourceDocument(
                path,
                content,
                sha256,
                len(content.encode("utf-8")),
                stat.S_IMODE(after.st_mode),
                _version(after),
            )

    def fingerprint(self, path: str) -> str | None:
        try:
            # Stable dev/inode/size/mtime/ctime let repeated context validation
            # avoid re-reading unchanged bodies. Actual source reads and every
            # write precondition still use the real complete content.
            with self.parent_fd(path) as (parent, name):
                before = self.scanner._stat(parent, name, path)
                cached = self._fingerprints.get(path)
                after = self.scanner._stat(parent, name, path)
                if (
                    cached
                    and before is not None
                    and after is not None
                    and stat.S_ISREG(after.st_mode)
                    and cached[0] == _version(before) == _version(after)
                ):
                    return cached[1]
            return self.read(path).sha256
        except SourceError as exc:
            if str(exc).startswith(
                ("FILE_UNAVAILABLE:", "FILE_EXCLUDED:", "PATH_EXCLUDED:", "INVALID_PARENT:")
            ):
                self._fingerprints.discard(path)
                return None
            raise

    def manifest(self, *, max_files=MAX_FILES, max_directories=10000, max_seconds=5.0):
        """Return bounded metadata, never full text or an immutable historical snapshot."""
        if not 1 <= max_files <= MAX_FILES or max_directories < 1 or max_seconds <= 0:
            raise SourceError("INVALID_BUDGET: invalid discovery limits")
        result, skipped, watch_directories = [], {}, [""]
        started = time.monotonic()
        directories = 0
        partial = False
        with self.root_fd() as root:
            spec = self._ignore(root)

            def walk(fd, prefix):
                nonlocal directories, partial
                directories += 1
                if directories > max_directories or time.monotonic() - started > max_seconds:
                    partial = True
                    return
                with os.scandir(fd) as iterator:
                    # Bound each directory independently before sorting its names.
                    entries = []
                    for entry in iterator:
                        entries.append(entry.name)
                        if len(entries) > max_files + max_directories:
                            partial = True
                            return
                for name in sorted(entries):
                    if time.monotonic() - started > max_seconds:
                        partial = True
                        return
                    path = f"{prefix}/{name}" if prefix else name
                    try:
                        validate_path(path)
                    except ValueError:
                        continue
                    info = self.scanner._stat(fd, name, path)
                    if info is None:
                        continue
                    directory = stat.S_ISDIR(info.st_mode)
                    if self.scanner._path_problem(path, spec, directory):
                        continue
                    if directory:
                        if len(watch_directories) < max_directories:
                            watch_directories.append(path)
                        with self.scanner._directory(fd, name, path, info) as child:
                            walk(child, path)
                        if partial:
                            return
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_size > MAX_FILE_BYTES:
                            if len(skipped) < 100:
                                skipped[path] = "file_size_limit"
                            continue
                        if len(result) >= max_files:
                            partial = True
                            return
                        result.append(
                            {"path": path, "size": info.st_size, "fingerprint": _version(info)}
                        )

            walk(root, "")
            self.metrics["metadata_walks"] += 1
        return {
            "files": sorted(result, key=lambda item: item["path"]),
            "partial": partial,
            "skipped": skipped,
            "directories": min(directories, max_directories),
            "watch_directories": watch_directories,
        }

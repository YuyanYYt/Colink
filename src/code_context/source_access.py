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
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from code_context.fingerprint_cache import FingerprintCache, SourceFingerprints
from code_context.models import content_hash
from code_context.policy import MAX_FILE_BYTES, MAX_FILES, validate_path
from code_context.scanner import ScanError, Scanner, _identity, _version
from code_context.storage import MirrorError

MAX_BATCH_PARENT_FDS = 128
MAX_BATCH_PARENTS = 64


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


class _FingerprintBatch:
    """One thread/nesting scope's bounded FD pool, never a source-body cache.

    Each parent owns its complete nofollow ancestor stack (not just its leaf FD).
    Eviction and batch exit validate every retained link before closing it.
    """

    def __init__(self, source, root, spec, root_fds, max_fds, max_parents):
        self.source, self.root, self.spec = source, root, spec
        self.root_fds, self.max_fds, self.max_parents = root_fds, max_fds, max_parents
        self.parents = OrderedDict()
        self.fds = 0
        self.failed = False

    def parent(self, path):
        parts = path.split("/")
        parent_path, name = path.rpartition("/")[0], parts[-1]
        if not parent_path:
            return self.root, name
        if parent_path in self.parents:
            self.parents.move_to_end(parent_path)
            return self.parents[parent_path][1], name
        depth = len(parts) - 1
        if depth > self.max_fds or not self.max_parents:
            self.source.metrics["fingerprint_batch_fallbacks"] += 1
            return None  # Deep chains use the original single-path safety checks.
        while self.parents and (
            len(self.parents) >= self.max_parents or self.fds + depth > self.max_fds
        ):
            self.release(next(iter(self.parents)))
            self.source.metrics["fingerprint_batch_parent_evictions"] += 1
        with ExitStack() as stack:
            fd, links = self.root, []
            for index, part in enumerate(parts[:-1]):
                relative = "/".join(parts[: index + 1])
                info = self.source.scanner._stat(fd, part, relative)
                if info is None or not stat.S_ISDIR(info.st_mode):
                    raise SourceError("INVALID_PARENT: parent must be a real directory")
                child = stack.enter_context(
                    self.source.scanner._directory(fd, part, relative, info)
                )
                if _version(os.fstat(child)) != _version(info):
                    raise SourceError("SOURCE_CHANGED: parent changed during validation")
                links.append((fd, part, relative, _version(info)))
                fd = child
            self.parents[parent_path] = stack.pop_all(), fd, links
            self.fds += depth
            self.source.metrics["fingerprint_batch_parent_opens"] += depth
            batches = self.source._fingerprint_batches.stack
            metrics = self.source.metrics
            metrics["fingerprint_batch_peak_parent_items"] = max(
                metrics["fingerprint_batch_peak_parent_items"], sum(len(b.parents) for b in batches)
            )
            metrics["fingerprint_batch_peak_retained_fds"] = max(
                metrics["fingerprint_batch_peak_retained_fds"],
                sum(b.root_fds + b.fds for b in batches),
            )
            return fd, name

    def release(self, path, *, validate=True):
        stack, _, links = self.parents.pop(path)
        self.fds -= len(links)
        try:
            if validate:
                for parent, name, relative, expected in links:
                    actual = self.source.scanner._stat(parent, name, relative)
                    if actual is None or _version(actual) != expected:
                        raise SourceError("SOURCE_CHANGED: parent link changed during validation")
        finally:
            # A body failure already rejects the batch; skip link validation then.
            if validate:
                stack.close()
            else:
                stack.__exit__(SourceError, SourceError("validation aborted"), None)

    def close(self, *, validate=True):
        error = None
        for path in list(self.parents):
            try:
                self.release(path, validate=validate)
            except (OSError, ScanError, SourceError):
                error = True
        if error:
            raise SourceError("SOURCE_CHANGED: parent changed during batch validation") from None


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
        self.metrics.update(
            fingerprint_batches=0,
            fingerprint_batch_parent_opens=0,
            fingerprint_batch_parent_evictions=0,
            fingerprint_batch_fallbacks=0,
            fingerprint_batch_peak_parent_items=0,
            fingerprint_batch_peak_retained_fds=0,
        )
        self._fingerprints = SourceFingerprints(FingerprintCache(max_entries=4096), self.source_id)
        self._fingerprint_batches = threading.local()
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

    def _ignore_versions(self, root):
        return tuple(
            _version(info) if (info := self.scanner._stat(root, name, name)) else None
            for name in (".gitignore", ".codecontextignore")
        )

    def _ignore(self, root):
        before = self._ignore_versions(root)
        if self._ignore_cache is not None and before == self._ignore_cache[0]:
            if self._ignore_versions(root) == before:
                return self._ignore_cache[1]
        spec = self.scanner._load_ignore(root)
        if self._ignore_versions(root) != before:
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

    def _fingerprint_scope(self):
        return (
            self.source_id,
            self.root,
            self.root_identity,
            self.ancestor_identity,
            self.scanner,
            self.scanner.root,
            frozenset(self.scanner._excluded),
        )

    def _fingerprint_metadata(self):
        # Use virtual root_fd for current registered authorization and exclusions.
        # Close its entire root stack before returning this metadata-only snapshot.
        with self.root_fd() as root:
            self._ignore(root)
            return (
                self._fingerprint_scope(),
                _version(os.fstat(root)),
                tuple(_version(os.lstat(p)) for p in reversed(self.scanner.root.parents)),
                self._ignore_cache[0],
            )

    @contextmanager
    def fingerprint_batch(self):
        """Validate cached hashes with batch-scoped, nofollow parent FD reuse.

        Source -> Registry (registered root_fd) -> Cache ordering is unchanged.
        Independent nesting pools share a 64-parent / 128-retained-FD ceiling,
        including pinned root ancestors; reserve another root stack for the final
        authorization check. No pool/descriptor survives this context manager.
        Deep absolute roots instead use the original per-path checks, with no
        retained FD pool. Their transient single-path FDs are not pool-budgeted.
        """
        with self.lock:
            batches = getattr(self._fingerprint_batches, "stack", [])
            self._fingerprint_batches.stack = batches
            if getattr(self._fingerprint_batches, "unpooled", False):
                raise SourceError("SOURCE_CHANGED: nested validation FD budget exceeded")
            root_fds = len(self.scanner.root.parts)
            available = (
                MAX_BATCH_PARENT_FDS - sum(b.root_fds + b.fds for b in batches) - 2 * root_fds
            )
            if available < 0:
                if batches:
                    raise SourceError("SOURCE_CHANGED: nested validation FD budget exceeded")
                metadata = self._fingerprint_metadata()
                self._fingerprint_batches.unpooled = True
                self.metrics["fingerprint_batches"] += 1
                self.metrics["fingerprint_batch_fallbacks"] += 1
                try:
                    yield self  # No active batch: fingerprints use safe parent_fd.
                    if metadata != self._fingerprint_metadata():
                        raise SourceError("SOURCE_CHANGED: validation batch scope changed")
                finally:
                    self._fingerprint_batches.unpooled = False
                return
            with self.root_fd() as root:
                spec = self._ignore(root)
                scope, version = self._fingerprint_scope(), _version(os.fstat(root))
                ancestors = tuple(
                    _version(os.lstat(p)) for p in reversed(self.scanner.root.parents)
                )
                ignore = self._ignore_cache[0]
                batch = _FingerprintBatch(
                    self,
                    root,
                    spec,
                    root_fds,
                    available,
                    MAX_BATCH_PARENTS - sum(len(b.parents) for b in batches),
                )
                batches.append(batch)
                self.metrics["fingerprint_batches"] += 1
                try:
                    try:
                        yield self
                    except BaseException:
                        batch.close(validate=False)
                        raise
                    else:
                        batch.close()
                        # Re-enter virtual root_fd: registered sources recheck the
                        # current workspace, project authorization and exclusions.
                        with self.root_fd() as current:
                            if (
                                batch.failed
                                or scope != self._fingerprint_scope()
                                or version != _version(os.fstat(root))
                                or version != _version(os.fstat(current))
                                or ancestors
                                != tuple(
                                    _version(os.lstat(p))
                                    for p in reversed(self.scanner.root.parents)
                                )
                                or ignore != self._ignore_versions(current)
                            ):
                                raise SourceError("SOURCE_CHANGED: validation batch scope changed")
                finally:
                    batches.pop()

    @contextmanager
    def _fingerprint_parent(self, path):
        batches = getattr(self._fingerprint_batches, "stack", ())
        if not batches:
            with self.parent_fd(path) as value:
                yield value
            return
        batch = batches[-1]
        if batch.failed:
            raise SourceError("SOURCE_CHANGED: validation batch was invalidated")
        try:
            validate_path(path)
        except ValueError:
            raise SourceError("INVALID_PATH: use a normalized project-relative path") from None
        if self.scanner._path_problem(path, batch.spec):
            raise SourceError("PATH_EXCLUDED: path is outside the allowed source policy")
        parent = batch.parent(path)
        if parent is None:
            with self.parent_fd(path) as value:
                yield value
        else:
            yield parent

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
            with self._fingerprint_parent(path) as (parent, name):
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
            batches = getattr(self._fingerprint_batches, "stack", ())
            if batches:
                batches[-1].failed = True
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

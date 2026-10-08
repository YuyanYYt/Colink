"""Bounded, journaled, no-overwrite moves inside one authorized live source.

Parents are independently pinned. Every entry is verified before and after the
native rename; no unknown object is copied, removed, overwritten or moved back.
A case-only move uses a durable unique-name intermediate. That intermediate is
an original source inode, never disposable staging. Recovery may finish its
registered second step only after proving the entire captured manifest.
"""

import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import PurePosixPath

from code_context.file_attributes import FileAttributes, capture_directory_attributes
from code_context.file_mutation import _read_named
from code_context.policy import MAX_FILES, SECRET_PATTERNS, content_problem, validate_path
from code_context.recovery_store import encode_metadata
from code_context.scanner import _version
from code_context.source_access import SourceError
from code_context.write_coordinator import WriteError, request_digest, validate_request

MAX_MOVE_ENTRIES = 256
MAX_MOVE_BYTES = 16 * 1024 * 1024
MAX_MOVE_SECONDS = 15
MAX_MOVE_DEPTH = 64
_HASH = re.compile(r"[a-f0-9]{64}")
_TEMP = re.compile(r"\.colink-write-[a-f0-9]{32}\.tmp")


def _identity(info):
    return info.st_dev, info.st_ino


def _stat(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _names(parent, *, limit=MAX_FILES):
    # Bound the scan before materializing it; an arbitrary huge directory is
    # never an unbounded list allocation during a source/target spelling check.
    names = []
    with os.scandir(parent) as entries:
        for entry in entries:
            if len(names) >= limit:
                raise WriteError("WRITE_MOVE_ENTRY_LIMIT: use a narrower source tree")
            names.append(entry.name)
    return sorted(names)


def _join(root, relative):
    return root + "/" + relative if relative else root


def _under(path, root):
    return path == root or path.startswith(root + "/")


def _relocate(path, old, new):
    return new + path[len(old) :]


def _kind(path, raw):
    """Text or a small explicit static-resource format, never arbitrary binary."""
    decoded = raw.decode("utf-8", errors="replace")
    if any(pattern.search(decoded) for pattern in SECRET_PATTERNS):
        raise WriteError("WRITE_MOVE_CONTENT_EXCLUDED: possible credential content")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and content_problem(text) is None:
        return "file"
    extension = PurePosixPath(path).suffix.lower()
    recognized = (
        (extension == ".png" and raw.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension in {".jpg", ".jpeg"} and raw.startswith(b"\xff\xd8\xff"))
        or (extension == ".gif" and raw.startswith((b"GIF87a", b"GIF89a")))
        or (extension == ".webp" and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
        or (extension == ".ico" and raw.startswith(b"\x00\x00\x01\x00"))
        or (extension == ".woff" and raw.startswith(b"wOFF"))
        or (extension == ".woff2" and raw.startswith(b"wOF2"))
        or (extension == ".ttf" and raw.startswith(b"\x00\x01\x00\x00"))
        or (extension == ".otf" and raw.startswith(b"OTTO"))
        or (extension == ".wav" and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE")
        or (extension == ".ogg" and raw.startswith(b"OggS"))
        or (extension == ".mp3" and raw.startswith(b"ID3"))
        or (extension == ".mp4" and raw[4:8] == b"ftyp")
    )
    if not recognized:
        raise WriteError("WRITE_MOVE_UNKNOWN_BINARY: only recognized static resources may move")
    return "binary"


@dataclass
class MoveSnapshot:
    entries: list
    bodies: dict
    total_bytes: int

    @property
    def digest(self):
        if self.entries[0]["kind"] != "directory":
            return self.entries[0]["sha256"]
        return hashlib.sha256(encode_metadata(self.entries).encode()).hexdigest()


def _snapshot(source, parent, name, path, *, target=None, bodies=False):
    started = time.monotonic()
    entries, contents, consumed = [], {}, 0
    device = os.fstat(parent).st_dev
    with source.root_fd() as root:
        spec = source._ignore(root)

        def visit(fd, physical, relative, depth):
            nonlocal consumed
            if (
                len(entries) >= MAX_MOVE_ENTRIES
                or depth > MAX_MOVE_DEPTH
                or time.monotonic() - started > MAX_MOVE_SECONDS
            ):
                raise WriteError("WRITE_MOVE_ENTRY_LIMIT: use a smaller bounded source tree")
            logical = _join(path, relative)
            try:
                validate_path(logical)
                if target is not None:
                    validate_path(_join(target, relative))
            except ValueError:
                raise WriteError("WRITE_MOVE_UNSAFE_TREE: invalid descendant path") from None
            before = _stat(fd, physical)
            if before is None:
                raise WriteError("WRITE_MOVE_CONFLICT: a source entry disappeared")
            directory = stat.S_ISDIR(before.st_mode)
            if source.scanner._path_problem(logical, spec, directory) or (
                target is not None
                and source.scanner._path_problem(_join(target, relative), spec, directory)
            ):
                raise WriteError("WRITE_MOVE_TREE_EXCLUDED: source or target tree is excluded")
            if (
                before.st_uid != os.geteuid()
                or before.st_dev != device
                or stat.S_IMODE(before.st_mode) > 0o777
            ):
                raise WriteError(
                    "WRITE_MOVE_UNSAFE_TREE: owned ordinary same-volume entries required"
                )
            if directory:
                opened = os.open(physical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _version(os.fstat(opened)) != _version(before):
                        raise WriteError("WRITE_MOVE_CONFLICT: directory changed while opening")
                    attributes = capture_directory_attributes(opened).to_record()
                    entries.append(
                        {
                            "rel": relative,
                            "kind": "directory",
                            "version": list(_version(before)),
                            "attributes": attributes,
                        }
                    )
                    for child in _names(opened, limit=MAX_MOVE_ENTRIES):
                        visit(
                            opened,
                            child,
                            child if not relative else relative + "/" + child,
                            depth + 1,
                        )
                    after, named = os.fstat(opened), _stat(fd, physical)
                    if (
                        named is None
                        or _version(before) != _version(after)
                        or _version(after) != _version(named)
                    ):
                        raise WriteError(
                            "WRITE_MOVE_CONFLICT: directory changed during manifest capture"
                        )
                finally:
                    os.close(opened)
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                actual = _read_named(fd, physical, attributes=True)
                kind = _kind(logical, actual.raw)
                if target is not None and _kind(_join(target, relative), actual.raw) != kind:
                    raise WriteError(
                        "WRITE_MOVE_TARGET_TYPE: static resource extension must remain recognized"
                    )
                consumed += len(actual.raw)
                if consumed > MAX_MOVE_BYTES:
                    raise WriteError("WRITE_MOVE_BYTE_LIMIT: use a smaller bounded source tree")
                entries.append(
                    {
                        "rel": relative,
                        "kind": kind,
                        "version": list(_version(actual.info)),
                        "sha256": actual.sha256,
                        "attributes": actual.attributes.to_record(),
                    }
                )
                if bodies:
                    contents[relative] = actual.raw
            else:
                raise WriteError("WRITE_MOVE_UNSAFE_TREE: links and special objects cannot move")

        visit(parent, name, "", 0)
        # The durable intent is bounded independently from the file-byte budget.
        encode_metadata(entries)
    return MoveSnapshot(entries, contents, consumed)


def _matches(expected, actual, *, renamed=False):
    if len(expected) != len(actual):
        return False
    for left, right in zip(expected, actual, strict=True):
        if set(left) != set(right) or any(
            left[key] != right[key] for key in left if key != "version"
        ):
            return False
        before, after = left["version"], right["version"]
        # Rename may change only the root inode's ctime. A descendant ctime
        # change, new inode, chmod, xattr edit or body/time change is a conflict.
        if before != after and not (renamed and not left["rel"] and before[:5] == after[:5]):
            return False
    return True


def _rename_no_replace(source_fd, source_name, target_fd, target_name):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(libc, "renameatx_np", None)
        flags = 0x4  # RENAME_EXCL; no copy or overwrite fallback.
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        flags = 0x1  # RENAME_NOREPLACE.
    else:
        function = None
        flags = 0
    if function is None:
        raise WriteError("WRITE_MOVE_UNSUPPORTED: native no-overwrite rename unavailable")
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    if (
        function(source_fd, os.fsencode(source_name), target_fd, os.fsencode(target_name), flags)
        != 0
    ):
        error = ctypes.get_errno()
        code = (
            "WRITE_MOVE_TARGET_EXISTS"
            if error in {errno.EEXIST, errno.ENOTEMPTY}
            else "WRITE_MOVE_NATIVE_FAILED"
        )
        raise WriteError(code + ": native move was refused; preserve registered materials")


class WriteMove:
    def __init__(self, coordinator):
        self.c, self.store = coordinator, coordinator.store

    @staticmethod
    def literal_exists(parent, name):
        return name in _names(parent)

    def directory_baseline(self, source, path):
        with ExitStack() as stack:
            if path:
                parent, name = stack.enter_context(source.parent_fd(path, directory=True))
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                stack.callback(os.close, fd)
            else:
                fd = stack.enter_context(source.root_fd())
            before = os.fstat(fd)
            record = capture_directory_attributes(fd).to_record()
            if _version(os.fstat(fd)) != _version(before):
                raise WriteError("WRITE_BASELINE_CHANGED: directory attributes changed")
            return {"version": list(_version(before)), "attributes": record}

    def snapshot(self, source, path, *, target=None, bodies=False):
        with source.parent_fd(path) as (parent, name):
            if name not in _names(parent):
                raise WriteError(
                    "WRITE_MOVE_SOURCE_UNAVAILABLE: use the source entry's exact spelling"
                )
            return _snapshot(source, parent, name, path, target=target, bodies=bodies)

    def status(self, project, path):
        self.c.operations._path(path)
        with self.c.lock:
            self.c.guard_read(project)
            source = self.c.source_provider(project)
            with source.lock:
                captured = self.snapshot(source, path)
                result = {
                    "project_id": project,
                    "path": path,
                    "kind": captured.entries[0]["kind"],
                    "sha256": captured.digest,
                    "tree_digest": captured.digest
                    if captured.entries[0]["kind"] == "directory"
                    else None,
                    "entries": len(captured.entries),
                    "total_bytes": captured.total_bytes,
                    "source_mode": "live",
                    "source_is_untrusted": True,
                }
                self.c.guard_read(project)
                return result

    def _scope(self, task, source_path, target_path):
        scope = json.loads(task["metadata"])["scope"]
        if scope is not None and (source_path not in scope or target_path not in scope):
            raise WriteError("WRITE_TASK_PATH_SCOPE: declare both move roots in this task")

    def _parent(self, task_id, source, path):
        from code_context.write_attributes import directory_attributes, task_attributes

        parent_path = path.rpartition("/")[0]
        created = self.store.query(
            "SELECT * FROM files WHERE task_id=? AND path=? AND kind='directory'",
            (task_id, parent_path),
        )
        mappings = self.store.query(
            "SELECT * FROM move_mappings WHERE task_id=? AND path=? AND kind='directory'",
            (task_id, parent_path),
        )
        baseline = self.store.query(
            "SELECT binding FROM move_baselines WHERE task_id=? AND path=?", (task_id, parent_path)
        )
        if created:
            binding = json.loads(created[0]["directory_identity"])
            actual = directory_attributes(source, parent_path, binding["identity"], binding["mode"])
            if actual != task_attributes(self.c, task_id, parent_path):
                raise WriteError("WRITE_MOVE_PARENT_CONFLICT: created parent attributes changed")
        elif mappings:
            binding = json.loads(mappings[0]["binding"])
            actual = self.directory_baseline(source, parent_path)
            if (
                actual["version"][:3] != binding["version"][:3]
                or actual["attributes"] != binding["attributes"]
            ):
                raise WriteError(
                    "WRITE_MOVE_PARENT_CONFLICT: moved parent identity or attributes changed"
                )
        elif baseline:
            binding = json.loads(baseline[0]["binding"])
            actual = self.directory_baseline(source, parent_path)
            if (
                actual["version"][:3] != binding["version"][:3]
                or actual["attributes"] != binding["attributes"]
            ):
                raise WriteError(
                    "WRITE_MOVE_PARENT_CONFLICT: origin parent identity or attributes changed"
                )
        elif parent_path:
            # Legacy schema-1 tasks did not capture parent attributes. They can
            # safely finish their old operations; new moves need a fresh task.
            raise WriteError("WRITE_MOVE_BASELINE_UNAVAILABLE: start a task with move baselines")

    def _validate_origin(self, task, root, captured):
        task_id = task["task_id"]
        files = {
            row["path"]: row
            for row in self.store.query("SELECT * FROM files WHERE task_id=?", (task_id,))
        }
        attributes = {
            row["path"]: row
            for row in self.store.query("SELECT * FROM file_attributes WHERE task_id=?", (task_id,))
        }
        origins = {
            row["path"]: row
            for row in self.store.query("SELECT * FROM manifest WHERE task_id=?", (task_id,))
        }
        directories = {
            row["path"]: row
            for row in self.store.query(
                "SELECT * FROM baseline_directories WHERE task_id=?", (task_id,)
            )
        }
        bindings = {
            row["path"]: json.loads(row["binding"])
            for row in self.store.query("SELECT * FROM move_baselines WHERE task_id=?", (task_id,))
        }
        mappings = {
            row["path"]: row
            for row in self.store.query("SELECT * FROM move_mappings WHERE task_id=?", (task_id,))
        }
        by_origin = {
            row["origin_path"]: row["path"]
            for row in mappings.values()
            if row["origin_path"] is not None
        }
        known = set()
        for path in set(origins) | set(directories):
            current_path = by_origin.get(path, path)
            current = files.get(current_path)
            if _under(current_path, root) and not (
                current and current["kind"] != "directory" and current["last_hash"] is None
            ):
                known.add(current_path)
        for path, row in files.items():
            if _under(path, root) and (row["kind"] == "directory" or row["last_hash"] is not None):
                known.add(path)
        for path, row in mappings.items():
            if _under(path, root) and (
                row["kind"] == "directory"
                or not files.get(path)
                or files[path]["last_hash"] is not None
            ):
                known.add(path)
        actual_paths = {_join(root, entry["rel"]) for entry in captured.entries}
        if known != actual_paths:
            raise WriteError(
                "WRITE_MOVE_ORIGIN_CONFLICT: the source tree differs from the task origin"
            )
        for entry in captured.entries:
            path = _join(root, entry["rel"])
            current, mapping = files.get(path), mappings.get(path)
            if current:
                if entry["kind"] == "directory":
                    if current["kind"] != "directory":
                        raise WriteError("WRITE_MOVE_ORIGIN_CONFLICT: task entry type changed")
                    binding = json.loads(current["directory_identity"])
                    if (
                        entry["version"][:2] != binding["identity"]
                        or stat.S_IMODE(entry["version"][2]) != binding["mode"]
                    ):
                        raise WriteError(
                            "WRITE_MOVE_ORIGIN_CONFLICT: task directory identity changed"
                        )
                elif (
                    current["last_hash"] != entry["sha256"]
                    or json.loads(current["last_version"]) != entry["version"]
                ):
                    raise WriteError(
                        "WRITE_MOVE_ORIGIN_CONFLICT: task file differs from its last saved state"
                    )
                if (
                    path not in attributes
                    or json.loads(attributes[path]["last_record"]) != entry["attributes"]
                ):
                    raise WriteError("WRITE_MOVE_ORIGIN_CONFLICT: task entry attributes changed")
            elif entry["kind"] == "directory":
                binding = json.loads(mapping["binding"]) if mapping else bindings.get(path)
                if (
                    binding is None
                    or entry["version"][:3] != binding["version"][:3]
                    or entry["attributes"] != binding["attributes"]
                ):
                    raise WriteError("WRITE_MOVE_ORIGIN_CONFLICT: origin directory binding changed")
            elif (
                path not in origins
                or origins[path]["sha256"] != entry["sha256"]
                or json.loads(origins[path]["version"]) != entry["version"]
            ):
                raise WriteError("WRITE_MOVE_ORIGIN_CONFLICT: file does not match the task origin")
        return files, attributes, mappings

    def _target(self, task_id, source_path, target_path):
        # One current path cannot represent two original task files. A tombstone
        # or an externally removed origin is not an available replacement slot.
        for row in self.store.query("SELECT path FROM files WHERE task_id=?", (task_id,)):
            if _under(row["path"], target_path):
                raise WriteError("WRITE_MOVE_TARGET_HISTORY: target already has task history")
        occupied = self.store.query(
            "SELECT path FROM manifest WHERE task_id=? "
            "UNION SELECT path FROM baseline_directories WHERE task_id=?",
            (task_id, task_id),
        )
        absences = {
            row["path"]
            for row in self.store.query(
                "SELECT path FROM move_absences WHERE task_id=?", (task_id,)
            )
        }
        mappings = self.store.query(
            "SELECT path,origin_path FROM move_mappings WHERE task_id=?", (task_id,)
        )
        relocated = {row["origin_path"] for row in mappings if row["origin_path"] is not None}
        for row in occupied:
            if (
                _under(row["path"], target_path)
                and row["path"] not in relocated
                and not any(_under(row["path"], old) for old in absences)
            ):
                raise WriteError("WRITE_MOVE_TARGET_HISTORY: an origin target cannot be adopted")
        # Reject aliases to another in-flight comparison path, even on a
        # case-sensitive volume, so its spelling is never silently overwritten.
        for row in mappings:
            if row["path"].casefold() == target_path.casefold() and row["path"] != source_path:
                raise WriteError("WRITE_MOVE_TARGET_HISTORY: target aliases another task path")

    def _parents_match(self, metadata, source_parent, target_parent):
        if (
            list(_identity(os.fstat(source_parent))) != metadata["source_parent"]
            or list(_identity(os.fstat(target_parent))) != metadata["target_parent"]
        ):
            raise WriteError("WRITE_MOVE_PARENT_CONFLICT: a journaled parent was replaced")

    def _checkpoint(self, task_id, request_id, metadata, phase):
        metadata["phase"] = phase
        self.c.operations._metadata(task_id, request_id, metadata, state="committing")

    def move_path(
        self, project, task_id, request_id, source_path, target_path, expected_sha256=None
    ):
        try:
            return self._move_path(
                project, task_id, request_id, source_path, target_path, expected_sha256
            )
        except BaseException:
            if (
                isinstance(task_id, str)
                and isinstance(request_id, str)
                and self.store.query(
                    "SELECT request_id FROM operations WHERE task_id=? AND request_id=? "
                    "AND state IN ('prepared','committing')",
                    (task_id, request_id),
                )
            ):
                self.c.operations._protect(task_id)
                raise WriteError(
                    "WRITE_RECOVERY_REQUIRED: preserve move materials and recover locally"
                ) from None
            raise

    def _move_path(
        self, project, task_id, request_id, source_path, target_path, expected_sha256=None
    ):
        validate_request(request_id)
        self.c.operations._path(source_path)
        self.c.operations._path(target_path)
        if (
            source_path == target_path
            or _under(source_path, target_path)
            or _under(target_path, source_path)
        ):
            raise WriteError(
                "WRITE_MOVE_NESTED: source and target must be distinct non-nested paths"
            )
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or _HASH.fullmatch(expected_sha256) is None
        ):
            raise WriteError("INVALID_EXPECTED_SHA256: use the current file hash or tree digest")
        digest = request_digest(
            {
                "kind": "move_path",
                "source_path": source_path,
                "target_path": target_path,
                "expected_sha256": expected_sha256,
            }
        )
        with self.c.lock:
            source, task, replay = self.c.operations._start(project, task_id, request_id, digest)
            if replay is not None:
                return replay
            self._scope(task, source_path, target_path)
            with source.lock:
                self._parent(task_id, source, source_path)
                self._parent(task_id, source, target_path)
                with (
                    source.parent_fd(source_path) as (source_parent, source_name),
                    source.parent_fd(target_path) as (target_parent, target_name),
                ):
                    if source_name not in _names(source_parent):
                        raise WriteError("WRITE_MOVE_SOURCE_UNAVAILABLE: source spelling changed")
                    captured = _snapshot(
                        source,
                        source_parent,
                        source_name,
                        source_path,
                        target=target_path,
                        bodies=True,
                    )
                    if expected_sha256 is not None and expected_sha256 != captured.digest:
                        raise WriteError(
                            "WRITE_MOVE_HASH_CONFLICT: source differs from the expected hash"
                        )
                    files, attributes, mappings = self._validate_origin(task, source_path, captured)
                    self._target(task_id, source_path, target_path)
                    case_only = (
                        _identity(os.fstat(source_parent)) == _identity(os.fstat(target_parent))
                        and source_name.casefold() == target_name.casefold()
                    )
                    target_info = _stat(target_parent, target_name)
                    if target_name in _names(target_parent) or (
                        target_info is not None
                        and (
                            not case_only
                            or _identity(target_info) != tuple(captured.entries[0]["version"][:2])
                        )
                    ):
                        raise WriteError("WRITE_MOVE_TARGET_EXISTS: target must not exist")
                    if os.fstat(source_parent).st_dev != os.fstat(target_parent).st_dev:
                        raise WriteError(
                            "WRITE_MOVE_CROSS_FILESYSTEM: copy fallback is unavailable"
                        )
                    temp_name = ".colink-write-" + uuid.uuid4().hex + ".tmp" if case_only else None
                    if temp_name is not None and _stat(source_parent, temp_name) is not None:
                        raise WriteError("WRITE_MOVE_TEMP_EXISTS: preserve unexpected intermediate")
                    participants = []
                    for entry in captured.entries:
                        path = _join(source_path, entry["rel"])
                        old = files.get(path)
                        mapping = mappings.get(path)
                        origin_path = (
                            mapping["origin_path"]
                            if mapping
                            else None
                            if old and old["kind"] in {"created", "directory"}
                            else path
                        )
                        participants.append(
                            {
                                "rel": entry["rel"],
                                "origin_path": origin_path,
                                "previous": old,
                                "attributes": attributes.get(path),
                                "first_touch": old is None and entry["kind"] != "directory",
                            }
                        )
                    # Missing, journaled deletions also move with their parent;
                    # otherwise verification would still inspect the old name.
                    deleted = [
                        row
                        for path, row in files.items()
                        if _under(path, source_path)
                        and row["kind"] != "directory"
                        and row["last_hash"] is None
                    ]
                    metadata = {
                        "kind": "move_path",
                        "path": source_path,
                        "source_path": source_path,
                        "target_path": target_path,
                        "source_id": source.source_id,
                        "source_parent": list(_identity(os.fstat(source_parent))),
                        "target_parent": list(_identity(os.fstat(target_parent))),
                        "expected_sha256": expected_sha256,
                        "source_digest": captured.digest,
                        "case_only": case_only,
                        "temp_name": temp_name,
                        "manifest": captured.entries,
                        "participants": participants,
                        "deleted": [
                            {"previous": row, "mapping": mappings.get(row["path"])}
                            for row in deleted
                        ],
                        "phase": "backing_up",
                    }
                    encoded = encode_metadata(metadata)
                    missing = sum(
                        ((len(raw) + 4095) // 4096) * 4096
                        for entry, raw in (
                            (entry, captured.bodies.get(entry["rel"])) for entry in captured.entries
                        )
                        if raw is not None
                        and not self.store.query(
                            "SELECT sha256 FROM objects WHERE sha256=?", (entry["sha256"],)
                        )
                    )
                    self.c.reserve_growth(
                        task_id,
                        object_bytes=missing,
                        metadata_bytes=4 * len(encoded.encode()) + 64 * 1024,
                        additional_files=len(participants),
                        source=source,
                    )
                    self.c.operations._record(task_id, request_id, digest, metadata)
                    try:
                        for participant, entry in zip(participants, captured.entries, strict=True):
                            if entry["kind"] == "directory":
                                continue
                            owner = f"{task_id}:op:{request_id}:before:{entry['rel']}"
                            self.store.put_blob(captured.bodies[entry["rel"]], owner)
                            if participant["first_touch"]:
                                self.store.put_blob(
                                    captured.bodies[entry["rel"]],
                                    f"{task_id}:origin:{participant['origin_path']}",
                                )
                        latest = _snapshot(
                            source, source_parent, source_name, source_path, target=target_path
                        )
                        if not _matches(captured.entries, latest.entries):
                            raise WriteError(
                                "WRITE_MOVE_CONFLICT: source changed before native move"
                            )
                        self._parents_match(metadata, source_parent, target_parent)
                        self._checkpoint(
                            task_id,
                            request_id,
                            metadata,
                            "moving_to_temp" if case_only else "moving_to_target",
                        )
                        self.c._authorized(project, _allow_pending=True)
                        if case_only:
                            _rename_no_replace(source_parent, source_name, source_parent, temp_name)
                            os.fsync(source_parent)
                            isolated = _snapshot(
                                source, source_parent, temp_name, source_path, target=target_path
                            )
                            if not _matches(captured.entries, isolated.entries, renamed=True):
                                raise WriteError(
                                    "WRITE_MOVE_CONFLICT: intermediate differs from captured source"
                                )
                            self._checkpoint(task_id, request_id, metadata, "moving_to_target")
                            self.c._authorized(project, _allow_pending=True)
                            _rename_no_replace(source_parent, temp_name, target_parent, target_name)
                        else:
                            _rename_no_replace(
                                source_parent, source_name, target_parent, target_name
                            )
                        os.fsync(source_parent)
                        if _identity(os.fstat(source_parent)) != _identity(os.fstat(target_parent)):
                            os.fsync(target_parent)
                        installed = _snapshot(source, target_parent, target_name, target_path)
                        if not _matches(
                            captured.entries, installed.entries, renamed=True
                        ) or source_name in _names(source_parent):
                            raise WriteError(
                                "WRITE_MOVE_CONFLICT: installed move or absence was not proved"
                            )
                        self._checkpoint(task_id, request_id, metadata, "installed")
                    except BaseException:
                        self.c.operations._protect(task_id)
                        raise WriteError(
                            "WRITE_RECOVERY_REQUIRED: preserve move materials and recover locally"
                        ) from None
                # Parent context exit validation precedes the durable result.
                try:
                    result = self._complete(source, task, request_id, metadata, installed)
                    self.c.on_change(project)
                    return result
                except BaseException:
                    self.c.operations._protect(task_id)
                    raise WriteError(
                        "WRITE_RECOVERY_REQUIRED: preserve move materials and recover locally"
                    ) from None

    def _release(self, task_id, request_id, metadata, *, aborted=False):
        prefix = f"{task_id}:op:{request_id}:"
        with self.store.transaction() as db:
            db.execute("DELETE FROM object_refs WHERE substr(owner,1,?)=?", (len(prefix), prefix))
            if aborted:
                for participant in metadata["participants"]:
                    if participant["first_touch"]:
                        db.execute(
                            "DELETE FROM object_refs WHERE owner=?",
                            (f"{task_id}:origin:{participant['origin_path']}",),
                        )
        self.store.collect_unreferenced()

    def _complete(self, source, task, request_id, metadata, installed):
        task_id, old, new = task["task_id"], metadata["source_path"], metadata["target_path"]
        # Re-read through current paths after both pinned-parent contexts exit.
        fresh = self.snapshot(source, new)
        if not _matches(installed.entries, fresh.entries):
            raise WriteError("WRITE_MOVE_CONFLICT: installed tree changed before bookkeeping")
        result = {
            "project_id": task["project_id"],
            "task_id": task_id,
            "state": "moved",
            "source_path": old,
            "target_path": new,
            "kind": fresh.entries[0]["kind"],
            "sha256": fresh.digest,
            "tree_digest": fresh.digest if fresh.entries[0]["kind"] == "directory" else None,
            "entries": len(fresh.entries),
            "total_bytes": fresh.total_bytes,
            "source_mutation_repeated": False,
        }
        metadata["phase"] = "confirmed"
        with self.store.transaction() as db:
            old_rows = db.execute(
                "SELECT path FROM move_mappings WHERE task_id=?", (task_id,)
            ).fetchall()
            for row in old_rows:
                if _under(row["path"], old):
                    db.execute(
                        "DELETE FROM move_mappings WHERE task_id=? AND path=?",
                        (task_id, row["path"]),
                    )
            for absence in db.execute(
                "SELECT path FROM move_absences WHERE task_id=?", (task_id,)
            ).fetchall():
                if _under(absence["path"], old) and absence["path"] != old:
                    db.execute(
                        "UPDATE move_absences SET path=? WHERE task_id=? AND path=?",
                        (_relocate(absence["path"], old, new), task_id, absence["path"]),
                    )
            for participant, entry in zip(metadata["participants"], fresh.entries, strict=True):
                before_path, after_path = _join(old, entry["rel"]), _join(new, entry["rel"])
                previous, attrs = participant["previous"], participant["attributes"]
                if previous:
                    db.execute(
                        "DELETE FROM files WHERE task_id=? AND path=?", (task_id, before_path)
                    )
                    db.execute(
                        "DELETE FROM file_attributes WHERE task_id=? AND path=?",
                        (task_id, before_path),
                    )
                if entry["kind"] != "directory":
                    db.execute(
                        "INSERT INTO files VALUES(?,?,?,?,?,?,?,NULL)",
                        (
                            task_id,
                            after_path,
                            previous["kind"] if previous else "modified",
                            previous["origin_hash"] if previous else entry["sha256"],
                            previous["origin_mode"] if previous else entry["attributes"]["mode"],
                            entry["sha256"],
                            encode_metadata(entry["version"]),
                        ),
                    )
                    db.execute(
                        "INSERT INTO file_attributes VALUES(?,?,?,?)",
                        (
                            task_id,
                            after_path,
                            attrs["origin_record"]
                            if attrs
                            else encode_metadata(entry["attributes"]),
                            encode_metadata(entry["attributes"]),
                        ),
                    )
                elif previous:
                    binding = json.loads(previous["directory_identity"])
                    if not entry["rel"]:
                        binding["parent_identity"] = metadata["target_parent"]
                    db.execute(
                        "INSERT INTO files VALUES(?,?,?,NULL,NULL,NULL,NULL,?)",
                        (task_id, after_path, "directory", encode_metadata(binding)),
                    )
                    db.execute(
                        "INSERT INTO file_attributes VALUES(?,?,?,?)",
                        (
                            task_id,
                            after_path,
                            attrs["origin_record"] if attrs else None,
                            encode_metadata(entry["attributes"]),
                        ),
                    )
                db.execute(
                    "INSERT INTO move_mappings VALUES(?,?,?,?,?)",
                    (
                        task_id,
                        after_path,
                        participant["origin_path"],
                        entry["kind"],
                        encode_metadata(
                            {"version": entry["version"], "attributes": entry["attributes"]}
                        ),
                    ),
                )
            for deletion in metadata["deleted"]:
                previous = deletion["previous"]
                before_path, after_path = previous["path"], _relocate(previous["path"], old, new)
                attribute = db.execute(
                    "SELECT * FROM file_attributes WHERE task_id=? AND path=?",
                    (task_id, before_path),
                ).fetchone()
                mapping = deletion["mapping"]
                db.execute(
                    "UPDATE files SET path=? WHERE task_id=? AND path=?",
                    (after_path, task_id, before_path),
                )
                if attribute:
                    db.execute(
                        "UPDATE file_attributes SET path=? WHERE task_id=? AND path=?",
                        (after_path, task_id, before_path),
                    )
                if mapping:
                    db.execute(
                        "INSERT INTO move_mappings VALUES(?,?,?,?,?)",
                        (
                            task_id,
                            after_path,
                            mapping["origin_path"],
                            mapping["kind"],
                            mapping["binding"],
                        ),
                    )
                elif previous["kind"] == "modified":
                    db.execute(
                        "INSERT INTO move_mappings VALUES(?,?,?,?,?)",
                        (task_id, after_path, before_path, "file", "{}"),
                    )
            db.execute("DELETE FROM move_absences WHERE task_id=? AND path=?", (task_id, new))
            db.execute(
                "INSERT OR REPLACE INTO move_absences VALUES(?,?,?)",
                (task_id, old, encode_metadata(metadata["source_parent"])),
            )
            db.execute(
                "UPDATE operations SET state='done',metadata=?,result=? "
                "WHERE task_id=? AND request_id=?",
                (encode_metadata(metadata), encode_metadata(result), task_id, request_id),
            )
        self._release(task_id, request_id, metadata)
        return result

    def _metadata(self, source, task, operation, metadata):
        try:
            old, new = metadata["source_path"], metadata["target_path"]
            self.c.operations._path(old)
            self.c.operations._path(new)
            if (
                metadata["path"] != old
                or old == new
                or _under(old, new)
                or _under(new, old)
                or metadata["source_id"] != source.source_id
            ):
                raise ValueError
            self._scope(task, old, new)
            if operation["digest"] != request_digest(
                {
                    "kind": "move_path",
                    "source_path": old,
                    "target_path": new,
                    "expected_sha256": metadata["expected_sha256"],
                }
            ):
                raise ValueError
            for key in ("source_parent", "target_parent"):
                if (
                    type(metadata[key]) is not list
                    or len(metadata[key]) != 2
                    or any(type(value) is not int or value < 0 for value in metadata[key])
                ):
                    raise ValueError
            entries, participants = metadata["manifest"], metadata["participants"]
            if (
                type(entries) is not list
                or not 1 <= len(entries) <= MAX_MOVE_ENTRIES
                or len(participants) != len(entries)
                or entries[0]["rel"] != ""
            ):
                raise ValueError
            relative = []
            for entry, participant in zip(entries, participants, strict=True):
                rel = entry["rel"]
                validate_path(_join(old, rel))
                validate_path(_join(new, rel))
                if (
                    rel in relative
                    or participant["rel"] != rel
                    or entry["kind"] not in {"file", "binary", "directory"}
                ):
                    raise ValueError
                relative.append(rel)
                if (
                    type(entry["version"]) is not list
                    or len(entry["version"]) != 6
                    or any(type(value) is not int or value < 0 for value in entry["version"])
                ):
                    raise ValueError
                FileAttributes.from_record(entry["attributes"])
                if entry["kind"] != "directory" and _HASH.fullmatch(entry["sha256"]) is None:
                    raise ValueError
                origin = participant["origin_path"]
                if origin is not None:
                    validate_path(origin)
                if type(participant["first_touch"]) is not bool:
                    raise ValueError
                if participant["first_touch"] and (
                    entry["kind"] == "directory"
                    or origin is None
                    or participant["previous"] is not None
                ):
                    raise ValueError
            snapshot = MoveSnapshot(
                entries,
                {},
                sum(entry["version"][3] for entry in entries if entry["kind"] != "directory"),
            )
            if (
                snapshot.total_bytes > MAX_MOVE_BYTES
                or snapshot.digest != metadata["source_digest"]
                or (
                    metadata["expected_sha256"] is not None
                    and metadata["expected_sha256"] != snapshot.digest
                )
            ):
                raise ValueError
            if (
                type(metadata["case_only"]) is not bool
                or (
                    metadata["case_only"]
                    and (
                        _TEMP.fullmatch(metadata["temp_name"]) is None
                        or metadata["source_parent"] != metadata["target_parent"]
                        or old.rsplit("/", 1)[-1].casefold() != new.rsplit("/", 1)[-1].casefold()
                    )
                )
                or (not metadata["case_only"] and metadata["temp_name"] is not None)
            ):
                raise ValueError
            if metadata["phase"] not in {
                "backing_up",
                "moving_to_temp",
                "moving_to_target",
                "installed",
            }:
                raise ValueError
            files, attributes, mappings = self._validate_origin(task, old, snapshot)
            for entry, participant in zip(entries, participants, strict=True):
                path = _join(old, entry["rel"])
                previous, mapping = files.get(path), mappings.get(path)
                origin = (
                    mapping["origin_path"]
                    if mapping
                    else None
                    if previous and previous["kind"] in {"created", "directory"}
                    else path
                )
                if (
                    participant["previous"] != previous
                    or participant["attributes"] != attributes.get(path)
                    or participant["origin_path"] != origin
                    or participant["first_touch"]
                    != (previous is None and entry["kind"] != "directory")
                ):
                    raise ValueError
            deleted = {
                path: {"previous": row, "mapping": mappings.get(path)}
                for path, row in files.items()
                if _under(path, old) and row["kind"] != "directory" and row["last_hash"] is None
            }
            if type(metadata["deleted"]) is not list or len(metadata["deleted"]) != len(deleted):
                raise ValueError
            for deletion in metadata["deleted"]:
                path = deletion["previous"]["path"]
                if deletion != deleted.pop(path):
                    raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError, SourceError):
            raise WriteError("WRITE_RECOVERY_METADATA: preserve invalid move journal") from None

    def _verify_backups(self, task_id, request_id, metadata):
        for participant, entry in zip(metadata["participants"], metadata["manifest"], strict=True):
            if entry["kind"] == "directory":
                continue
            owner = f"{task_id}:op:{request_id}:before:{entry['rel']}"
            refs = self.store.query("SELECT sha256 FROM object_refs WHERE owner=?", (owner,))
            if not refs or refs[0]["sha256"] != entry["sha256"]:
                raise WriteError("WRITE_RECOVERY_OBJECT: complete move backup required")
            self.store.read_blob(entry["sha256"])
            if participant["first_touch"]:
                origins = self.store.query(
                    "SELECT sha256 FROM object_refs WHERE owner=?",
                    (f"{task_id}:origin:{participant['origin_path']}",),
                )
                if not origins or origins[0]["sha256"] != entry["sha256"]:
                    raise WriteError("WRITE_RECOVERY_OBJECT: protected move origin required")

    def recover(self, source, task, operation, metadata):
        self._metadata(source, task, operation, metadata)
        old, new = metadata["source_path"], metadata["target_path"]
        with (
            source.parent_fd(old) as (source_parent, source_name),
            source.parent_fd(new) as (target_parent, target_name),
        ):
            self._parents_match(metadata, source_parent, target_parent)
            self._parent(task["task_id"], source, old)
            self._parent(task["task_id"], source, new)
            source_present = source_name in _names(source_parent)
            target_present = target_name in _names(target_parent)
            temp = metadata["temp_name"]
            temp_present = temp is not None and _stat(source_parent, temp) is not None
            if source_present and not target_present and not temp_present:
                current = _snapshot(source, source_parent, source_name, old, target=new)
                if not _matches(metadata["manifest"], current.entries):
                    raise WriteError("WRITE_RECOVERY_CONFLICT: original move source changed")
                self._validate_origin(task, old, current)
                outcome = {"state": "aborted", "source_mutation_repeated": False}
                metadata["phase"] = "aborted"
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE operations SET state='aborted',metadata=?,result=? "
                        "WHERE task_id=? AND request_id=?",
                        (
                            encode_metadata(metadata),
                            encode_metadata(outcome),
                            task["task_id"],
                            operation["request_id"],
                        ),
                    )
                self._release(task["task_id"], operation["request_id"], metadata, aborted=True)
                return outcome
            if temp_present and not source_present and not target_present and metadata["case_only"]:
                if metadata["phase"] not in {"moving_to_temp", "moving_to_target"}:
                    raise WriteError(
                        "WRITE_RECOVERY_CONFLICT: intermediate disagrees with journal phase"
                    )
                current = _snapshot(source, source_parent, temp, old, target=new)
                if not _matches(metadata["manifest"], current.entries, renamed=True):
                    raise WriteError(
                        "WRITE_RECOVERY_CONFLICT: intermediate is not the captured source"
                    )
                self._verify_backups(task["task_id"], operation["request_id"], metadata)
                self._checkpoint(
                    task["task_id"], operation["request_id"], metadata, "moving_to_target"
                )
                if not self.c._alive():
                    raise WriteError("LOCAL_CONTROL_UNAVAILABLE: recovery authorization was lost")
                _rename_no_replace(source_parent, temp, target_parent, target_name)
                os.fsync(source_parent)
                target_present, temp_present = True, False
            if (
                not source_present
                and target_present
                and not temp_present
                and metadata["phase"] in {"moving_to_target", "installed"}
            ):
                installed = _snapshot(source, target_parent, target_name, new)
                if not _matches(metadata["manifest"], installed.entries, renamed=True):
                    raise WriteError(
                        "WRITE_RECOVERY_CONFLICT: moved target changed or was replaced"
                    )
                self._verify_backups(task["task_id"], operation["request_id"], metadata)
            else:
                raise WriteError(
                    "WRITE_RECOVERY_CONFLICT: move location or identity cannot be proved"
                )
        # Complete only after revalidating both parent path contexts.
        self._complete(source, task, operation["request_id"], metadata, installed)
        return {"state": "confirmed_move", "source_mutation_repeated": False}

    def verify(self, task, source):
        """Prove moved-directory bindings and exact old-name absences for Diff."""
        task_id = task["task_id"]
        files = {
            row["path"]: row
            for row in self.store.query("SELECT * FROM files WHERE task_id=?", (task_id,))
        }
        for row in self.store.query("SELECT * FROM move_absences WHERE task_id=?", (task_id,)):
            with source.parent_fd(row["path"]) as (parent, name):
                if list(_identity(os.fstat(parent))) != json.loads(
                    row["parent_identity"]
                ) or name in _names(parent):
                    raise WriteError("WRITE_DIFF_CONFLICT: a moved source name or parent changed")
        directories = [
            row
            for row in self.store.query(
                "SELECT * FROM move_mappings WHERE task_id=? ORDER BY path", (task_id,)
            )
            if row["kind"] == "directory"
        ]
        for row in directories:
            # Top-level moved roots validate the whole tree once. Each nested
            # mapped directory remains in that tree's authoritative known set.
            if any(
                other["path"] != row["path"] and _under(row["path"], other["path"])
                for other in directories
            ):
                continue
            captured = self.snapshot(source, row["path"])
            self._validate_origin(task, row["path"], captured)
        for row in self.store.query(
            "SELECT path FROM move_mappings WHERE task_id=? AND kind='binary'", (task_id,)
        ):
            current = files.get(row["path"])
            if current and current["last_hash"] is not None:
                captured = self.snapshot(source, row["path"])
                if captured.digest != current["last_hash"] or captured.entries[0][
                    "version"
                ] != json.loads(current["last_version"]):
                    raise WriteError("WRITE_DIFF_CONFLICT: a moved binary changed")

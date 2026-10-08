"""Verified APFS cache quotas, pinned key directories and inactive-entry LRU.

Only keys recorded inside this application's private, fixed cache image are
eligible for eviction. Active leases and an acquire's requested key are never
evicted. Native quotas bound actual job writes; admission headroom is advisory.
"""

import hashlib
import os
import plistlib
import posixpath
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager

from code_context.execution_sandbox import GIB, BoundedDisk, _run
from code_context.local_control import private_directory, read_state, write_state
from code_context.scanner import _identity, _version
from code_context.source_access import SourceError

KEY = re.compile(r"[a-f0-9]{64}")
PROJECT = re.compile(r"[a-f0-9]{24}")
PROJECT_LIMIT = 2 * GIB
GLOBAL_LIMIT = 4 * GIB
ADMISSION_HEADROOM = 32 * 1024**2
MAX_PROJECTS = 64
MAX_KEYS = 128
MAX_TREE_ENTRIES = 100_000


def _persistent_identity(info):
    # APFS inode + birth time survive image reattachment; st_dev does not.
    birth = getattr(info, "st_birthtime_ns", None)
    if birth is None:
        birth = int(getattr(info, "st_birthtime", 0) * 1_000_000_000)
    return [info.st_ino, birth, info.st_uid]


@contextmanager
def _directory(parent, name, *, expected=None, removed=False, private=True):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        raise SourceError("CACHE_IDENTITY_CHANGED: cache directories must be real") from None
    try:
        info = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            info.st_uid != os.getuid()
            or info.st_mode & (0o077 if private else 0o022)
            or _identity(info) != _identity(named)
            or expected is not None
            and _persistent_identity(info) != expected
        ):
            raise SourceError("CACHE_IDENTITY_CHANGED: cache directory identity changed")
        yield fd, info
        if not removed and _identity(
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        ) != _identity(info):
            raise SourceError("CACHE_IDENTITY_CHANGED: cache directory was replaced")
    finally:
        os.close(fd)


class ExecutionCache:
    def __init__(self, root, *, clock=None):
        self.state = private_directory(root)
        self.lock = threading.RLock()
        self.disk = None
        self.projects, self.references = {}, {}
        self.access, self.keys, self.key_access = {}, {}, {}
        self._clock = clock or time.time
        self.image_identity = self.container_uuid = self.base_volume_uuid = None
        self.evicted = 0
        self.capacity = {}

    def _save_projects(self):
        write_state(
            self.state,
            "cache.json",
            {
                "version": 2,
                "projects": self.projects,
                "image_identity": self.image_identity,
                "container_uuid": self.container_uuid,
                "base_volume_uuid": self.base_volume_uuid,
            },
        )

    def _save_keys(self, project):
        write_state(self.state, "keys-" + project + ".json", {"keys": self.keys[project]})

    def _container(self):
        try:
            result = plistlib.loads(
                _run(["/usr/sbin/diskutil", "apfs", "list", "-plist", self.disk.container])
            )
            containers = [
                item
                for item in result["Containers"]
                if item["ContainerReference"] == self.disk.container
            ]
            if len(containers) != 1:
                raise ValueError
            container = containers[0]
            if (
                not 0 < container["CapacityCeiling"] <= GLOBAL_LIMIT
                or not 0 <= container["CapacityFree"] <= container["CapacityCeiling"]
                or self.container_uuid is not None
                and container["APFSContainerUUID"] != self.container_uuid
            ):
                raise ValueError
            return container
        except (KeyError, TypeError, ValueError, plistlib.InvalidFileException):
            raise SourceError("CACHE_QUOTA_UNVERIFIED: fixed cache container changed") from None

    def _open(self):
        if self.disk is not None:
            self.disk.verify()
            self._container()
            return
        image = self.state.root / "cache.sparsebundle"
        exists = image.exists() or image.is_symlink()
        if not exists:
            self.disk = BoundedDisk(self.state.root, "cache", size=GLOBAL_LIMIT)
            self.projects = {}
        else:
            metadata = read_state(self.state, "cache.json")
            image_access = private_directory(image)
            with image_access.root_fd() as parent:
                self.image_identity = _persistent_identity(os.fstat(parent))
            if metadata.get("image_identity", self.image_identity) != self.image_identity:
                raise SourceError("CACHE_IMAGE_CHANGED: owned cache image was replaced")
            self.projects = metadata["projects"]
            if not isinstance(self.projects, dict) or len(self.projects) > MAX_PROJECTS:
                raise SourceError("CACHE_METADATA_INVALID")
            self.container_uuid = metadata.get("container_uuid")
            self.base_volume_uuid = metadata.get("base_volume_uuid")
            try:
                info = plistlib.loads(
                    _run(
                        [
                            "/usr/sbin/diskutil",
                            "image",
                            "attach",
                            "--noMount",
                            "--plist",
                            str(image),
                        ]
                    )
                )
                entities = info["system-entities"]
                disk = object.__new__(BoundedDisk)
                disk.state, disk.image, disk.size = self.state, image, GLOBAL_LIMIT
                disk.device = next(
                    item["dev-entry"]
                    for item in entities
                    if item.get("content-hint") == "GUID_partition_scheme"
                )
                disk.container = next(
                    item["dev-entry"]
                    for item in entities
                    if item.get("content-hint") == "Apple_APFS_Container"
                )
                disk.volume = next(
                    item["dev-entry"]
                    for item in entities
                    if item.get("volume-name") == "CoLink-cache"
                )
                disk.mount = self.state.root / "cache-mount"
                with self.state.root_fd() as parent:
                    try:
                        os.mkdir(disk.mount.name, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    with _directory(parent, disk.mount.name):
                        pass
                _run(
                    [
                        "/usr/sbin/diskutil",
                        "mount",
                        "-mountOptions",
                        "noowners,nobrowse",
                        "-mountPoint",
                        str(disk.mount),
                        disk.volume,
                    ]
                )
                disk.access = private_directory(disk.mount)
                disk.verify()
                self.disk = disk
            except (KeyError, StopIteration, TypeError, ValueError):
                raise SourceError(
                    "CACHE_IMAGE_UNVERIFIED: cache attachment could not be proved"
                ) from None
        with private_directory(image).root_fd() as parent:
            self.image_identity = _persistent_identity(os.fstat(parent))
        container = self._container()
        base = [
            item for item in container["Volumes"] if item["DeviceIdentifier"] == self.disk.volume
        ]
        if len(base) != 1 or self.base_volume_uuid not in (None, base[0]["APFSVolumeUUID"]):
            raise SourceError("CACHE_IMAGE_CHANGED: base cache volume changed")
        self.container_uuid = container["APFSContainerUUID"]
        self.base_volume_uuid = base[0]["APFSVolumeUUID"]
        self._save_projects()

    def _volume_info(self, project):
        entry = self.projects[project]
        mount = self.state.root / ("p-" + project)
        try:
            info = plistlib.loads(_run(["/usr/sbin/diskutil", "info", "-plist", entry["uuid"]]))
            container = self._container()
            volumes = [
                item for item in container["Volumes"] if item["APFSVolumeUUID"] == entry["uuid"]
            ]
            if (
                len(volumes) != 1
                or volumes[0]["CapacityQuota"] != PROJECT_LIMIT
                or volumes[0]["Name"] != entry["name"]
                or info.get("VolumeUUID") != entry["uuid"]
                or info.get("MountPoint") != str(mount)
                or info.get("APFSContainerReference") != self.disk.container
                or info.get("FilesystemType") != "apfs"
                or not info.get("WritableVolume")
                or not 0 <= volumes[0]["CapacityInUse"] <= PROJECT_LIMIT
            ):
                raise ValueError
            capacity = {
                "used_bytes": volumes[0]["CapacityInUse"],
                "project_free_bytes": PROJECT_LIMIT - volumes[0]["CapacityInUse"],
                "global_free_bytes": container["CapacityFree"],
            }
            self.capacity[project] = capacity
            return capacity
        except (KeyError, TypeError, ValueError, plistlib.InvalidFileException):
            raise SourceError("CACHE_QUOTA_UNVERIFIED: project volume quota changed") from None

    def _project(self, project):
        if PROJECT.fullmatch(project) is None:
            raise SourceError("INVALID_CACHE_PROJECT")
        mount = self.state.root / ("p-" + project)
        entry = self.projects.get(project)
        if entry is None:
            if len(self.projects) >= MAX_PROJECTS:
                raise SourceError("CACHE_PROJECT_LIMIT")
            output = _run(
                [
                    "/usr/sbin/diskutil",
                    "apfs",
                    "addVolume",
                    self.disk.container,
                    "APFS",
                    "CoLink-p-" + project,
                    "-quota",
                    str(PROJECT_LIMIT),
                    "-nomount",
                ]
            )
            match = re.search(rb"Disk from APFS operation: (disk[0-9]+s[0-9]+)", output)
            if not match:
                raise SourceError("CACHE_QUOTA_UNVERIFIED")
            info = plistlib.loads(_run(["/usr/sbin/diskutil", "info", "-plist", match[1].decode()]))
            entry = {"uuid": info["VolumeUUID"], "name": "CoLink-p-" + project}
            self.projects[project] = entry
            self._save_projects()
        elif not isinstance(entry, dict) or entry.get("name") != "CoLink-p-" + project:
            raise SourceError("CACHE_METADATA_INVALID")
        if not os.path.ismount(mount):
            with self.state.root_fd() as parent:
                try:
                    os.mkdir(mount.name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                with _directory(parent, mount.name):
                    pass
            _run(
                [
                    "/usr/sbin/diskutil",
                    "mount",
                    "-mountOptions",
                    "noowners,nobrowse",
                    "-mountPoint",
                    str(mount),
                    entry["uuid"],
                ]
            )
        self._volume_info(project)
        if project not in self.access:
            # chmod only a checked real mount after UUID/container/quota proof.
            if not stat.S_ISDIR(os.lstat(mount).st_mode):
                raise SourceError("CACHE_VOLUME_CHANGED")
            os.chmod(mount, 0o700, follow_symlinks=False)
            self.access[project] = private_directory(mount)
        else:
            self.access[project].ensure_available()
        if project not in self.keys:
            metadata = self.state.root / ("keys-" + project + ".json")
            if metadata.exists() or metadata.is_symlink():
                keys = read_state(self.state, metadata.name)["keys"]
                if (
                    not isinstance(keys, dict)
                    or len(keys) > MAX_KEYS
                    or any(KEY.fullmatch(k) is None for k in keys)
                ):
                    raise SourceError("CACHE_METADATA_INVALID")
                self.keys[project] = keys
            else:
                self.keys[project] = {}
            for key, record in tuple(self.keys[project].items()):
                if record.get("evicting"):
                    with self.access[project].root_fd() as parent:
                        try:
                            os.stat(key, dir_fd=parent, follow_symlinks=False)
                        except FileNotFoundError:
                            self.keys[project].pop(key)
                            self._save_keys(project)
                            continue
                    self._evict(project, key)
        return mount

    def _key(self, project, key):
        record = self.keys[project].get(key)
        access = self.access[project]
        with access.root_fd() as parent:
            if record is None:
                if len(self.keys[project]) >= MAX_KEYS:
                    raise SourceError("CACHE_KEY_LIMIT: release inactive entries before replanning")
                try:
                    os.mkdir(key, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    raise SourceError(
                        "CACHE_UNOWNED_KEY: existing key has no private ownership record"
                    ) from None
                with _directory(parent, key) as (_, info):
                    record = {"identity": _persistent_identity(info), "last_used": self._clock()}
                self.keys[project][key] = record
                self._save_keys(project)
            with _directory(parent, key, expected=record["identity"]):
                pass
        path = access.root / key
        known = self.key_access.get((project, key))
        if known is None:
            self.key_access[(project, key)] = private_directory(path)
        else:
            known.ensure_available()
        if record.get("artifact_replace"):
            self._recover_artifact(project, key)
        return path

    def _recover_artifact(self, project, key):
        record = self.keys[project][key]
        intent = record["artifact_replace"]
        name, staging = intent["name"], intent["staging"]
        if (
            name not in {"node_modules", "target"}
            or re.fullmatch(r"\.capture-[a-f0-9]{32}", staging) is None
        ):
            raise SourceError("CACHE_METADATA_INVALID")
        if self.references.get((project, key), 0) > 1:
            raise SourceError(
                "CACHE_ARTIFACT_RECOVERY_PENDING: another active lease needs this key"
            )
        access = self.key_access[(project, key)]
        with access.root_fd() as parent:
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is None or _persistent_identity(current) != intent["new"]["identity"]:
                if current is not None:
                    if (
                        intent["old_identity"] is None
                        or _persistent_identity(current) != intent["old_identity"]
                    ):
                        raise SourceError(
                            "CACHE_ARTIFACT_IDENTITY_CHANGED: replacement target changed"
                        )
                    with _directory(parent, name, expected=intent["old_identity"]) as (old_fd, _):
                        self._erase_contents(old_fd)
                    os.rmdir(name, dir_fd=parent)
                with _directory(parent, staging, expected=intent["new"]["identity"]):
                    pass
                os.rename(staging, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
        record.setdefault("artifacts", {})[name] = intent["new"]
        record.pop("artifact_replace")
        self._save_keys(project)

    def _after_artifact_remove(self):
        """Crash seam after an owned old artifact is removed, before publish."""

    def _after_artifact_publish(self):
        """Crash seam after rename, before the private ownership manifest commits."""

    @staticmethod
    def _erase_contents(fd):
        remaining = MAX_TREE_ENTRIES
        device = os.fstat(fd).st_dev

        def erase(parent, depth):
            nonlocal remaining
            if depth > 64:
                raise SourceError("CACHE_EVICTION_LIMIT: cache tree is too deep")
            for entry in os.scandir(parent):
                remaining -= 1
                if remaining < 0:
                    raise SourceError("CACHE_EVICTION_LIMIT: cache tree is too large")
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device:
                    raise SourceError("CACHE_EVICTION_UNSAFE: nested filesystems are excluded")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                    )
                    try:
                        pinned = os.fstat(child)
                        if _identity(pinned) != _identity(info):
                            raise SourceError(
                                "CACHE_IDENTITY_CHANGED: cache contents raced eviction"
                            )
                        erase(child, depth + 1)
                        if _identity(
                            os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
                        ) != _identity(pinned):
                            raise SourceError(
                                "CACHE_IDENTITY_CHANGED: cache contents were replaced"
                            )
                        os.rmdir(entry.name, dir_fd=parent)
                    finally:
                        os.close(child)
                else:
                    # Symlinks are unlinked as names; their targets are never read.
                    if _identity(
                        os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
                    ) != _identity(info):
                        raise SourceError("CACHE_IDENTITY_CHANGED: cache contents were replaced")
                    os.unlink(entry.name, dir_fd=parent)
            os.fsync(parent)

        erase(fd, 0)

    def _evict(self, project, key):
        if self.references.get((project, key), 0):
            raise SourceError("CACHE_IN_USE: active cache cannot be evicted")
        record = self.keys[project][key]
        with self.access[project].root_fd() as parent:
            with _directory(parent, key, expected=record["identity"], removed=True) as (fd, info):
                record["evicting"] = True
                self._save_keys(project)
                self._erase_contents(fd)
                if _identity(os.stat(key, dir_fd=parent, follow_symlinks=False)) != _identity(info):
                    raise SourceError("CACHE_IDENTITY_CHANGED: cache key raced eviction")
                os.rmdir(key, dir_fd=parent)
                os.fsync(parent)
        self.keys[project].pop(key)
        self.key_access.pop((project, key), None)
        self._save_keys(project)
        self.evicted += 1

    def _make_room(self, project, key, reserve):
        capacity = self._volume_info(project)
        if min(capacity["project_free_bytes"], capacity["global_free_bytes"]) >= reserve:
            return
        # Mount only already-recorded app volumes, so global pressure can reclaim
        # another project's inactive keys without creating a second full cache.
        for other in tuple(self.projects):
            if other != project:
                self._project(other)
        candidates = sorted(
            (record["last_used"], candidate_project, candidate_key)
            for candidate_project, keys in self.keys.items()
            for candidate_key, record in keys.items()
            if (candidate_project, candidate_key) != (project, key)
            and not self.references.get((candidate_project, candidate_key), 0)
        )
        if capacity["project_free_bytes"] < reserve:
            candidates.sort(key=lambda item: (item[1] != project, item[0]))
        for _, candidate_project, candidate_key in candidates:
            if capacity["project_free_bytes"] < reserve and candidate_project != project:
                continue
            self._evict(candidate_project, candidate_key)
            capacity = self._volume_info(project)
            if min(capacity["project_free_bytes"], capacity["global_free_bytes"]) >= reserve:
                return
        raise SourceError(
            "CACHE_CAPACITY_EXHAUSTED: active or requested cache data prevents admission"
        )

    def acquire(self, project_id, key, *, reserve_bytes=ADMISSION_HEADROOM):
        if (
            not isinstance(project_id, str)
            or not 1 <= len(project_id) <= 256
            or not isinstance(key, str)
            or KEY.fullmatch(key) is None
            or type(reserve_bytes) is not int
            or not 0 <= reserve_bytes <= PROJECT_LIMIT
        ):
            raise SourceError("INVALID_CACHE_KEY")
        project = hashlib.sha256(project_id.encode()).hexdigest()[:24]
        with self.lock:
            self._open()
            self._project(project)
            self._make_room(project, key, reserve_bytes)
            if key not in self.keys[project] and len(self.keys[project]) >= MAX_KEYS:
                inactive = sorted(
                    (value["last_used"], name)
                    for name, value in self.keys[project].items()
                    if not self.references.get((project, name), 0)
                )
                if not inactive:
                    raise SourceError("CACHE_KEY_LIMIT: all retained keys are active")
                self._evict(project, inactive[0][1])
            cache = self._key(project, key)
            self.keys[project][key]["last_used"] = self._clock()
            self._save_keys(project)
            self.references[(project, key)] = self.references.get((project, key), 0) + 1
            return cache

    def _lease(self, project_id, key):
        project = hashlib.sha256(project_id.encode()).hexdigest()[:24]
        if not self.references.get((project, key), 0):
            raise SourceError("CACHE_LEASE_REQUIRED: workspace artifacts need an active lease")
        self._open()
        self._project(project)
        self._key(project, key)
        return project, self.key_access[(project, key)]

    @staticmethod
    def _tree_size(fd):
        """Bound admission with metadata; native quotas remain authoritative."""
        count, total = 0, 0
        device = os.fstat(fd).st_dev

        def measure(parent, depth):
            nonlocal count, total
            if depth > 64:
                raise SourceError("CACHE_ARTIFACT_LIMIT")
            before = os.fstat(parent)
            for entry in os.scandir(parent):
                count += 1
                if count > MAX_TREE_ENTRIES:
                    raise SourceError("CACHE_ARTIFACT_LIMIT")
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device or info.st_uid != os.getuid():
                    raise SourceError("CACHE_ARTIFACT_UNSAFE: external artifact storage excluded")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                    )
                    try:
                        if _version(os.fstat(child)) != _version(info):
                            raise SourceError("CACHE_ARTIFACT_CHANGED")
                        measure(child, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += info.st_size
                elif not stat.S_ISLNK(info.st_mode):
                    raise SourceError("CACHE_ARTIFACT_UNSAFE: unsupported artifact entry")
                if total > PROJECT_LIMIT:
                    raise SourceError("CACHE_ARTIFACT_LIMIT")
                if _version(os.stat(entry.name, dir_fd=parent, follow_symlinks=False)) != _version(
                    info
                ):
                    raise SourceError("CACHE_ARTIFACT_CHANGED")
            if _version(os.fstat(parent)) != _version(before):
                raise SourceError("CACHE_ARTIFACT_CHANGED")

        try:
            measure(fd, 0)
        except OSError:
            raise SourceError("CACHE_ARTIFACT_CHANGED: stable owned artifact required") from None
        return total

    @staticmethod
    def _copy_tree(source, destination):
        """Copy only pinned owned files and relative links inside the artifact."""
        count, total = 0, 0
        device = os.fstat(source).st_dev

        def copy(parent, target, prefix, depth):
            nonlocal count, total
            if depth > 64:
                raise SourceError("CACHE_ARTIFACT_LIMIT")
            before = os.fstat(parent)
            for entry in sorted(os.scandir(parent), key=lambda item: item.name):
                count += 1
                if count > MAX_TREE_ENTRIES or len(entry.name.encode()) > 255:
                    raise SourceError("CACHE_ARTIFACT_LIMIT")
                info = entry.stat(follow_symlinks=False)
                relative = prefix + entry.name
                if info.st_uid != os.getuid() or info.st_dev != device:
                    raise SourceError(
                        "CACHE_ARTIFACT_UNSAFE: external artifact storage is excluded"
                    )
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                    )
                    out = None
                    try:
                        if _version(os.fstat(child)) != _version(info):
                            raise SourceError("CACHE_ARTIFACT_CHANGED")
                        os.mkdir(entry.name, mode=0o700, dir_fd=target)
                        out = os.open(
                            entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target
                        )
                        copy(child, out, relative + "/", depth + 1)
                    finally:
                        os.close(child)
                        if out is not None:
                            os.close(out)
                elif stat.S_ISLNK(info.st_mode):
                    link = os.readlink(entry.name, dir_fd=parent)
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), link))
                    if (
                        not link
                        or len(link.encode()) > 4096
                        or link.startswith("/")
                        or resolved == ".."
                        or resolved.startswith("../")
                    ):
                        raise SourceError(
                            "CACHE_ARTIFACT_UNSAFE: links must remain within the artifact"
                        )
                    os.symlink(link, entry.name, dir_fd=target)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    source_fd = os.open(
                        entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                    target_fd = None
                    try:
                        if _version(os.fstat(source_fd)) != _version(info):
                            raise SourceError("CACHE_ARTIFACT_CHANGED")
                        target_fd = os.open(
                            entry.name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o700 if info.st_mode & 0o111 else 0o600,
                            dir_fd=target,
                        )
                        while True:
                            chunk = os.read(source_fd, 64 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > PROJECT_LIMIT:
                                raise SourceError("CACHE_ARTIFACT_LIMIT")
                            while chunk:
                                chunk = chunk[os.write(target_fd, chunk) :]
                        os.fsync(target_fd)
                        if _version(os.fstat(source_fd)) != _version(info):
                            raise SourceError("CACHE_ARTIFACT_CHANGED")
                    finally:
                        os.close(source_fd)
                        if target_fd is not None:
                            os.close(target_fd)
                else:
                    raise SourceError(
                        "CACHE_ARTIFACT_UNSAFE: regular files without hardlinks required"
                    )
                if _version(os.stat(entry.name, dir_fd=parent, follow_symlinks=False)) != _version(
                    info
                ):
                    raise SourceError("CACHE_ARTIFACT_CHANGED")
            if _version(os.fstat(parent)) != _version(before):
                raise SourceError("CACHE_ARTIFACT_CHANGED")
            os.fsync(target)

        try:
            copy(source, destination, "", 0)
        except OSError:
            raise SourceError(
                "CACHE_ARTIFACT_COPY_FAILED: bounded storage or stable artifacts required"
            ) from None
        return total

    def capture_workspace(self, project_id, key, workspace, *, input_digest=None):
        if input_digest is not None and KEY.fullmatch(input_digest) is None:
            raise SourceError("INVALID_CACHE_INPUT_DIGEST")
        workspace_access = private_directory(workspace)
        captured, skipped = [], []
        with self.lock:
            project, access = self._lease(project_id, key)
            record = self.keys[project][key]
            artifacts = record.setdefault("artifacts", {})
            with workspace_access.root_fd() as source, access.root_fd() as destination:
                for name in ("node_modules", "target"):
                    if name == "target" and input_digest is None:
                        continue
                    try:
                        original = os.stat(name, dir_fd=source, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISDIR(original.st_mode):
                        raise SourceError("CACHE_ARTIFACT_UNSAFE: artifact roots cannot be links")
                    old = artifacts.get(name)
                    if old and self.references[(project, key)] > 1:
                        skipped.append(name)
                        continue
                    with _directory(source, name, private=False) as (input_fd, _):
                        required = self._tree_size(input_fd)
                    self._make_room(project, key, min(PROJECT_LIMIT, required + 1024**2))
                    staging = ".capture-" + uuid.uuid4().hex
                    os.mkdir(staging, mode=0o700, dir_fd=destination)
                    staging_identity = _persistent_identity(
                        os.stat(staging, dir_fd=destination, follow_symlinks=False)
                    )
                    try:
                        with _directory(source, name, expected=None, private=False) as (
                            input_fd,
                            _,
                        ):
                            with _directory(destination, staging, expected=staging_identity) as (
                                output_fd,
                                _,
                            ):
                                size = self._copy_tree(input_fd, output_fd)
                        # Only CoLink's separately recorded artifact root may be
                        # replaced. Arbitrary cache paths are never overwritten.
                        try:
                            existing = os.stat(name, dir_fd=destination, follow_symlinks=False)
                        except FileNotFoundError:
                            existing = None
                        if existing is not None:
                            if not old or _persistent_identity(existing) != old["identity"]:
                                raise SourceError("CACHE_ARTIFACT_IDENTITY_CHANGED")
                        replacement = {
                            "identity": staging_identity,
                            "bytes": size,
                            "input_digest": input_digest if name == "target" else None,
                        }
                        record["artifact_replace"] = {
                            "name": name,
                            "staging": staging,
                            "new": replacement,
                            "old_identity": old["identity"] if old else None,
                        }
                        self._save_keys(project)
                        if existing is not None:
                            with _directory(destination, name, expected=old["identity"]) as (
                                old_fd,
                                _,
                            ):
                                self._erase_contents(old_fd)
                            os.rmdir(name, dir_fd=destination)
                            self._after_artifact_remove()
                        os.rename(staging, name, src_dir_fd=destination, dst_dir_fd=destination)
                        os.fsync(destination)
                        self._after_artifact_publish()
                        artifacts[name] = replacement
                        record.pop("artifact_replace")
                        self._save_keys(project)
                        captured.append(name)
                    except BaseException:
                        # Staging is uniquely created inside this pinned leased
                        # key. Removing it is normal app runtime cleanup only.
                        if not record.get("artifact_replace"):
                            try:
                                with _directory(
                                    destination, staging, expected=staging_identity
                                ) as (stage_fd, _):
                                    self._erase_contents(stage_fd)
                                os.rmdir(staging, dir_fd=destination)
                            except (SourceError, OSError):
                                pass
                        raise
            self._volume_info(project)
        return {
            "state": "captured" if captured else "unchanged",
            "artifacts": captured,
            "active_retained": skipped,
        }

    def restore_workspace(self, project_id, key, workspace, *, input_digest=None):
        if input_digest is not None and KEY.fullmatch(input_digest) is None:
            raise SourceError("INVALID_CACHE_INPUT_DIGEST")
        workspace_access = private_directory(workspace)
        restored = []
        with self.lock:
            project, access = self._lease(project_id, key)
            artifacts = self.keys[project][key].get("artifacts", {})
            with access.root_fd() as source, workspace_access.root_fd() as destination:
                for name in ("node_modules", "target"):
                    artifact = artifacts.get(name)
                    if (
                        not artifact
                        or name == "target"
                        and artifact["input_digest"] != input_digest
                    ):
                        continue
                    try:
                        os.stat(name, dir_fd=destination, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise SourceError(
                            "CACHE_ARTIFACT_DESTINATION_EXISTS: preserve existing workspace paths"
                        )
                    staging = ".restore-" + uuid.uuid4().hex
                    os.mkdir(staging, mode=0o700, dir_fd=destination)
                    staging_identity = _persistent_identity(
                        os.stat(staging, dir_fd=destination, follow_symlinks=False)
                    )
                    try:
                        with _directory(source, name, expected=artifact["identity"]) as (
                            input_fd,
                            _,
                        ):
                            with _directory(destination, staging, expected=staging_identity) as (
                                output_fd,
                                _,
                            ):
                                self._copy_tree(input_fd, output_fd)
                        os.rename(staging, name, src_dir_fd=destination, dst_dir_fd=destination)
                        os.fsync(destination)
                        restored.append(name)
                    except BaseException:
                        try:
                            with _directory(destination, staging, expected=staging_identity) as (
                                stage_fd,
                                _,
                            ):
                                self._erase_contents(stage_fd)
                            os.rmdir(staging, dir_fd=destination)
                        except (SourceError, OSError):
                            pass
                        raise
        return {"state": "restored" if restored else "empty", "artifacts": restored}

    def release(self, project_id, key):
        project = hashlib.sha256(project_id.encode()).hexdigest()[:24]
        with self.lock:
            count = self.references.get((project, key), 0)
            if not count:
                return
            self._key(project, key)
            self.keys[project][key]["last_used"] = self._clock()
            self._save_keys(project)
            if count <= 1:
                self.references.pop((project, key))
            else:
                self.references[(project, key)] = count - 1

    def status(self):
        return {
            "project_limit_bytes": PROJECT_LIMIT,
            "global_limit_bytes": GLOBAL_LIMIT,
            "hard_limit": "verified APFS volume quotas and fixed cache disk",
            "admission_headroom_bytes": ADMISSION_HEADROOM,
            "active_leases": sum(tuple(self.references.values())),
            "retained_keys": sum(len(keys) for keys in tuple(self.keys.values())),
            "evicted_keys": self.evicted,
        }

    def close(self):
        with self.lock:
            if self.references:
                raise SourceError("CACHE_IN_USE: stop confirmed jobs before detaching")
            if self.disk:
                self.disk.close()
                self.disk = None
                self.access.clear()
                self.key_access.clear()

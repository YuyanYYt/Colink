"""Recover only privately journaled, stopped, identity-verified CoLink task disks.

Call with scheduling stopped. This never adopts processes or mounts an image.
Unrecognized files, incomplete native proof or live references remain untouched
and block new terminal grants. Cache images and development/test images without
production job records are deliberately outside this recovery scope.
"""

import json
import math
import os
import plistlib
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from code_context.execution_store import TERMINAL_STATES
from code_context.local_control import _read_state, private_directory, write_state
from code_context.scanner import Scanner, _version
from code_context.source_access import SourceAccess, SourceError

JOB = re.compile(r"job-[a-f0-9]{32}")
DEVICE = re.compile(r"(?:/dev/)?disk[0-9]+(?:s[0-9]+)*")
UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")
MAX_TASK_BYTES = 2 * 1024**3
MAX_RECORDS = 4096
MAX_ENTRIES = 131072
MAX_NATIVE_BYTES = 4 * 1024**2
MAX_DISK_RECORD_BYTES = 8192
RECORD_FIELDS = frozenset(
    {
        "version",
        "job_id",
        "project_id",
        "source_id",
        "image_basename",
        "root_identity",
        "image_identity",
        "mount",
        "mount_underlying",
        "device",
        "container",
        "volume",
        "container_uuid",
        "volume_uuid",
        "mounted_identity",
        "size",
        "cleanup_verified",
        "cleanup_origin",
        "recovery_resource_id",
        "execution_started",
        "scope_retired",
        "state",
        "updated_at",
    }
)


class DiskRecoveryError(SourceError):
    """Failures do not include paths, metadata values or native tool output."""


def disk_identity(info):
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "birth_ns": int(info.st_birthtime * 1e9) if hasattr(info, "st_birthtime") else None,
        "uid": info.st_uid,
    }


def _identity(value):
    return (
        isinstance(value, dict)
        and set(value) == {"dev", "ino", "birth_ns", "uid"}
        and type(value["dev"]) is int
        and value["dev"] >= 0
        and type(value["ino"]) is int
        and value["ino"] > 0
        and type(value["uid"]) is int
        and value["uid"] == os.geteuid()
        and (
            value["birth_ns"] is None or (type(value["birth_ns"]) is int and value["birth_ns"] > 0)
        )
    )


def _device(value):
    if not isinstance(value, str) or DEVICE.fullmatch(value) is None:
        raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
    return value.removeprefix("/dev/")


class NativeDiskRecovery:
    """Read exact image/device mappings before any normal, non-forced eject."""

    @staticmethod
    def _run(argv):
        if sys.platform != "darwin":
            raise DiskRecoveryError("TASK_DISK_RECOVERY_UNAVAILABLE")
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
                close_fds=True,
                timeout=10,
                check=False,
            )
            if result.returncode or len(result.stdout) > MAX_NATIVE_BYTES:
                raise DiskRecoveryError("TASK_DISK_NATIVE_UNVERIFIED")
            return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            raise DiskRecoveryError("TASK_DISK_NATIVE_UNVERIFIED") from None

    @classmethod
    def _plist(cls, argv):
        try:
            result = plistlib.loads(cls._run(argv))
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, TypeError, OverflowError, plistlib.InvalidFileException):
            raise DiskRecoveryError("TASK_DISK_NATIVE_UNVERIFIED") from None

    def attachments(self, image):
        inventory = self._plist(["/usr/bin/hdiutil", "info", "-plist"])
        images = inventory.get("images")
        if not isinstance(images, list) or len(images) > MAX_RECORDS:
            raise DiskRecoveryError("TASK_DISK_NATIVE_UNVERIFIED")
        try:
            expected = disk_identity(image.lstat())
        except FileNotFoundError:
            expected = None
        matches = []
        for row in images:
            if not isinstance(row, dict):
                continue
            path = row.get("image-path")
            if path == str(image):
                matches.append(row)
                continue
            if expected is not None and isinstance(path, str) and len(path) <= 4096:
                try:
                    # Metadata only: detect another attachment through a parent
                    # or final symlink. An alias cannot authorize an eject.
                    current = disk_identity(os.stat(path))
                except (OSError, ValueError):
                    continue
                if current == expected:
                    raise DiskRecoveryError("TASK_DISK_ATTACHMENT_ALIAS")
        if len(matches) > 1:
            raise DiskRecoveryError("TASK_DISK_ATTACHMENT_AMBIGUOUS")
        return matches

    def verify_attachment(self, record, entry):
        entities = entry.get("system-entities")
        if (
            entry.get("owner-uid") != os.geteuid()
            or not isinstance(entities, list)
            or len(entities) > 32
            or any(not isinstance(row, dict) for row in entities)
            or type(entry.get("blockcount")) is not int
            or type(entry.get("blocksize")) is not int
            or not 0 < entry["blockcount"] * entry["blocksize"] <= record["size"]
            or entry.get("image-encrypted") is not False
        ):
            raise DiskRecoveryError("TASK_DISK_ATTACHMENT_CHANGED")
        devices = {_device(row.get("dev-entry")) for row in entities}
        if not {_device(record[key]) for key in ("device", "container", "volume")} <= devices:
            raise DiskRecoveryError("TASK_DISK_ATTACHMENT_CHANGED")
        roots = [row for row in entities if row.get("content-hint") == "GUID_partition_scheme"]
        mounts = [row for row in entities if row.get("mount-point")]
        if (
            len(roots) != 1
            or _device(roots[0].get("dev-entry")) != _device(record["device"])
            or len(mounts) != 1
            or mounts[0].get("mount-point") != record["mount"]
            or _device(mounts[0].get("dev-entry")) != _device(record["volume"])
        ):
            raise DiskRecoveryError("TASK_DISK_ATTACHMENT_CHANGED")
        volume = self._plist(["/usr/sbin/diskutil", "info", "-plist", record["volume"]])
        containers = self._plist(
            ["/usr/sbin/diskutil", "apfs", "list", "-plist", record["container"]]
        ).get("Containers")
        if (
            not isinstance(containers, list)
            or len(containers) != 1
            or not isinstance(containers[0], dict)
        ):
            raise DiskRecoveryError("TASK_DISK_ATTACHMENT_CHANGED")
        container = containers[0]
        volumes = container.get("Volumes")
        if (
            volume.get("VolumeUUID") != record["volume_uuid"]
            or volume.get("MountPoint") != record["mount"]
            or volume.get("FilesystemType") != "apfs"
            or volume.get("WritableVolume") is not True
            or _device(volume.get("APFSContainerReference")) != _device(record["container"])
            or container.get("APFSContainerUUID") != record["container_uuid"]
            or _device(container.get("ContainerReference")) != _device(record["container"])
            or type(container.get("CapacityCeiling")) is not int
            or not 0 < container["CapacityCeiling"] <= record["size"]
            or not isinstance(volumes, list)
            or len(volumes) != 1
            or not isinstance(volumes[0], dict)
            or volumes[0].get("APFSVolumeUUID") != record["volume_uuid"]
            or _device(volumes[0].get("DeviceIdentifier")) != _device(record["volume"])
        ):
            raise DiskRecoveryError("TASK_DISK_ATTACHMENT_CHANGED")
        try:
            mount = SourceAccess(record["mount"])
            with mount.root_fd() as fd:
                if disk_identity(os.fstat(fd)) != record["mounted_identity"]:
                    raise DiskRecoveryError("TASK_DISK_MOUNT_CHANGED")
        except (SourceError, OSError):
            raise DiskRecoveryError("TASK_DISK_MOUNT_CHANGED") from None

    def detach(self, record, entry, image):
        current = self.attachments(image)
        if current != [entry]:
            raise DiskRecoveryError("TASK_DISK_ATTACHMENT_CHANGED")
        self.verify_attachment(record, current[0])
        self._run(["/usr/sbin/diskutil", "eject", record["device"]])
        if self.attachments(image):
            raise DiskRecoveryError("TASK_DISK_DETACH_UNCONFIRMED")


class TaskDiskRecovery:
    def __init__(
        self,
        root,
        source_for,
        job_for,
        *,
        native=None,
        clock=time.time,
        cleanup_proof=None,
        scope_expire=None,
    ):
        self.state = private_directory(root)
        self.source_for, self.job_for = source_for, job_for
        self.native = native or NativeDiskRecovery()
        self.clock = clock
        self.cleanup_proof = cleanup_proof
        self.scope_expire = scope_expire
        self.lock = threading.RLock()

    def _validate(self, record, job_id):
        if (
            set(record) - RECORD_FIELDS
            or len(json.dumps(record, ensure_ascii=False).encode("utf-8")) > MAX_DISK_RECORD_BYTES
            or type(record.get("version")) is not int
            or record["version"] != 1
            or record.get("job_id") != job_id
            or record.get("image_basename") != job_id + ".sparsebundle"
            or record.get("state")
            not in {"creating", "attached", "detached", "retiring", "retired"}
            or type(record.get("cleanup_verified")) is not bool
            or not _identity(record.get("root_identity"))
            or type(record.get("size")) is not int
            or not 0 < record["size"] <= MAX_TASK_BYTES
            or type(record.get("updated_at")) not in (float, int)
            or not math.isfinite(record["updated_at"])
            or record["updated_at"] > self.clock() + 60
            or not isinstance(record.get("project_id"), str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record["project_id"]) is None
            or not isinstance(record.get("source_id"), str)
            or re.fullmatch(r"[a-f0-9]{64}", record["source_id"]) is None
        ):
            raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
        with self.state.root_fd() as parent:
            if disk_identity(os.fstat(parent)) != record["root_identity"]:
                raise DiskRecoveryError("TASK_DISK_ROOT_CHANGED")
        if "cleanup_origin" in record or "recovery_resource_id" in record:
            if (
                record.get("cleanup_origin") != "verified_retired_resource_scope"
                or record["cleanup_verified"] is not True
                or type(record.get("recovery_resource_id")) is not int
                or not 1 < record["recovery_resource_id"] < 2**64
            ):
                raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
        if "execution_started" in record and type(record["execution_started"]) is not bool:
            raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
        if "scope_retired" in record and (
            record["scope_retired"] is not True
            or record["cleanup_verified"] is not True
            or record["state"] != "retired"
        ):
            raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
        for key in ("image_identity", "mount_underlying", "mounted_identity"):
            if record.get(key) is not None and not _identity(record[key]):
                raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
        for key in ("container_uuid", "volume_uuid"):
            if record.get(key) is not None and (
                not isinstance(record[key], str) or UUID.fullmatch(record[key]) is None
            ):
                raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
        for key in ("device", "container", "volume"):
            if record.get(key) is not None:
                _device(record[key])
        mount = record.get("mount")
        if mount is not None:
            if not isinstance(mount, str) or len(mount.encode("utf-8")) > 1024:
                raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
            path = Path(mount)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or str(path) != mount
                or not (
                    path == self.state.root / (job_id + "-mount")
                    or (
                        path.parent == Path("/private/tmp")
                        and re.fullmatch(r"cl-w-[A-Za-z0-9_-]{1,80}", path.name) is not None
                    )
                )
            ):
                raise DiskRecoveryError("TASK_DISK_MOUNT_SCOPE")

    def _eligible(self, record):
        try:
            source = self.source_for(record["project_id"])
            source.ensure_available()
            if source.source_id != record["source_id"]:
                raise DiskRecoveryError("TASK_DISK_SOURCE_CHANGED")
        except (SourceError, KeyError):
            raise DiskRecoveryError("TASK_DISK_SOURCE_CHANGED") from None
        job = self.job_for(record["job_id"])
        if (
            not isinstance(job, dict)
            or job.get("project_id") != record["project_id"]
            or job.get("source_id") != record["source_id"]
        ):
            raise DiskRecoveryError("TASK_DISK_JOB_UNVERIFIED")
        if job.get("state") not in TERMINAL_STATES:
            raise DiskRecoveryError("TASK_DISK_STILL_REFERENCED")
        snapshot = job.get("snapshot") or {}
        if not isinstance(snapshot, dict):
            raise DiskRecoveryError("TASK_DISK_JOB_UNVERIFIED")
        if record["cleanup_verified"] is not True:
            if job["state"] != "interrupted" or self.cleanup_proof is None:
                raise DiskRecoveryError("TASK_DISK_CLEANUP_UNVERIFIED")
            try:
                proof = self.cleanup_proof(record, job)
            except (OSError, ValueError, SourceError):
                raise DiskRecoveryError("TASK_DISK_CLEANUP_UNVERIFIED") from None
            if (
                not isinstance(proof, dict)
                or set(proof) != {"job_id", "resource_id", "cleanup_verified", "launchd_retired"}
                or proof["job_id"] != record["job_id"]
                or proof["cleanup_verified"] is not True
                or proof["launchd_retired"] is not True
                or type(proof["resource_id"]) is not int
                or not 1 < proof["resource_id"] < 2**64
            ):
                raise DiskRecoveryError("TASK_DISK_CLEANUP_UNVERIFIED")
            # Proof acquisition can take time. Recheck both durable bindings
            # before committing the independently verified cleanup receipt.
            self._current(record)
            source.ensure_available()
            if self.job_for(record["job_id"]) != job:
                raise DiskRecoveryError("TASK_DISK_JOB_CHANGED")
            record = {
                **record,
                "cleanup_verified": True,
                "cleanup_origin": "verified_retired_resource_scope",
                "recovery_resource_id": proof["resource_id"],
                "updated_at": self.clock(),
            }
            write_state(self.state, record["job_id"] + ".disk.json", record)
        if (
            job["state"] != "interrupted"
            and snapshot.get("cleanup_verified") is not True
            and snapshot.get("tree_scope") != "not_started"
        ):
            raise DiskRecoveryError("TASK_DISK_CLEANUP_UNVERIFIED")
        return record

    def _current(self, record):
        current = _read_state(self.state, record["job_id"] + ".disk.json")[0]
        if current != record:
            raise DiskRecoveryError("TASK_DISK_RECORD_CHANGED")
        self._validate(current, record["job_id"])

    def _persist(self, record, state):
        self._current(record)
        saved = {**record, "state": state, "updated_at": self.clock()}
        write_state(self.state, record["job_id"] + ".disk.json", saved)
        return saved

    def _image_info(self, record):
        with self.state.root_fd() as parent:
            try:
                info = os.stat(record["image_basename"], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_dev != os.fstat(parent).st_dev
                or disk_identity(info) != record.get("image_identity")
            ):
                raise DiskRecoveryError("TASK_DISK_IMAGE_CHANGED")
            return info

    def _tree(self, fd, device, *, erase=False, depth=0, budget=None):
        budget = [0] if budget is None else budget
        if depth > 16:
            raise DiskRecoveryError("TASK_DISK_RETIREMENT_LIMIT")
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                budget[0] += 1
                if budget[0] > MAX_ENTRIES:
                    raise DiskRecoveryError("TASK_DISK_RETIREMENT_LIMIT")
                names.append(entry.name)
        for name in names:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid != os.geteuid() or info.st_dev != device:
                raise DiskRecoveryError("TASK_DISK_RETIREMENT_SCOPE")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, Scanner._directory_flags(), dir_fd=fd)
                try:
                    if disk_identity(os.fstat(child)) != disk_identity(info):
                        raise DiskRecoveryError("TASK_DISK_IMAGE_CHANGED")
                    self._tree(child, device, erase=erase, depth=depth + 1, budget=budget)
                    if disk_identity(
                        os.stat(name, dir_fd=fd, follow_symlinks=False)
                    ) != disk_identity(info):
                        raise DiskRecoveryError("TASK_DISK_IMAGE_CHANGED")
                    if erase:
                        os.rmdir(name, dir_fd=fd)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if erase:
                    if _version(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _version(info):
                        raise DiskRecoveryError("TASK_DISK_IMAGE_CHANGED")
                    os.unlink(name, dir_fd=fd)
            else:
                raise DiskRecoveryError("TASK_DISK_RETIREMENT_SCOPE")

    def _remove_image(self, record):
        self._current(record)
        self._eligible(record)
        if self.native.attachments(self.state.root / record["image_basename"]):
            raise DiskRecoveryError("TASK_DISK_STILL_ATTACHED")
        with self.state.root_fd() as parent:
            fd = os.open(record["image_basename"], Scanner._directory_flags(), dir_fd=parent)
            try:
                info = os.fstat(fd)
                if disk_identity(info) != record["image_identity"]:
                    raise DiskRecoveryError("TASK_DISK_IMAGE_CHANGED")
                self._tree(fd, info.st_dev)
                self._tree(fd, info.st_dev, erase=True)
                if (
                    disk_identity(
                        os.stat(record["image_basename"], dir_fd=parent, follow_symlinks=False)
                    )
                    != record["image_identity"]
                ):
                    raise DiskRecoveryError("TASK_DISK_IMAGE_CHANGED")
                os.rmdir(record["image_basename"], dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(fd)

    def _check_mount(self, record):
        if record.get("mount") is None:
            return
        mount = Path(record["mount"])
        try:
            info = mount.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode) or disk_identity(info) != record.get("mount_underlying"):
            raise DiskRecoveryError("TASK_DISK_MOUNT_CHANGED")
        access = SourceAccess(mount)
        with access.root_fd() as fd:
            if disk_identity(os.fstat(fd)) != record["mount_underlying"]:
                raise DiskRecoveryError("TASK_DISK_MOUNT_CHANGED")
            with os.scandir(fd) as entries:
                if next(entries, None) is not None:
                    raise DiskRecoveryError("TASK_DISK_MOUNT_NOT_EMPTY")

    def _remove_mount(self, record):
        self._check_mount(record)
        if record.get("mount") is None:
            return
        mount = Path(record["mount"])
        try:
            access = SourceAccess(mount)
        except SourceError:
            if not mount.exists() and not mount.is_symlink():
                return
            raise
        parent_access = SourceAccess(mount.parent)
        with parent_access.root_fd() as parent:
            with access.root_fd() as fd:
                if disk_identity(os.fstat(fd)) != record["mount_underlying"]:
                    raise DiskRecoveryError("TASK_DISK_MOUNT_CHANGED")
                with os.scandir(fd) as entries:
                    if next(entries, None) is not None:
                        raise DiskRecoveryError("TASK_DISK_MOUNT_NOT_EMPTY")
            if (
                disk_identity(os.stat(mount.name, dir_fd=parent, follow_symlinks=False))
                != record["mount_underlying"]
            ):
                raise DiskRecoveryError("TASK_DISK_MOUNT_CHANGED")
            os.rmdir(mount.name, dir_fd=parent)

    def _after_image_removed(self, record):
        """Crash-test seam; the retiring intent already covers all owned paths."""

    def _recover_one(self, record):
        info = self._image_info(record)
        if record["state"] == "retired":
            if info is not None or self.native.attachments(
                self.state.root / record["image_basename"]
            ):
                raise DiskRecoveryError("TASK_DISK_RETIRED_IMAGE_PRESENT")
            try:
                if self._expire_retired_record(record):
                    return "expired_record"
            except (SourceError, OSError, ValueError, TypeError, KeyError):
                # A retired image is already absent and detached. Inability to
                # expire its metadata (for example a kernel boot change) is
                # maintenance debt, not an unknown live task disk.
                return "retained_expired_record"
            return "already_retired"
        record = self._eligible(record)
        image = self.state.root / record["image_basename"]
        attached = self.native.attachments(image)
        if attached:
            if info is None or any(
                record.get(key) is None
                for key in (
                    "device",
                    "container",
                    "volume",
                    "container_uuid",
                    "volume_uuid",
                    "mounted_identity",
                )
            ):
                raise DiskRecoveryError("TASK_DISK_ATTACHMENT_UNVERIFIED")
            self._current(record)
            self._eligible(record)
            self.native.verify_attachment(record, attached[0])
            self.native.detach(record, attached[0], image)
            self._image_info(record)
            record = self._persist(record, "detached")
        if info is None and record["state"] not in {"creating", "retiring"}:
            raise DiskRecoveryError("TASK_DISK_IMAGE_MISSING")
        self._check_mount(record)
        record = self._persist(record, "retiring")
        if info is not None:
            self._remove_image(record)
        self._after_image_removed(record)
        self._remove_mount(record)
        self._persist(record, "retired")
        return "reclaimed"

    def _expire_retired_record(self, record):
        """Expire metadata only; never recover or eject an active task here."""
        if (
            record["state"] != "retired"
            or record["cleanup_verified"] is not True
            or self.clock() - record["updated_at"] < 86400
            or self.job_for(record["job_id"]) is not None
        ):
            return False
        source = self.source_for(record["project_id"])
        source.ensure_available()
        if source.source_id != record["source_id"]:
            raise DiskRecoveryError("TASK_DISK_SOURCE_CHANGED")
        if self._image_info(record) is not None or self.native.attachments(
            self.state.root / record["image_basename"]
        ):
            raise DiskRecoveryError("TASK_DISK_RETIRED_IMAGE_PRESENT")
        if record.get("mount") and (
            Path(record["mount"]).exists() or Path(record["mount"]).is_symlink()
        ):
            raise DiskRecoveryError("TASK_DISK_MOUNT_CHANGED")
        if (
            self.scope_expire is not None
            and record.get("execution_started") is not False
            and record.get("scope_retired") is not True
        ):
            try:
                proof = self.scope_expire(record)
            except (SourceError, OSError, ValueError):
                raise DiskRecoveryError("TASK_DISK_SCOPE_EXPIRY_UNVERIFIED") from None
            if (
                not isinstance(proof, dict)
                or set(proof)
                != {"job_id", "resource_id", "cleanup_verified", "launchd_retired", "expired"}
                or proof["job_id"] != record["job_id"]
                or proof["cleanup_verified"] is not True
                or proof["launchd_retired"] is not True
                or proof["expired"] is not True
                or type(proof["resource_id"]) is not int
                or not 1 < proof["resource_id"] < 2**64
                or record.get("recovery_resource_id", proof["resource_id"]) != proof["resource_id"]
            ):
                raise DiskRecoveryError("TASK_DISK_SCOPE_EXPIRY_UNVERIFIED")
            self._current(record)
            record = {**record, "scope_retired": True}
            write_state(self.state, record["job_id"] + ".disk.json", record)
        name = record["job_id"] + ".disk.json"
        current, version = _read_state(self.state, name)
        if current != record or self.job_for(record["job_id"]) is not None:
            raise DiskRecoveryError("TASK_DISK_RECORD_CHANGED")
        self._validate(current, record["job_id"])
        with self.state.root_fd() as parent:
            if _version(os.stat(name, dir_fd=parent, follow_symlinks=False)) != version:
                raise DiskRecoveryError("TASK_DISK_RECORD_CHANGED")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        return True

    def expire_retired_records(self, *, job_ids=None, limit=100):
        """A bounded metadata-only sweep while normal jobs may be running."""
        if type(limit) is not int or not 1 <= limit <= MAX_RECORDS:
            raise DiskRecoveryError("TASK_DISK_RECORD_LIMIT")
        if job_ids is not None and (
            not isinstance(job_ids, (list, tuple, set, frozenset))
            or len(job_ids) > MAX_RECORDS
            or any(not isinstance(j, str) or JOB.fullmatch(j) is None for j in job_ids)
        ):
            raise DiskRecoveryError("TASK_DISK_RECORD_INVALID")
        with self.lock:
            if job_ids is None:
                with self.state.root_fd() as parent:
                    names = []
                    with os.scandir(parent) as entries:
                        for entry in entries:
                            if len(names) >= MAX_RECORDS * 3:
                                return {
                                    "expired_records": [],
                                    "blocked": ["TASK_DISK_RECORD_LIMIT"],
                                }
                            names.append(entry.name)
                candidates = sorted(
                    name.removesuffix(".disk.json")
                    for name in names
                    if name.endswith(".disk.json")
                    and JOB.fullmatch(name.removesuffix(".disk.json")) is not None
                )
            else:
                candidates = sorted(set(job_ids))
            expired, blocked = [], []
            attempts = 0
            for job_id in candidates:
                try:
                    record = _read_state(self.state, job_id + ".disk.json")[0]
                    self._validate(record, job_id)
                    if (
                        record["state"] != "retired"
                        or self.clock() - record["updated_at"] < 86400
                        or self.job_for(job_id) is not None
                    ):
                        continue
                    attempts += 1
                    if self._expire_retired_record(record):
                        expired.append(job_id)
                except (SourceError, OSError, ValueError, TypeError, KeyError):
                    blocked.append(job_id)
                    attempts += 1
                if attempts >= limit:
                    break
            return {"expired_records": expired, "blocked": blocked}

    def recover(self):
        with self.lock:
            with self.state.root_fd() as parent:
                names = []
                with os.scandir(parent) as entries:
                    for entry in entries:
                        if len(names) >= MAX_RECORDS * 3:
                            return {
                                "state": "blocked",
                                "reclaimed": [],
                                "blocked": [{"reason": "TASK_DISK_RECORD_LIMIT"}],
                                "unknown": 1,
                            }
                        names.append(entry.name)
            records, blocked, reclaimed, expired, recognized = {}, [], [], [], set()
            retained_expired = []
            cleanup_proofs = {}
            for name in sorted(names):
                if (
                    not name.endswith(".disk.json")
                    or JOB.fullmatch(name.removesuffix(".disk.json")) is None
                ):
                    continue
                job_id = name.removesuffix(".disk.json")
                recognized.add(name)
                try:
                    record = _read_state(self.state, name)[0]
                    self._validate(record, job_id)
                    records[job_id] = record
                    recognized.add(record["image_basename"])
                    if record.get("mount") == str(self.state.root / (job_id + "-mount")):
                        recognized.add(job_id + "-mount")
                except (SourceError, OSError, ValueError, TypeError):
                    blocked.append({"job_id": job_id, "reason": "TASK_DISK_RECORD_UNVERIFIED"})
            # Retired receipts have their own 24-hour lifetime. Prune only
            # expired, detached, unreferenced records before the capacity gate.
            for job_id, record in tuple(records.items()):
                if record["state"] == "retired":
                    try:
                        if self._expire_retired_record(record):
                            expired.append(job_id)
                            records.pop(job_id)
                    except (SourceError, OSError, ValueError, TypeError, KeyError):
                        pass
            if len(records) > MAX_RECORDS:
                blocked.append({"reason": "TASK_DISK_RECORD_LIMIT"})
            else:
                for job_id, record in records.items():
                    try:
                        outcome = self._recover_one(record)
                        if outcome == "reclaimed":
                            reclaimed.append(job_id)
                        elif outcome == "expired_record":
                            expired.append(job_id)
                        elif outcome == "retained_expired_record":
                            retained_expired.append(job_id)
                        if outcome in {"reclaimed", "already_retired"}:
                            current = _read_state(self.state, job_id + ".disk.json")[0]
                            self._validate(current, job_id)
                            if current.get("cleanup_origin") == "verified_retired_resource_scope":
                                cleanup_proofs[job_id] = {
                                    "job_id": job_id,
                                    "resource_id": current["recovery_resource_id"],
                                    "cleanup_verified": True,
                                    "launchd_retired": True,
                                }
                    except DiskRecoveryError as error:
                        blocked.append({"job_id": job_id, "reason": str(error)})
                    except (SourceError, OSError, ValueError, TypeError, KeyError):
                        blocked.append(
                            {"job_id": job_id, "reason": "TASK_DISK_RECOVERY_UNVERIFIED"}
                        )
            unknown = len(set(names) - recognized)
            return {
                "state": "blocked" if blocked or unknown else "ready",
                "reclaimed": reclaimed,
                "expired_records": expired,
                "retained_expired_records": retained_expired,
                "blocked": blocked,
                "unknown": unknown,
                "cleanup_proofs": cleanup_proofs,
            }

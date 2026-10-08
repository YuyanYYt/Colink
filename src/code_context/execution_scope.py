"""macOS execution scopes owned by an ephemeral launchd resource coalition.

The kernel preserves a resource coalition across fork, setsid and exec. We pin
its never-reused ID and process birth IDs, then signal with audit tokens so PID
reuse cannot redirect a signal. This is a lifecycle boundary, not a VM or a
filesystem/network sandbox. The coordinator must provide the native sandbox.

Private libproc ABI is deliberately probed before starting project code. An
unsupported kernel, incomplete enumeration or denied read fails closed. There
is no PPID/process-group fallback. Application state and its control capability
must be outside every project read/write root.
"""

import ctypes
import errno
import hashlib
import json
import os
import plistlib
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MAX_PIDS = 16384
MAX_SCOPE_DIRECTORIES = 4096
MAX_MANIFEST_BYTES = 8192
# Reserve 8 KiB of serialized state per retained scope, <= 32 MiB overall.
# Manifests/identity plus in-flight replacements fit beside a <= 6 KiB plist.
MAX_SCOPE_PLIST_BYTES = 6 * 1024
MAX_SCOPE_JSON_BYTES = 500
TESTED_DARWIN_MAJORS = frozenset({27})


class ScopeError(ValueError):
    """Errors deliberately exclude executable arguments and capabilities."""


def _kernel_boot_identity():
    # kern.boottime is a native timeval on this tested 64-bit Darwin ABI. A
    # resource coalition ID must never be interpreted across kernel boots.
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        function = library.sysctlbyname
        function.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        function.restype = ctypes.c_int

        class Timeval(ctypes.Structure):
            _fields_ = [("seconds", ctypes.c_int64), ("microseconds", ctypes.c_int32)]

        timeval = Timeval()
        length = ctypes.c_size_t(ctypes.sizeof(timeval))
        if (
            sys.platform != "darwin"
            or function(b"kern.boottime", ctypes.byref(timeval), ctypes.byref(length), None, 0)
            or length.value != 16
            or timeval.seconds <= 0
            or not 0 <= timeval.microseconds < 1_000_000
        ):
            raise ValueError
        return f"{int(timeval.seconds)}:{int(timeval.microseconds)}"
    except (OSError, AttributeError, ValueError):
        raise ScopeError("EXECUTION_SCOPE_ABI_UNSUPPORTED") from None


def _scope_job_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or "\x00" in value:
        raise ScopeError("EXECUTION_SCOPE_JOB_INVALID")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ScopeError("EXECUTION_SCOPE_JOB_INVALID") from None
    return value


def _scope_json(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_SCOPE_JSON_BYTES:
        raise ScopeError("EXECUTION_SCOPE_METADATA_CAPACITY")
    return raw


class _Unique(ctypes.Structure):
    _fields_ = [
        ("uuid", ctypes.c_uint8 * 16),
        ("unique", ctypes.c_uint64),
        ("parent_unique", ctypes.c_uint64),
        ("version", ctypes.c_int32),
        ("parent_version", ctypes.c_int32),
        ("reserved", ctypes.c_uint64 * 2),
    ]


class _ShortInfo(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("flags", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class _TaskInfo(ctypes.Structure):
    _fields_ = [
        ("virtual_bytes", ctypes.c_uint64),
        ("resident_bytes", ctypes.c_uint64),
        ("total_user", ctypes.c_uint64),
        ("total_system", ctypes.c_uint64),
        ("threads_user", ctypes.c_uint64),
        ("threads_system", ctypes.c_uint64),
        ("counters", ctypes.c_int32 * 12),
    ]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    unique: int
    version: int

    def as_dict(self):
        return {"pid": self.pid, "unique": self.unique, "version": self.version}

    def same_birth(self, other):
        # XNU changes pidversion on exec, but keeps the unique process ID.
        return isinstance(other, ProcessIdentity) and (self.pid, self.unique) == (
            other.pid,
            other.unique,
        )


@dataclass(frozen=True)
class CoalitionMember:
    identity: ProcessIdentity
    resource_id: int
    status: int


class MacKernel:
    """Strict, bounded access to currently tested libproc process identities."""

    def __init__(self):
        if sys.platform != "darwin":
            raise ScopeError("EXECUTION_SCOPE_PLATFORM_UNSUPPORTED")
        try:
            if int(os.uname().release.split(".")[0]) not in TESTED_DARWIN_MAJORS:
                raise ValueError
            self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.lib.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.lib.proc_pidinfo.restype = ctypes.c_int
            self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.lib.proc_listallpids.restype = ctypes.c_int
            self.lib.proc_signal_with_audittoken.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.lib.proc_signal_with_audittoken.restype = ctypes.c_int
            self.clock_lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            self.clock_lib.mach_timebase_info.argtypes = [ctypes.c_void_p]
            self.clock_lib.mach_timebase_info.restype = ctypes.c_int
            timebase = _Timebase()
            if (
                ctypes.sizeof(_Unique) != 56
                or ctypes.sizeof(_ShortInfo) != 64
                or ctypes.sizeof(_TaskInfo) != 96
                or ctypes.sizeof(_Timebase) != 8
                or self.clock_lib.mach_timebase_info(ctypes.byref(timebase)) != 0
                or not timebase.numer
                or not timebase.denom
            ):
                raise ValueError
            self.timebase_numer, self.timebase_denom = int(timebase.numer), int(timebase.denom)
            own = self.identity(os.getpid())
            coalition = self.resource_id(os.getpid())
            short = self._info(os.getpid(), 13, _ShortInfo())
            task = self._info(os.getpid(), 4, _TaskInfo())
            if (
                own is None
                or coalition is None
                or coalition <= 1
                or short is None
                or short.pid != os.getpid()
                or short.uid != os.getuid()
                or task is None
                or task.resident_bytes == 0
                or self._audit_signal(own, signal.SIGCONT) != 0
                or self._audit_signal(
                    ProcessIdentity(own.pid, own.unique, (own.version + 1) & 0xFFFFFFFF),
                    signal.SIGCONT,
                )
                != errno.ESRCH
            ):
                raise ValueError
            self.pids()
        except (OSError, AttributeError, ValueError, ScopeError):
            raise ScopeError("EXECUTION_SCOPE_ABI_UNSUPPORTED") from None

    def _info(self, pid, flavor, data):
        ctypes.set_errno(0)
        result = self.lib.proc_pidinfo(pid, flavor, 0, ctypes.byref(data), ctypes.sizeof(data))
        if result == ctypes.sizeof(data):
            return data
        if result == 0 and ctypes.get_errno() == errno.ESRCH:
            return None
        raise ScopeError("EXECUTION_SCOPE_ENUMERATION_FAILED")

    def identity(self, pid):
        data = self._info(pid, 17, _Unique())
        if data is None:
            return None
        if not data.unique or data.version <= 0:
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_FAILED")
        return ProcessIdentity(pid, int(data.unique), int(data.version))

    def resource_id(self, pid):
        data = self._info(pid, 20, (ctypes.c_uint64 * 5)())
        return int(data[0]) if data is not None else None

    def pids(self):
        data = (ctypes.c_int * MAX_PIDS)()
        ctypes.set_errno(0)
        count = self.lib.proc_listallpids(data, ctypes.sizeof(data))
        if not 0 < count < len(data):
            raise ScopeError("EXECUTION_SCOPE_ENUMERATION_FAILED")
        return {int(pid) for pid in data[:count] if pid > 0}

    def resource_ids(self):
        ids = set()
        for pid in self.pids():
            resource = self.resource_id(pid)
            if resource is not None:
                ids.add(resource)
        return ids

    def members(self, resource_id, exclude=()):
        if type(resource_id) is not int or resource_id <= 1:
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_FAILED")
        excluded = frozenset(exclude)
        result = {}
        for pid in self.pids():
            if self.resource_id(pid) != resource_id:
                continue
            before = self.identity(pid)
            if before is None:
                continue
            for _ in range(3):
                short = self._info(pid, 13, _ShortInfo())
                coalition = self.resource_id(pid)
                after = self.identity(pid)
                if short is None or after is None:
                    break
                if not before.same_birth(after) or coalition != resource_id:
                    raise ScopeError("EXECUTION_SCOPE_IDENTITY_CHANGED")
                if before.version != after.version:
                    # Recheck UID and status after an exec; the old short info
                    # may describe the previous executable image.
                    before = after
                    continue
                if short.pid != pid or short.uid != os.getuid():
                    raise ScopeError("EXECUTION_SCOPE_IDENTITY_FAILED")
                if not any(after.same_birth(item) for item in excluded):
                    result[pid] = CoalitionMember(after, resource_id, int(short.status))
                break
            else:
                raise ScopeError("EXECUTION_SCOPE_IDENTITY_CHANGED")
        return result

    def metrics(self, members):
        resident = 0
        live = 0
        cpu = {}
        for member in members.values():
            if member.status == 5:  # SZOMB: no longer an executing task.
                continue
            task = self._info(member.identity.pid, 4, _TaskInfo())
            if task is None:
                continue
            current_identity = self.identity(member.identity.pid)
            if current_identity is None:
                continue
            current_resource = self.resource_id(member.identity.pid)
            if current_resource is None:
                continue
            if (
                not member.identity.same_birth(current_identity)
                or current_resource != member.resource_id
            ):
                raise ScopeError("EXECUTION_SCOPE_IDENTITY_CHANGED")
            # XNU task CPU totals survive exec. Use the immutable process ID
            # so an image change never charges the same work a second time.
            cpu[(member.identity.pid, member.identity.unique)] = (
                int(task.total_user + task.total_system)
                * self.timebase_numer
                // self.timebase_denom
            )
            if current_identity.version != member.identity.version:
                # The CPU counter is cumulative across exec; resident bytes
                # and status may belong to the old executable image.
                continue
            resident += int(task.resident_bytes)
            live += 1
            # XNU recount_times_mach was converted with the kernel timebase.
        # Shared resident pages are counted conservatively per process. CPU
        # sees sampled live tasks; descendants exiting between scans can evade
        # accounting. These are monitoring thresholds, not hard OS quotas.
        return {"rss_bytes": resident, "processes": live, "cpu_nanoseconds": cpu}

    def _audit_signal(self, identity, requested_signal):
        token = (ctypes.c_uint32 * 8)()
        token[5], token[7] = identity.pid, identity.version
        return int(self.lib.proc_signal_with_audittoken(token, requested_signal))

    def signal_member(self, member, requested_signal):
        if (
            not isinstance(member, CoalitionMember)
            or not isinstance(requested_signal, int)
            or type(requested_signal) is bool
            or requested_signal not in signal.valid_signals()
        ):
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_FAILED")
        before = self.identity(member.identity.pid)
        if before is None:
            return False
        short = self._info(before.pid, 13, _ShortInfo())
        resource = self.resource_id(before.pid)
        after = self.identity(before.pid)
        if short is None or after is None:
            return False
        if (
            not member.identity.same_birth(before)
            or not before.same_birth(after)
            or resource != member.resource_id
        ):
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_CHANGED")
        if before.version != after.version:
            return False  # Recheck UID and image in the next coalition scan.
        if short.pid != before.pid or short.uid != os.getuid():
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_FAILED")
        # The kernel checks the fresh pidversion atomically before signalling.
        # ESRCH means this image exited or execed; a later scan can retry.
        result = self._audit_signal(after, requested_signal)
        if result not in (0, errno.ESRCH):
            raise ScopeError("EXECUTION_SCOPE_SIGNAL_FAILED")
        return result == 0

    def signal_identity(self, identity, requested_signal):
        """Signal a caller-verified owned process using its kernel birth token.

        Ownership/policy remains the trusted caller's responsibility. This is
        the atomic alternative to numeric kill after a separate PID check.
        Returns False if that generation already exited, never adopts a reuse.
        """
        if (
            not isinstance(identity, ProcessIdentity)
            or not isinstance(requested_signal, int)
            or type(requested_signal) is bool
            or requested_signal not in signal.valid_signals()
        ):
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_FAILED")
        current = self.identity(identity.pid)
        if current is None:
            return False
        if current != identity:
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_CHANGED")
        result = self._audit_signal(identity, requested_signal)
        if result not in (0, errno.ESRCH):
            raise ScopeError("EXECUTION_SCOPE_SIGNAL_FAILED")
        return result == 0


class CoalitionScope:
    """A single trusted helper's fixed resource coalition; never adopt a PID."""

    def __init__(self, kernel, leader, resource_id):
        if (
            kernel.identity(leader.pid) != leader
            or kernel.resource_id(leader.pid) != resource_id
            or resource_id <= 1
        ):
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_FAILED")
        own = kernel.resource_id(os.getpid())
        if own == resource_id and leader.pid != os.getpid():
            raise ScopeError("EXECUTION_SCOPE_NOT_DEDICATED")
        self.kernel, self.leader, self.resource_id = kernel, leader, resource_id
        self.failed = False
        self.reap = lambda: None

    def scan(self, exclude_leader=True):
        self.reap()
        leader = self.kernel.identity(self.leader.pid)
        if leader is not None and (
            leader != self.leader or self.kernel.resource_id(leader.pid) != self.resource_id
        ):
            raise ScopeError("EXECUTION_SCOPE_IDENTITY_CHANGED")
        return self.kernel.members(
            self.resource_id, exclude=(self.leader,) if exclude_leader else ()
        )

    def send(self, requested_signal, exclude_leader=True):
        for member in self.scan(exclude_leader).values():
            if member.status != 5:
                self.kernel.signal_member(member, requested_signal)

    def stop(self, grace=(3, 2, 1), exclude_leader=True):
        """Escalate, then freeze/revalidate/kill until two full empty scans.

        SIGSTOP prevents observed members from continuing to fork while the
        kernel list is checked again. New members trigger another sweep. There
        is a bounded deadline; an incomplete sweep is explicitly unsuccessful.
        """
        try:
            for requested_signal, seconds in zip(
                (signal.SIGINT, signal.SIGTERM), grace[:2], strict=True
            ):
                self.send(requested_signal, exclude_leader)
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if not self.scan(exclude_leader):
                        time.sleep(0.01)
                        if not self.scan(exclude_leader):
                            return True
                    time.sleep(0.01)
            deadline = time.monotonic() + max(2, grace[2])
            empty = 0
            while time.monotonic() < deadline:
                members = self.scan(exclude_leader)
                if not members:
                    empty += 1
                    if empty >= 2:
                        return True
                    time.sleep(0.01)
                    continue
                empty = 0
                for member in members.values():
                    if member.status != 5:
                        self.kernel.signal_member(member, signal.SIGSTOP)
                time.sleep(0.005)
                frozen = self.scan(exclude_leader)
                if any(member.status not in (4, 5) for member in frozen.values()):
                    continue
                time.sleep(0.005)
                checked = self.scan(exclude_leader)
                if {item.identity for item in frozen.values()} != {
                    item.identity for item in checked.values()
                } or any(member.status not in (4, 5) for member in checked.values()):
                    continue
                for member in checked.values():
                    if member.status != 5:
                        self.kernel.signal_member(member, signal.SIGKILL)
                time.sleep(0.01)
            self.failed = True
            return False
        except ScopeError:
            self.failed = True
            return False


def _launchctl(*args, timeout=5):
    return subprocess.run(
        ["/bin/launchctl", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        timeout=timeout,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
    )


def _manifest_valid(payload, *, name=None):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"uid", "label", "port", "token", "job_id", "kernel_boot"}
        or type(payload["uid"]) is not int
        or payload["uid"] != os.getuid()
        or not isinstance(payload["label"], str)
        or re.fullmatch(r"com\.colink\.execution\.[a-f0-9]{32}", payload["label"]) is None
        or name is not None
        and payload["label"] != "com.colink.execution." + name
        or not isinstance(payload["token"], str)
        or re.fullmatch(r"[a-f0-9]{64}", payload["token"]) is None
        or type(payload["port"]) is not int
        or not 1024 <= payload["port"] <= 65535
        or not isinstance(payload["kernel_boot"], str)
        or re.fullmatch(r"[0-9]{1,20}:[0-9]{1,6}", payload["kernel_boot"]) is None
    ):
        raise ScopeError("EXECUTION_SCOPE_CONTROL_UNSAFE")
    _scope_job_id(payload["job_id"])


def _scope_file_version(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_scope_file(parent, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        version = _scope_file_version(before)
        if (
            len(raw) > limit
            or len(raw) != before.st_size
            or version != _scope_file_version(after)
            or version != _scope_file_version(named)
        ):
            raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
        return raw, version
    finally:
        os.close(fd)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _scope_payload(raw):
    payload = json.loads(raw, object_pairs_hook=_unique_pairs)
    if not isinstance(payload, dict):
        raise ValueError
    return payload


def _supervisor_plist(directory, helper_path):
    return {
        "Label": "com.colink.execution." + directory.name,
        "ProgramArguments": [
            sys.executable,
            "-I",
            "-B",
            "-S",
            "-X",
            "utf8",
            str(helper_path),
            "--supervise-launchd",
            str(directory / "control.json"),
        ],
        "WorkingDirectory": str(directory),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Background",
        "ExitTimeOut": 1,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def _read_manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > MAX_SCOPE_JSON_BYTES
        ):
            raise ScopeError("EXECUTION_SCOPE_CONTROL_UNSAFE")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_SCOPE_JSON_BYTES + 1)
            payload = _scope_payload(raw)
        _manifest_valid(payload, name=Path(path).parent.name)
        if payload["kernel_boot"] != _kernel_boot_identity():
            raise ScopeError("EXECUTION_SCOPE_CONTROL_UNSAFE")
        return payload
    except (OSError, ValueError, TypeError, AttributeError):
        raise ScopeError("EXECUTION_SCOPE_CONTROL_UNSAFE") from None
    finally:
        os.close(fd)


def _retirement_budget(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_TIME_BUDGET")
    return remaining


def _private_scope_directory(info):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")


@contextmanager
def _retirement_root_fd(source):
    from code_context.source_access import SourceError

    try:
        with source.root_fd() as parent:
            yield parent
    except SourceError as error:
        # SourceAccess intentionally sanitizes ValueError from the body. Keep
        # our already-sanitized budget/change code without exposing its input.
        if isinstance(error.__context__, ScopeError):
            raise error.__context__ from None
        raise


def _existing_scope_root(scope_root):
    from code_context.source_access import SourceAccess

    path = Path(scope_root)
    if not path.is_absolute() or ".." in path.parts:
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
    source = SourceAccess(path)  # Does not create a missing path or follow symlinks.
    with _retirement_root_fd(source) as parent:
        _private_scope_directory(os.fstat(parent))
    return source


@dataclass(frozen=True, repr=False)
class RetiredJobScopeIndex:
    """One recovery batch's bounded private metadata, never a durable cache."""

    source: object
    root_version: tuple
    candidates: dict


def index_retired_job_scopes(scope_root, *, scan_seconds=0.5):
    """Index each private control record once; an expired scan proves nothing."""
    from code_context.source_access import SourceError

    if type(scan_seconds) not in (int, float) or not 0 < scan_seconds <= 1:
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_TIME_BUDGET")
    deadline = time.monotonic() + scan_seconds
    try:
        source = _existing_scope_root(scope_root)
        candidates = {}
        with _retirement_root_fd(source) as rootfd:
            root_version = _scope_file_version(os.fstat(rootfd))
            names = os.listdir(rootfd)
            if len(names) > MAX_SCOPE_DIRECTORIES:
                raise ScopeError("EXECUTION_SCOPE_METADATA_CAPACITY")
            for name in names:
                _retirement_budget(deadline)
                if re.fullmatch(r"[a-f0-9]{32}", name) is None:
                    raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=rootfd,
                )
                try:
                    before = os.fstat(fd)
                    _private_scope_directory(before)
                    raw, version = _read_scope_file(fd, "control.json", MAX_SCOPE_JSON_BYTES)
                    payload = _scope_payload(raw)
                    # Legacy scopes have no attributable job and are never
                    # eligible. They are not adopted or rewritten on restart.
                    if "job_id" not in payload:
                        continue
                    _manifest_valid(payload, name=name)
                    directory_version = _scope_file_version(before)
                    if directory_version != _scope_file_version(
                        os.fstat(fd)
                    ) or directory_version != _scope_file_version(
                        os.stat(name, dir_fd=rootfd, follow_symlinks=False)
                    ):
                        raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                    candidates.setdefault(payload["job_id"], []).append(
                        (name, directory_version, raw, version)
                    )
                finally:
                    os.close(fd)
            _retirement_budget(deadline)
            if root_version != _scope_file_version(os.fstat(rootfd)):
                raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
        return RetiredJobScopeIndex(source, root_version, candidates)
    except ScopeError:
        raise
    except (SourceError, OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED") from None


def _identity_record_valid(payload, control, control_raw, plist_raw):
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"job_id", "kernel_boot", "control_sha256", "plist_sha256", "identity", "resource_id"}
        or payload["job_id"] != control["job_id"]
        or payload["kernel_boot"] != control["kernel_boot"]
        or payload["control_sha256"] != hashlib.sha256(control_raw).hexdigest()
        or payload["plist_sha256"] != hashlib.sha256(plist_raw).hexdigest()
        or type(payload["resource_id"]) is not int
        or not 1 < payload["resource_id"] < 2**64
        or not isinstance(payload["identity"], dict)
        or set(payload["identity"]) != {"pid", "unique", "version"}
        or any(type(value) is not int for value in payload["identity"].values())
        or not 0 < payload["identity"]["pid"] < 2**31
        or not 0 < payload["identity"]["unique"] < 2**64
        or not 0 < payload["identity"]["version"] < 2**32
    ):
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
    return ProcessIdentity(**payload["identity"])


def _retire_inactive_launchd(target, plist_path, deadline):
    result = _launchctl("print", target, timeout=min(0.5, _retirement_budget(deadline)))
    if len(result.stdout) > 65536:
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
    if result.returncode == 113:
        return
    if result.returncode:
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
    lines = [line.strip() for line in result.stdout.decode("utf-8").splitlines()]
    if (
        not lines
        or lines[0] != target + " = {"
        or [line[7:] for line in lines if line.startswith("path = ")] != [str(plist_path)]
        or [line[8:] for line in lines if line.startswith("state = ")] != ["not running"]
        or any(line.startswith("pid = ") for line in lines)
    ):
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
    result = _launchctl("bootout", target, timeout=min(0.5, _retirement_budget(deadline)))
    if result.returncode not in (0, 113):
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")


def verify_retired_job_scope(scope_root, job_id, *, index=None):
    """Prove one interrupted job is empty and unloaded, without signalling it.

    Only the caller's exact, authenticated, inactive launchd label may be
    booted out. Missing/legacy/ambiguous/changed metadata or a live member
    rejects recovery. Kernel boot changes reject old IDs. No process adoption,
    command restart or generated-file cleanup occurs here.
    """
    from code_context import execution_process
    from code_context.source_access import SourceAccess, SourceError

    deadline = time.monotonic() + 2
    _scope_job_id(job_id)
    try:
        if index is None:
            index = index_retired_job_scopes(scope_root)
        if not isinstance(index, RetiredJobScopeIndex) or Path(scope_root) != index.source.root:
            raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
        matches = index.candidates.get(job_id, ())
        if len(matches) != 1:
            raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
        name, directory_version, indexed_raw, indexed_version = matches[0]
        with _retirement_root_fd(index.source) as rootfd:
            _private_scope_directory(os.fstat(rootfd))
            if index.root_version != _scope_file_version(os.fstat(rootfd)):
                raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
            if directory_version != _scope_file_version(
                os.stat(name, dir_fd=rootfd, follow_symlinks=False)
            ):
                raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
        directory = SourceAccess(index.source.root / name)
        with _retirement_root_fd(directory) as parent:
            _private_scope_directory(os.fstat(parent))
            if directory_version != _scope_file_version(os.fstat(parent)):
                raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
            control_raw, control_version = _read_scope_file(
                parent, "control.json", MAX_SCOPE_JSON_BYTES
            )
            identity_raw, identity_version = _read_scope_file(
                parent, "identity.json", MAX_SCOPE_JSON_BYTES
            )
            plist_raw, plist_version = _read_scope_file(parent, "job.plist", MAX_SCOPE_PLIST_BYTES)
        if control_raw != indexed_raw or control_version != indexed_version:
            raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
        control, record = _scope_payload(control_raw), _scope_payload(identity_raw)
        _manifest_valid(control, name=name)
        identity = _identity_record_valid(record, control, control_raw, plist_raw)
        if (
            control["job_id"] != job_id
            or control["kernel_boot"] != _kernel_boot_identity()
            or plistlib.loads(plist_raw)
            != _supervisor_plist(directory.root, Path(execution_process.__file__).resolve())
        ):
            raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
        _retirement_budget(deadline)
        kernel = MacKernel()
        if kernel.identity(identity.pid) is not None or kernel.members(record["resource_id"]):
            raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
        target = f"gui/{os.getuid()}/{control['label']}"
        _retire_inactive_launchd(target, directory.root / "job.plist", deadline)
        if (
            _launchctl("print", target, timeout=min(0.5, _retirement_budget(deadline))).returncode
            != 113
        ):
            raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
        for number in range(2):
            _retirement_budget(deadline)
            if kernel.identity(identity.pid) is not None or kernel.members(record["resource_id"]):
                raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
            if not number:
                time.sleep(0.025)
        if _kernel_boot_identity() != control["kernel_boot"]:
            raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
        with _retirement_root_fd(directory) as parent:
            if directory_version != _scope_file_version(os.fstat(parent)):
                raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
            for file_name, expected, limit in (
                ("control.json", (control_raw, control_version), MAX_SCOPE_JSON_BYTES),
                ("identity.json", (identity_raw, identity_version), MAX_SCOPE_JSON_BYTES),
                ("job.plist", (plist_raw, plist_version), MAX_SCOPE_PLIST_BYTES),
            ):
                if _read_scope_file(parent, file_name, limit) != expected:
                    raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
        with _retirement_root_fd(index.source) as rootfd:
            if index.root_version != _scope_file_version(os.fstat(rootfd)):
                raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
        if (
            _launchctl("print", target, timeout=min(0.5, _retirement_budget(deadline))).returncode
            != 113
        ):
            raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
        _retirement_budget(deadline)
        return {
            "job_id": job_id,
            "resource_id": record["resource_id"],
            "cleanup_verified": True,
            "launchd_retired": True,
        }
    except ScopeError:
        raise
    except (
        SourceError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED") from None


def _scope_rename_excl(parent, old_name, new_name):
    if sys.platform != "darwin":
        raise ScopeError("EXECUTION_SCOPE_PLATFORM_UNSUPPORTED")
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        function = library.renameatx_np
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        if function(parent, os.fsencode(old_name), parent, os.fsencode(new_name), 4):
            raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
    except (OSError, AttributeError):
        raise ScopeError("EXECUTION_SCOPE_ABI_UNSUPPORTED") from None


def expire_verified_job_scope(scope_root, job_id):
    """Remove one expired app-owned scope only after native retirement proof.

    The trusted coordinator must first establish: at least 24 hours expired,
    no execution ledger remains, and the task disk is retired or absent. This
    is an internal lifecycle API, never a project command or MCP permission.
    Unknown objects and legacy/unbound scopes are preserved. A failed partial
    removal retains the remainder for explicit recovery, without guessing.
    """
    import fcntl

    from code_context.source_access import SourceError

    deadline = time.monotonic() + 3
    job_id = _scope_job_id(job_id)
    try:
        index = index_retired_job_scopes(scope_root)
        proof = verify_retired_job_scope(scope_root, job_id, index=index)
        name, directory_version, indexed_control, indexed_version = index.candidates[job_id][0]
        with _retirement_root_fd(index.source) as rootfd:
            try:
                fcntl.flock(rootfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ScopeError("EXECUTION_SCOPE_STATE_BUSY") from None
            child = None
            quarantine = None
            try:
                _retirement_budget(deadline)
                if index.root_version != _scope_file_version(os.fstat(rootfd)):
                    raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=rootfd
                )
                before = os.fstat(child)
                _private_scope_directory(before)
                if directory_version != _scope_file_version(
                    before
                ) or directory_version != _scope_file_version(
                    os.stat(name, dir_fd=rootfd, follow_symlinks=False)
                ):
                    raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                allowed = {"control.json", "identity.json", "job.plist"}
                if set(os.listdir(child)) != allowed:
                    raise ScopeError("EXECUTION_SCOPE_EXPIRY_UNKNOWN_OBJECT")
                captured = {}
                for file_name, limit in (
                    ("control.json", MAX_SCOPE_JSON_BYTES),
                    ("identity.json", MAX_SCOPE_JSON_BYTES),
                    ("job.plist", MAX_SCOPE_PLIST_BYTES),
                ):
                    captured[file_name] = _read_scope_file(child, file_name, limit)
                if captured["control.json"] != (indexed_control, indexed_version):
                    raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                control = _scope_payload(captured["control.json"][0])
                record = _scope_payload(captured["identity.json"][0])
                _manifest_valid(control, name=name)
                identity = _identity_record_valid(
                    record, control, captured["control.json"][0], captured["job.plist"][0]
                )
                if (
                    record["resource_id"] != proof["resource_id"]
                    or control["kernel_boot"] != _kernel_boot_identity()
                ):
                    raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
                if index.root_version != _scope_file_version(os.fstat(rootfd)):
                    raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                # Detach the exact directory with an exclusive native rename
                # before deleting. A replacement is restored, never purged.
                quarantine = ".expired-" + name + "-" + secrets.token_hex(16)
                _scope_rename_excl(rootfd, name, quarantine)
                moved = os.stat(quarantine, dir_fd=rootfd, follow_symlinks=False)
                if (moved.st_dev, moved.st_ino, moved.st_mode, moved.st_uid) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_uid,
                ) or (os.fstat(child).st_dev, os.fstat(child).st_ino) != (
                    before.st_dev,
                    before.st_ino,
                ):
                    raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                if set(os.listdir(child)) != allowed:
                    raise ScopeError("EXECUTION_SCOPE_EXPIRY_UNKNOWN_OBJECT")
                for file_name, limit in (
                    ("control.json", MAX_SCOPE_JSON_BYTES),
                    ("identity.json", MAX_SCOPE_JSON_BYTES),
                    ("job.plist", MAX_SCOPE_PLIST_BYTES),
                ):
                    if _read_scope_file(child, file_name, limit) != captured[file_name]:
                        raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                _retirement_budget(deadline)
                kernel = MacKernel()
                target = f"gui/{os.getuid()}/{control['label']}"
                if (
                    kernel.identity(identity.pid) is not None
                    or kernel.members(record["resource_id"])
                    or control["kernel_boot"] != _kernel_boot_identity()
                    or _launchctl(
                        "print", target, timeout=min(0.5, _retirement_budget(deadline))
                    ).returncode
                    != 113
                ):
                    raise ScopeError("EXECUTION_SCOPE_RETIREMENT_UNVERIFIED")
                for file_name in ("job.plist", "identity.json", "control.json"):
                    if (
                        _read_scope_file(
                            child,
                            file_name,
                            MAX_SCOPE_PLIST_BYTES
                            if file_name == "job.plist"
                            else MAX_SCOPE_JSON_BYTES,
                        )
                        != captured[file_name]
                    ):
                        raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                    os.unlink(file_name, dir_fd=child)
                if os.listdir(child):
                    raise ScopeError("EXECUTION_SCOPE_EXPIRY_UNKNOWN_OBJECT")
                final = os.stat(quarantine, dir_fd=rootfd, follow_symlinks=False)
                if (final.st_dev, final.st_ino, final.st_mode, final.st_uid) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_uid,
                ):
                    raise ScopeError("EXECUTION_SCOPE_STATE_CHANGED")
                os.rmdir(quarantine, dir_fd=rootfd)
                quarantine = None
                os.fsync(rootfd)
                return {**proof, "expired": True}
            except BaseException:
                if quarantine is not None:
                    try:
                        _scope_rename_excl(rootfd, quarantine, name)
                    except (OSError, ScopeError):
                        pass  # Preserve, never overwrite an unexpected name.
                raise
            finally:
                if child is not None:
                    os.close(child)
                fcntl.flock(rootfd, fcntl.LOCK_UN)
    except ScopeError:
        raise
    except (
        SourceError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        raise ScopeError("EXECUTION_SCOPE_EXPIRY_UNVERIFIED") from None


class _ControlWriter:
    def __init__(self, connection):
        self.connection = connection
        self.stream = connection.makefile("wb")

    @property
    def closed(self):
        return self.stream.closed

    def write(self, data):
        return self.stream.write(data)

    def flush(self):
        self.stream.flush()

    def close(self):
        if not self.closed:
            try:
                self.stream.close()
            finally:
                try:
                    self.connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass


class LaunchdSupervisor:
    """Popen-like control/response pipes for one authenticated native scope."""

    @classmethod
    def spawn(cls, scope_root, helper_path, *, job_id=None):
        from code_context.local_control import private_directory, write_state

        kernel = MacKernel()
        kernel_boot = _kernel_boot_identity()
        if job_id is not None:
            _scope_job_id(job_id)
        before = kernel.resource_ids()
        state = private_directory(Path(scope_root))
        with state.root_fd() as rootfd:
            if len(os.listdir(rootfd)) >= MAX_SCOPE_DIRECTORIES:
                raise ScopeError("EXECUTION_SCOPE_METADATA_CAPACITY")
            name = secrets.token_hex(16)
            os.mkdir(name, 0o700, dir_fd=rootfd)
        job_id = "scope_" + name if job_id is None else job_id
        directory = private_directory(state.root / name)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.set_inheritable(False)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        label = "com.colink.execution." + name
        target = f"gui/{os.getuid()}/{label}"
        manifest = {
            "uid": os.getuid(),
            "label": label,
            "port": listener.getsockname()[1],
            "token": secrets.token_hex(32),
            "job_id": job_id,
            "kernel_boot": kernel_boot,
        }
        control_raw = _scope_json(manifest)
        write_state(directory, "control.json", manifest)
        helper_path = Path(helper_path).resolve(strict=True)
        plist = _supervisor_plist(directory.root, helper_path)
        serialized_plist = plistlib.dumps(plist)
        if len(serialized_plist) > MAX_SCOPE_PLIST_BYTES:
            raise ScopeError("EXECUTION_SCOPE_METADATA_CAPACITY")
        binding = {
            "job_id": job_id,
            "kernel_boot": kernel_boot,
            "control_sha256": hashlib.sha256(control_raw).hexdigest(),
            "plist_sha256": hashlib.sha256(serialized_plist).hexdigest(),
        }
        # Validate the largest identities before launchd receives any plist.
        _scope_json(
            {
                **binding,
                "identity": {"pid": 2**31 - 1, "unique": 2**64 - 1, "version": 2**32 - 1},
                "resource_id": 2**64 - 1,
            }
        )
        with directory.root_fd() as rootfd:
            fd = os.open(
                "job.plist",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=rootfd,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(serialized_plist)
                stream.flush()
                os.fsync(stream.fileno())
        connection = reader = scope = None
        loaded = False
        try:
            if _launchctl(
                "bootstrap", f"gui/{os.getuid()}", str(directory.root / "job.plist")
            ).returncode:
                raise ScopeError("EXECUTION_SCOPE_START_FAILED")
            loaded = True
            kick = _launchctl("kickstart", "-p", target)
            if kick.returncode or not kick.stdout.strip().isdigit():
                raise ScopeError("EXECUTION_SCOPE_START_FAILED")
            pid = int(kick.stdout.strip())
            connection, _ = listener.accept()
            connection.settimeout(5)
            connection.set_inheritable(False)
            reader = connection.makefile("rb")
            raw = reader.readline(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES or not raw.endswith(b"\n"):
                raise ScopeError("EXECUTION_SCOPE_AUTHENTICATION_FAILED")
            hello = json.loads(raw)
            # kickstart may return before launchd's child has changed uid and
            # exec'd the helper. Bind the final birth token after its handshake,
            # still before sending any project arguments or input capability.
            identity, resource = kernel.identity(pid), kernel.resource_id(pid)
            if identity is None or resource is None or resource in before:
                raise ScopeError("EXECUTION_SCOPE_NOT_DEDICATED")
            scope = CoalitionScope(kernel, identity, resource)
            if {item.identity for item in scope.scan(False).values()} != {identity}:
                raise ScopeError("EXECUTION_SCOPE_NOT_DEDICATED")
            if (
                not secrets.compare_digest(hello.get("token", ""), manifest["token"])
                or hello.get("identity") != identity.as_dict()
                or hello.get("resource_id") != resource
                or hello.get("job_id") != job_id
                or hello.get("kernel_boot") != kernel_boot
                or _kernel_boot_identity() != kernel_boot
                or kernel.identity(pid) != identity
                or kernel.resource_id(pid) != resource
            ):
                raise ScopeError("EXECUTION_SCOPE_AUTHENTICATION_FAILED")
            identity_record = {**binding, "identity": identity.as_dict(), "resource_id": resource}
            _scope_json(identity_record)
            write_state(directory, "identity.json", identity_record)
            connection.settimeout(None)
            result = cls()
            result.pid, result.scope, result.target = pid, scope, target
            result.job_id, result.directory = job_id, directory
            result.connection, result.stdout = connection, reader
            result.stdin = _ControlWriter(connection)
            result.cleanup_verified = False
            return result
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            if scope is not None:
                scope.stop((0.1, 0.1, 1), exclude_leader=False)
            if loaded:
                try:
                    _launchctl("bootout", target)
                except (OSError, subprocess.SubprocessError):
                    pass
            if reader is not None:
                reader.close()
            if connection is not None:
                connection.close()
            raise ScopeError("EXECUTION_SCOPE_START_FAILED") from None
        finally:
            listener.close()

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout if timeout is not None else 8)
        while time.monotonic() < deadline:
            if self.scope.kernel.identity(self.pid) is None:
                break
            time.sleep(0.01)
        # Always examine the entire fixed coalition, even if its helper exited.
        self.cleanup_verified = self.scope.stop((0.1, 0.1, 1), exclude_leader=False)
        try:
            _launchctl("bootout", self.target)
            if _launchctl("print", self.target).returncode != 113:
                self.cleanup_verified = False
        except (OSError, subprocess.SubprocessError):
            self.cleanup_verified = False
        self.connection.close()
        if not self.cleanup_verified:
            raise ScopeError("EXECUTION_SCOPE_SHUTDOWN_UNVERIFIED")
        return 0


def helper_connection(manifest_path):
    """Only the launchd helper reads this private capability, before exec."""
    manifest = _read_manifest(manifest_path)
    kernel = MacKernel()
    identity, resource = kernel.identity(os.getpid()), kernel.resource_id(os.getpid())
    if identity is None or resource is None:
        raise ScopeError("EXECUTION_SCOPE_IDENTITY_FAILED")
    scope = CoalitionScope(kernel, identity, resource)
    if {item.identity for item in scope.scan(False).values()} != {identity}:
        raise ScopeError("EXECUTION_SCOPE_NOT_DEDICATED")
    connection = socket.create_connection(("127.0.0.1", manifest["port"]), timeout=5)
    connection.set_inheritable(False)
    connection.settimeout(None)
    writer, reader = connection.makefile("wb"), connection.makefile("rb")
    hello = {
        "token": manifest["token"],
        "identity": identity.as_dict(),
        "resource_id": resource,
        "job_id": manifest["job_id"],
        "kernel_boot": manifest["kernel_boot"],
    }
    writer.write(json.dumps(hello).encode() + b"\n")
    writer.flush()
    # launchd EnvironmentVariables overlays launchd's environment. Do not pass
    # that inherited environment (including agent socket settings) to commands.
    os.environ.clear()
    return scope, connection, reader, writer, f"gui/{os.getuid()}/{manifest['label']}"


def helper_bootout(target):
    """Best-effort self-unload after cleanup, including parent control EOF."""
    try:
        _launchctl("bootout", target)
    except (OSError, subprocess.SubprocessError):
        pass

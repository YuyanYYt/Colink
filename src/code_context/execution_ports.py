"""Project port inventory and locally confirmed old-process release on macOS.

No MCP caller can authorize killing an unrelated local process. CoLink jobs use
their existing cancellation authority; external development processes require an
exact native identity/cwd/listener plan and confirmation in local control. PID
start times, unique identities and PID versions are rechecked before each signal.
The tested native audit-token primitive pins the PID version while signaling;
unsupported kernels fail closed without falling back to a numeric PID signal.
"""

import ctypes
import fcntl
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from code_context.execution_defaults import DEFAULT_DEVELOPMENT_PORTS as DEFAULT_PORTS
from code_context.execution_scope import MacKernel, ProcessIdentity, ScopeError, _Unique
from code_context.local_control import private_directory
from code_context.policy import SECRET_PATTERNS
from code_context.source_access import SourceError
from code_context.write_coordinator import request_digest, validate_request

PLAN = re.compile(r"pp_[a-f0-9]{32}")
DEVELOPMENT_PROCESS = re.compile(
    r"(?:python(?:[0-9.]+)?|node|nodejs|java|mvn|gradle|bash|zsh|sh|uv|"
    r"ruby|php|go|dotnet|deno|bun|npm|npx|vite|spring)"
)


class PortError(SourceError):
    """Content-free failures; never return argv, environment or private cwd."""


class _BSDInfo(ctypes.Structure):
    _fields_ = (
        [
            (name, ctypes.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "reserved",
            )
        ]
        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        + [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
        + [
            ("nice", ctypes.c_int32),
            ("start_sec", ctypes.c_uint64),
            ("start_usec", ctypes.c_uint64),
        ]
    )


class _VInfoStat(ctypes.Structure):
    _fields_ = (
        [
            ("dev", ctypes.c_uint32),
            ("mode", ctypes.c_uint16),
            ("nlink", ctypes.c_uint16),
            ("ino", ctypes.c_uint64),
            ("uid", ctypes.c_uint32),
            ("gid", ctypes.c_uint32),
        ]
        + [
            (name, ctypes.c_int64)
            for name in (
                "atime",
                "atimensec",
                "mtime",
                "mtimensec",
                "ctime",
                "ctimensec",
                "birthtime",
                "birthtimensec",
                "size",
                "blocks",
            )
        ]
        + [
            ("blksize", ctypes.c_int32),
            ("flags", ctypes.c_uint32),
            ("gen", ctypes.c_uint32),
            ("rdev", ctypes.c_uint32),
            ("spare", ctypes.c_int64 * 2),
        ]
    )


class _VNodeInfo(ctypes.Structure):
    _fields_ = [
        ("stat", _VInfoStat),
        ("type", ctypes.c_int),
        ("pad", ctypes.c_int),
        ("fsid", ctypes.c_int32 * 2),
    ]


class _VNodePath(ctypes.Structure):
    _fields_ = [("info", _VNodeInfo), ("path", ctypes.c_char * 1024)]


class _VNodePaths(ctypes.Structure):
    _fields_ = [("cwd", _VNodePath), ("root", _VNodePath)]


class NativeProcesses:
    """libproc fields match the local public proc_info.h ABI; exact sizes fail closed."""

    def __init__(self):
        if (
            sys.platform != "darwin"
            or ctypes.sizeof(_BSDInfo) != 136
            or ctypes.sizeof(_VNodePaths) != 2352
            or ctypes.sizeof(_Unique) != 56
        ):
            raise PortError(
                "PORT_NATIVE_UNAVAILABLE: native process identity inspection is required"
            )
        try:
            self.library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.library.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.library.proc_pidinfo.restype = ctypes.c_int
            self.library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            self.library.proc_pidpath.restype = ctypes.c_int
        except (OSError, AttributeError):
            raise PortError(
                "PORT_NATIVE_UNAVAILABLE: native process inspection is unavailable"
            ) from None
        self.kernel = None

    def _generation(self, pid):
        value = _Unique()
        if (
            self.library.proc_pidinfo(pid, 17, 0, ctypes.byref(value), ctypes.sizeof(value))
            != ctypes.sizeof(value)
            or value.unique <= 0
            or value.version <= 0
        ):
            return None
        return int(value.unique), int(value.version)

    def inspect(self, pid):
        before = self._generation(pid)
        if before is None:
            return None
        info, paths = _BSDInfo(), _VNodePaths()
        if self.library.proc_pidinfo(
            pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
        ) != ctypes.sizeof(info):
            return None
        if info.pid != pid or info.start_sec <= 0 or info.start_usec >= 1_000_000:
            return None
        got_cwd = self.library.proc_pidinfo(pid, 9, 0, ctypes.byref(paths), ctypes.sizeof(paths))
        executable = ctypes.create_string_buffer(4096)
        got_path = self.library.proc_pidpath(pid, executable, ctypes.sizeof(executable))
        cwd = None
        try:
            if got_cwd == ctypes.sizeof(paths):
                candidate = bytes(paths.cwd.path).decode("utf-8")
                actual = os.stat(candidate, follow_symlinks=False)
                if (actual.st_dev, actual.st_ino) == (
                    paths.cwd.info.stat.dev,
                    paths.cwd.info.stat.ino,
                ):
                    cwd = candidate
            executable_path = executable.value.decode("utf-8") if got_path > 0 else None
        except (OSError, UnicodeError):
            executable_path = None
        name = (bytes(info.name) or bytes(info.comm)).decode("utf-8", errors="replace")
        name = "".join(char for char in name if ord(char) >= 32)[:64]
        if any(pattern.search(name) for pattern in SECRET_PATTERNS):
            name = "[redacted]"
        if self._generation(pid) != before:
            return None
        return {
            "pid": pid,
            "ppid": info.ppid,
            "pgid": info.pgid,
            "uid": info.uid,
            "ruid": info.ruid,
            "svuid": info.svuid,
            "start_sec": info.start_sec,
            "start_usec": info.start_usec,
            "unique": before[0],
            "version": before[1],
            "name": name,
            "cwd": cwd,
            "executable": executable_path,
            "exited": info.status == 5,
        }

    def signal(self, process, requested_signal):
        """Only an already authorized caller may ask to signal this identity."""
        try:
            if self.kernel is None:
                self.kernel = MacKernel()
            identity = ProcessIdentity(process["pid"], process["unique"], process["version"])
            return self.kernel.signal_identity(identity, requested_signal)
        except ScopeError as exc:
            if "IDENTITY_CHANGED" in str(exc):
                raise PortError(
                    "PORT_IDENTITY_CHANGED: native process generation changed"
                ) from None
            raise PortError("PORT_SIGNAL_FAILED: native identity signal unavailable") from None


class PortsCoordinator:
    def __init__(
        self,
        source_for,
        execution_coordinator,
        control_alive,
        *,
        store_root=None,
        term_wait_seconds=2.0,
        kill_wait_seconds=1.0,
        clock=time.time,
    ):
        self.source_for, self.execution, self.control_alive = (
            source_for,
            execution_coordinator,
            control_alive,
        )
        self.directory = private_directory(
            Path(store_root or (self.execution.state.root / "ports"))
        )
        self.root = self.directory.root
        try:
            self.native = NativeProcesses()
        except PortError:
            # The optional port capability must not prevent read-only startup.
            # Every operation which inspects or signals a process checks this
            # before touching a native handle; there is no generic PID fallback.
            self.native = None
        self.clock = clock
        if not 0 <= term_wait_seconds <= 5 or not 0 <= kill_wait_seconds <= 2:
            raise PortError("INVALID_PORT_BUDGET: use bounded signal wait times")
        self.term_wait, self.kill_wait = term_wait_seconds, kill_wait_seconds
        self.lock = threading.RLock()
        self.plans, self.confirmed = {}, {}
        self.closed = False
        with self.directory.root_fd() as parent:
            self.lease = os.open(
                "ports.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            info = os.fstat(self.lease)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                os.close(self.lease)
                raise PortError("PORT_UNSAFE_STORE: use private owned process metadata")
            try:
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self.lease)
                raise PortError("PORT_ALREADY_OPEN: stop the current port coordinator") from None
            fd = os.open(
                "ports.sqlite3", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            info = os.fstat(fd)
            os.close(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                raise PortError("PORT_UNSAFE_STORE: private process receipts are required")
        self.db = sqlite3.connect(self.root / "ports.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA auto_vacuum=FULL")
        self.db.execute("PRAGMA max_page_count=256")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS receipts(request TEXT PRIMARY KEY,data TEXT NOT NULL)"
        )
        self.db.commit()

    def _require_native(self):
        if self.native is None:
            raise PortError("PORT_NATIVE_UNAVAILABLE: native port inspection is unavailable")

    def _ports(self, project_id):
        source = self.source_for(project_id)
        source.ensure_available()
        ports = tuple(self.execution.grants.get(project_id, {}).get("ports", DEFAULT_PORTS))
        if len(ports) > 16 or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
            raise PortError("PORT_SCOPE_UNAVAILABLE: choose explicit local development ports")
        return source, ports

    def _listeners(self, ports=None):
        self._require_native()
        try:
            selection = (
                ["-iTCP:" + ",".join(map(str, ports))]
                if ports is not None
                else ["-u", str(os.getuid()), "-iTCP"]
            )
            result = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-a", *selection, "-sTCP:LISTEN", "-Fpcufn"],
                cwd=self.root,
                env={"PATH": "/usr/bin:/bin:/usr/sbin", "HOME": str(self.root), "LC_ALL": "C"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise PortError("PORT_INSPECTION_FAILED: local port inventory is unavailable") from None
        if result.returncode not in {0, 1} or len(result.stdout) > 256 * 1024:
            raise PortError("PORT_INVENTORY_LIMIT: local port inventory exceeds safe bounds")
        rows, pid, uid, name = [], None, None, ""
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith("p"):
                pid, uid = int(line[1:]), None
            elif line.startswith("u"):
                uid = int(line[1:])
            elif line.startswith("c"):
                name = "".join(char for char in line[1:] if ord(char) >= 32)[:64]
                if any(pattern.search(name) for pattern in SECRET_PATTERNS):
                    name = "[redacted]"
            elif line.startswith("n") and pid is not None and uid is not None:
                address, _, port = line[1:].rpartition(":")
                if port.isdecimal() and (ports is None or int(port) in ports):
                    rows.append(
                        {
                            "pid": pid,
                            "uid": uid,
                            "process": name,
                            "address": address[:128],
                            "port": int(port),
                        }
                    )
        unique = {tuple(sorted(row.items())): row for row in rows}
        return sorted(unique.values(), key=lambda row: (row["port"], row["pid"], row["address"]))[
            :256
        ]

    def _owned_job(self, project_id, source, process):
        if (
            not process
            or self._protected(process["pid"])
            or any(
                process[key] != os.getuid() or process[key] == 0 for key in ("uid", "ruid", "svuid")
            )
        ):
            return None
        ancestors, item = set(), process
        for _ in range(64):
            if not item or item["pid"] in ancestors or item["pid"] <= 1:
                break
            ancestors.add(item["pid"])
            item = self.native.inspect(item["ppid"])
        for job_id in tuple(self.execution.live):
            snapshot = self.execution.manager.snapshot(job_id)
            if (
                snapshot.get("state") not in {"running", "starting"}
                or snapshot.get("pid") not in ancestors
            ):
                continue
            job = self.execution.store.get_job(job_id)
            if job and job["project_id"] == project_id and job["source_id"] == source.source_id:
                return job_id
        return None

    def _protected(self, pid):
        current = os.getpid()
        visited = set()
        for _ in range(64):
            if current == pid or current <= 1:
                return current == pid
            if current in visited:
                break
            visited.add(current)
            info = self.native.inspect(current)
            if info is None:
                break
            current = info["ppid"]
        return False

    def _belongs(self, source, process):
        if (
            not process
            or process["pid"] <= 1
            or self._protected(process["pid"])
            or any(
                process[field] != os.getuid() or process[field] == 0
                for field in ("uid", "ruid", "svuid")
            )
            or not process["cwd"]
            or not process["executable"]
        ):
            return False
        try:
            cwd, executable = Path(process["cwd"]).resolve(strict=True), Path(process["executable"])
            relative = cwd.relative_to(source.root)
            if any(
                cwd == excluded or excluded in cwd.parents for excluded in source.scanner._excluded
            ):
                return False
            current = source.root
            for part in relative.parts:
                current /= part
                if (current / ".git").exists() or (current / ".git").is_symlink():
                    return False
            if str(executable).startswith(("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/")):
                return False
            return bool(
                DEVELOPMENT_PROCESS.fullmatch(executable.name.lower())
            ) or executable.is_relative_to(source.root)
        except (OSError, ValueError):
            return False

    def status(self, project_id):
        source, ports = self._ports(project_id)
        self._require_native()
        listeners = []
        for row in self._listeners():
            process = self.native.inspect(row["pid"])
            own = self._owned_job(project_id, source, process) if process else None
            belongs = self._belongs(source, process)
            listeners.append(
                {
                    "port": row["port"],
                    "address": row["address"],
                    "pid": row["pid"],
                    "process": process["name"] if process else row["process"],
                    "ownership": "colink_job"
                    if own
                    else "project_process"
                    if belongs
                    else "other_or_unknown",
                    "job_id": own,
                    "release_available": own is not None or belongs,
                    "requires_local_confirmation": own is None,
                }
            )
        occupied = {row["port"] for row in listeners}
        return {
            "project_id": project_id,
            "ports": list(ports),
            "listeners": listeners,
            "observed_ports": sorted(occupied),
            "available_ports": [port for port in ports if port not in occupied],
        }

    def plan_release(self, project_id, pid, *, force=False):
        if type(pid) is not int or pid <= 1 or type(force) is not bool:
            raise PortError(
                "INVALID_PORT_TARGET: choose a listed process and explicit force preference"
            )
        with self.lock:
            grant = self.execution.authorize(project_id)
            source, ports = self._ports(project_id)
            self._require_native()
            process = self.native.inspect(pid)
            if process is None:
                raise PortError("PORT_PROCESS_GONE: refresh the local port inventory")
            job_id = self._owned_job(project_id, source, process)
            if job_id is None and not self._belongs(source, process):
                raise PortError("PORT_PROCESS_SCOPE: target is system, other-project or uncertain")
            listeners = [row for row in self._listeners() if row["pid"] == pid]
            if not listeners:
                raise PortError(
                    "PORT_PROCESS_NOT_LISTENING: choose a current registered-port listener"
                )
            if self.native.inspect(pid) != process:
                raise PortError("PORT_IDENTITY_CHANGED: process changed during planning")
            now = self.clock()
            self.plans = {
                key: plan for key, plan in self.plans.items() if now - plan["created"] < 300
            }
            self.confirmed = {
                key: value for key, value in self.confirmed.items() if key in self.plans
            }
            if len(self.plans) >= 128:
                raise PortError("PORT_PLAN_LIMIT: active port plan capacity is full")
            plan_id = "pp_" + uuid.uuid4().hex
            self.plans[plan_id] = {
                "id": plan_id,
                "project_id": project_id,
                "source_id": source.source_id,
                "epoch": request_digest(grant),
                "created": now,
                "process": process,
                "listeners": listeners,
                "job_id": job_id,
                "force": force,
            }
            return {
                "project_id": project_id,
                "port_plan_id": plan_id,
                "pid": pid,
                "process": process["name"],
                "ports": sorted({r["port"] for r in listeners}),
                "ownership": "colink_job" if job_id else "project_process",
                "job_id": job_id,
                "requires_local_confirmation": job_id is None,
                "force_requested": force,
                "expires_at": now + 300,
            }

    def _plan(self, plan_id):
        if not isinstance(plan_id, str) or PLAN.fullmatch(plan_id) is None:
            raise PortError("INVALID_PORT_PLAN: use a current port-release plan")
        plan = self.plans.get(plan_id)
        if plan is None or self.clock() - plan["created"] > 300:
            raise PortError("PORT_PLAN_EXPIRED: obtain a fresh process plan")
        source, ports = self._ports(plan["project_id"])
        grant = self.execution.authorize(plan["project_id"])
        if source.source_id != plan["source_id"] or request_digest(grant) != plan["epoch"]:
            raise PortError("PORT_PLAN_SCOPE: source or authorization changed")
        process = self.native.inspect(plan["process"]["pid"])
        if process != plan["process"]:
            raise PortError("PORT_IDENTITY_CHANGED: the planned process no longer matches")
        listeners = [row for row in self._listeners() if row["pid"] == process["pid"]]
        if listeners != plan["listeners"]:
            raise PortError("PORT_LISTENERS_CHANGED: registered listeners changed; replan")
        if plan["job_id"] is None and not self._belongs(source, process):
            raise PortError("PORT_PROCESS_SCOPE: planned process no longer belongs to this project")
        return plan

    def confirm_release(self, port_plan_id, *, allow_force=False):
        """LocalControl only. Never register this method as an MCP tool."""
        with self.lock:
            if self.control_alive() is not True or type(allow_force) is not bool:
                raise PortError("PORT_LOCAL_CONTROL_REQUIRED: confirm in the connected local app")
            self._require_native()
            plan = self._plan(port_plan_id)
            if allow_force and not plan["force"]:
                raise PortError("PORT_FORCE_NOT_PLANNED: review a plan explicitly requesting force")
            self.confirmed[port_plan_id] = allow_force
            return {
                "port_plan_id": port_plan_id,
                "confirmed": True,
                "force_authorized": allow_force,
                "pid": plan["process"]["pid"],
            }

    def pending_confirmations(self):
        """Private UI metadata only; stale plans remain unable to authorize signals."""
        with self.lock:
            return [
                {
                    "port_plan_id": plan["id"],
                    "project_id": plan["project_id"],
                    "pid": plan["process"]["pid"],
                    "process": plan["process"]["name"],
                    "ports": sorted({row["port"] for row in plan["listeners"]}),
                    "force_requested": plan["force"],
                    "requires_local_confirmation": True,
                    "expires_at": plan["created"] + 300,
                }
                for plan in sorted(self.plans.values(), key=lambda item: item["created"])
                if plan["job_id"] is None
                and plan["id"] not in self.confirmed
                and self.clock() - plan["created"] <= 300
            ]

    def _save(self, request, receipt):
        with self.db:
            self.db.execute(
                "DELETE FROM receipts WHERE json_extract(data,'$.created') < ?",
                (self.clock() - 24 * 3600,),
            )
            if (
                self.db.execute("SELECT count(*) FROM receipts").fetchone()[0] >= 256
                and not self.db.execute(
                    "SELECT request FROM receipts WHERE request=?", (request,)
                ).fetchone()
            ):
                raise PortError("PORT_RECEIPT_LIMIT: retained port receipts are full")
            self.db.execute(
                "INSERT OR REPLACE INTO receipts VALUES(?,?)", (request, json.dumps(receipt))
            )

    @staticmethod
    def _identity(process):
        if process is None:
            return None
        return tuple(
            process[field]
            for field in (
                "pid",
                "uid",
                "ruid",
                "svuid",
                "start_sec",
                "start_usec",
                "unique",
                "version",
            )
        )

    def _signal(self, plan, requested_signal):
        if self.control_alive() is not True:
            raise PortError("PORT_LOCAL_CONTROL_REQUIRED: local connection was revoked")
        grant = self.execution.authorize(plan["project_id"])
        if request_digest(grant) != plan["epoch"]:
            raise PortError("PORT_PLAN_SCOPE: local authorization changed before signaling")
        current = self.native.inspect(plan["process"]["pid"])
        if current != plan["process"]:
            raise PortError("PORT_IDENTITY_CHANGED: process identity changed before signaling")
        self.native.signal(current, requested_signal)

    def _wait(self, plan, seconds):
        deadline = time.monotonic() + seconds
        while True:
            current = self.native.inspect(plan["process"]["pid"])
            if self._identity(current) != self._identity(plan["process"]) or (
                current and current.get("exited")
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.025)

    def release(self, project_id, port_plan_id, request_id):
        validate_request(request_id)
        with self.lock:
            self.execution.authorize(project_id)
            source, ports = self._ports(project_id)
            self._require_native()
            previous = self.db.execute(
                "SELECT data FROM receipts WHERE request=?", (request_id,)
            ).fetchone()
            if previous:
                receipt = json.loads(previous[0])
                if (
                    receipt["project_id"] != project_id
                    or receipt["source_id"] != source.source_id
                    or receipt["plan_id"] != port_plan_id
                ):
                    raise PortError("REQUEST_ID_CONFLICT: port request belongs to another plan")
                current = self.native.inspect(receipt["result"]["pid"])
                if receipt["state"] != "released" and (
                    self._identity(current) != tuple(receipt["identity"]) or current.get("exited")
                ):
                    occupied = {row["port"] for row in self._listeners()}
                    receipt["state"] = "released"
                    receipt["result"].update(
                        state="released",
                        process_stopped=True,
                        ports_available=all(p not in occupied for p in receipt["ports"]),
                    )
                    self._save(request_id, receipt)
                return {
                    **receipt["result"],
                    "duplicate": True,
                    "requires_new_local_confirmation": receipt["state"] != "released",
                }
            plan = self._plan(port_plan_id)
            if plan["project_id"] != project_id:
                raise PortError("PORT_PLAN_SCOPE: plan belongs to another project")
            if plan["job_id"] is None and port_plan_id not in self.confirmed:
                raise PortError(
                    "PORT_LOCAL_CONFIRMATION_REQUIRED: confirm this exact process in the local app"
                )
            receipt = {
                "created": self.clock(),
                "project_id": project_id,
                "source_id": source.source_id,
                "identity": self._identity(plan["process"]),
                "ports": sorted({r["port"] for r in plan["listeners"]}),
                "plan_id": port_plan_id,
                "state": "stopping",
                "result": {
                    "project_id": project_id,
                    "port_plan_id": port_plan_id,
                    "pid": plan["process"]["pid"],
                    "state": "stopping",
                    "duplicate": False,
                    "scope": "confirmed_process" if plan["job_id"] is None else "colink_job",
                },
            }
            self._save(request_id, receipt)
            if plan["job_id"] is not None:
                self.execution.cancel(project_id, plan["job_id"])
            else:
                self._signal(plan, signal.SIGTERM)
            stopped = self._wait(plan, self.term_wait)
            forced = False
            if not stopped and plan["job_id"] is None and self.confirmed.get(port_plan_id) is True:
                self._signal(plan, signal.SIGKILL)
                stopped, forced = self._wait(plan, self.kill_wait), True
            occupied = {row["port"] for row in self._listeners()}
            available = all(row["port"] not in occupied for row in plan["listeners"])
            result = {
                **receipt["result"],
                "state": "released" if stopped else "stopping",
                "process_stopped": stopped,
                "ports_available": available,
                "force_used": forced,
                "force_authorized": self.confirmed.get(port_plan_id) is True,
            }
            receipt.update(state=result["state"], result=result)
            self._save(request_id, receipt)
            self.confirmed.pop(port_plan_id, None)
            return result

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.plans.clear()
                self.confirmed.clear()
                self.db.close()
                fcntl.flock(self.lease, fcntl.LOCK_UN)
                os.close(self.lease)

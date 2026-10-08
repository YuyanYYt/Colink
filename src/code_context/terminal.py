"""Explicit developer-authorized host terminal, supervised but not sandboxed.

Only opaque receipts are persisted. Commands, environment values and stdin are
not written to the ledger. A request reserved before a crash is never respawned.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
from pathlib import Path

from code_context.execution_process import TERMINAL_STATES, ProcessError, ProcessManager
from code_context.local_control import private_directory, read_state, write_state
from code_context.source_access import SourceError


class HostTerminal:
    def __init__(self, root, source_for, authorize, on_change, tools):
        self.state = private_directory(root)
        self.source_for, self.authorize, self.on_change = source_for, authorize, on_change
        self.tools = tools
        self.epoch = secrets.token_hex(16)
        self.secret = secrets.token_bytes(32)
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = False
        self.manager = ProcessManager(
            sandbox_check=self._approved, scope_root=self.state.root / "scope"
        )
        self.stop = threading.Event()
        self.monitor = threading.Thread(
            target=self._monitor, name="host-terminal-grants", daemon=True
        )
        self.monitor.start()

    def _approved(self, argv, cwd, env):
        # The process manager checks again immediately before dispatch.
        with self.lock:
            for job in self.jobs.values():
                if (
                    job.get("argv") == tuple(argv)
                    and job.get("cwd") == cwd
                    and job.get("env") == env
                ):
                    try:
                        if not self.closed and self.authorize(job["project_id"]) == job["grant"]:
                            return True
                    except SourceError:
                        continue
        return False

    def _fingerprint(self, value):
        return hmac.new(
            self.secret, json.dumps(value, sort_keys=True).encode(), "sha256"
        ).hexdigest()

    @staticmethod
    def _request(project_id, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", request_id
        ):
            raise SourceError("INVALID_TERMINAL_REQUEST_ID")
        return (
            "request-"
            + hashlib.sha256((project_id + "\0" + request_id).encode()).hexdigest()
            + ".json"
        )

    def _environment(self, values):
        if (
            not isinstance(values, dict)
            or len(values) > 128
            or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
                or not isinstance(v, str)
                or "\0" in v
                or len(v.encode()) > 16384
                for k, v in values.items()
            )
        ):
            raise SourceError("INVALID_TERMINAL_ENVIRONMENT")
        # Do not leak the tunnel/control-plane credentials inherited by the app.
        allowed = {"HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "JAVA_HOME"}
        env = {
            k: v
            for k, v in os.environ.items()
            if k in allowed or k.startswith(("DB_", "PG", "MYSQL", "REDIS", "DATABASE_", "SPRING_"))
        }
        roots = [
            str(Path(p).parent)
            for p in self.tools.values()
            if isinstance(p, str) and Path(p).is_absolute()
        ]
        roots += [
            str(Path.home() / ".local/bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        env.update(
            HOME=str(Path.home()),
            PATH=":".join(dict.fromkeys(roots)),
            TERM="xterm-256color",
            COLINK_HOST_TERMINAL="1",
        )
        env.update(values)
        return env

    def start(
        self,
        project_id,
        request_id,
        command,
        *,
        cwd="",
        env=None,
        tty=True,
        service=True,
        timeout_seconds=300,
    ):
        grant = self.authorize(project_id)
        source = self.source_for(project_id)
        source.ensure_available()
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 128
            or any(not isinstance(v, str) or not v or "\0" in v for v in command)
            or sum(len(v.encode()) for v in command) > 32768
            or not isinstance(cwd, str)
            or "\0" in cwd
            or Path(cwd).is_absolute()
            or type(tty) is not bool
            or type(service) is not bool
            or type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 300
        ):
            raise SourceError("INVALID_TERMINAL_COMMAND")
        directory = (source.root / cwd).resolve(strict=True)
        if not directory.is_relative_to(source.root.resolve()) or not directory.is_dir():
            raise SourceError("TERMINAL_CWD_OUTSIDE_PROJECT")
        environment = self._environment(env or {})
        if command[0] == "shell":
            if len(command) != 2:
                raise SourceError("INVALID_TERMINAL_SHELL")
            argv = ["/bin/zsh", "-f", "-c", command[1]]
        else:
            executable = (
                shutil.which(command[0], path=environment["PATH"])
                if "/" not in command[0]
                else str((directory / command[0]).resolve())
            )
            if not executable or not os.access(executable, os.X_OK):
                raise SourceError("TERMINAL_COMMAND_UNAVAILABLE")
            argv = [executable, *command[1:]]
        name = self._request(project_id, request_id)
        fingerprint = self._fingerprint([command, cwd, env, tty, service, timeout_seconds])
        with self.lock:
            if self.closed:
                raise SourceError("TERMINAL_CLOSED")
            previous = read_state(self.state, name) if (self.state.root / name).exists() else None
            if previous:
                if previous["epoch"] != self.epoch:
                    return {
                        "job_id": previous["job_id"],
                        "state": "interrupted",
                        "reason": "runtime_restarted",
                        "replayed": True,
                        "automatic_restart": False,
                    }
                if previous["fingerprint"] != fingerprint:
                    raise SourceError("TERMINAL_REQUEST_CONFLICT")
                if previous["job_id"] not in self.jobs:
                    raise SourceError(
                        "TERMINAL_START_UNCERTAIN: reserved request will not be replayed"
                    )
                return self.status(project_id, previous["job_id"])
            if len(self.jobs) >= 128:
                raise SourceError("TERMINAL_SESSION_CAPACITY: reconnect locally")
            job_id = "job-" + secrets.token_hex(16)
            job = {
                "project_id": project_id,
                "grant": grant,
                "argv": tuple(argv),
                "cwd": str(directory),
                "env": environment,
                "inputs": {},
            }
            write_state(
                self.state,
                name,
                {"epoch": self.epoch, "job_id": job_id, "fingerprint": fingerprint},
            )
            self.jobs[job_id] = job
            try:
                self.manager.submit(
                    job_id,
                    argv,
                    str(directory),
                    environment,
                    service=service,
                    timeout_seconds=timeout_seconds,
                    interactive=True,
                    tty=tty,
                    on_exit=lambda result: self._exited(project_id, job_id),
                )
            except ProcessError:
                job["failed"] = True
                raise SourceError(
                    "TERMINAL_START_FAILED: reuse request ID; do not blindly repeat commands"
                ) from None
            return self.status(project_id, job_id)

    def _exited(self, project_id, job_id):
        with self.lock:
            if job_id in self.jobs:
                for field in ("argv", "env"):
                    self.jobs[job_id].pop(field, None)
        self.on_change(project_id)

    def _job(self, project_id, job_id, *, active=False):
        self.source_for(project_id).ensure_available()
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["project_id"] != project_id:
                raise SourceError("TERMINAL_JOB_UNKNOWN")
            if job.get("failed"):
                raise SourceError("TERMINAL_START_FAILED: inspect local state")
            if active and self.authorize(project_id) != job["grant"]:
                raise SourceError("TERMINAL_GRANT_CHANGED")
            return job

    def status(self, project_id, job_id):
        self._job(project_id, job_id)
        result = self.manager.snapshot(job_id)
        if result["service"] and result["state"] in {"queued", "starting", "running"}:
            try:
                self._job(project_id, job_id, active=True)
                self.manager.touch(job_id)
            except SourceError:
                self.manager.cancel(job_id)
        return {
            **result,
            "execution_scope": "current_os_user",
            "filesystem_isolated": False,
            "database_scope": "account_permissions",
        }

    def output(self, project_id, job_id, cursor=0, max_bytes=65536, wait_ms=0):
        self.status(project_id, job_id)
        return self.manager.read_output(job_id, cursor, max_bytes, wait_ms)

    def send(self, project_id, job_id, request_id, data, eof=False):
        self._request(project_id, request_id)
        if not isinstance(data, str) or len(data.encode()) > 16384 or type(eof) is not bool:
            raise SourceError("INVALID_TERMINAL_INPUT")
        with self.lock:
            job = self._job(project_id, job_id, active=True)
            fingerprint = self._fingerprint([data, eof])
            previous = job["inputs"].get(request_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise SourceError("TERMINAL_REQUEST_CONFLICT")
                return {**previous["result"], "replayed": True}
            if len(job["inputs"]) >= 1024:
                raise SourceError("TERMINAL_INPUT_CAPACITY")
            snapshot = self.manager.snapshot(job_id)
            if snapshot["state"] != "running":
                raise SourceError("TERMINAL_NOT_READY: wait for running status")
            if snapshot["tty"] and eof:
                raise SourceError("TERMINAL_TTY_EOF: send exit or control-D instead")
            # Reserve before sending: uncertain partial delivery cannot be repeated.
            result = {"job_id": job_id, "delivery": "uncertain", "executed": False}
            job["inputs"][request_id] = {"fingerprint": fingerprint, "result": result}
            result.update(self.manager.send_input(job_id, data.encode(), eof=eof))
            return result

    def cancel(self, project_id, job_id):
        self._job(project_id, job_id)
        return self.manager.cancel(job_id)

    def list(self, project_id):
        self.source_for(project_id).ensure_available()
        with self.lock:
            ids = [
                key
                for key, value in self.jobs.items()
                if value["project_id"] == project_id and not value.get("failed")
            ]
        results = []
        for job_id in ids:
            try:
                results.append(self.status(project_id, job_id))
            except ProcessError:
                continue
        return {"jobs": results, "runtime_epoch": self.epoch}

    def revoke(self):
        with self.lock:
            ids = tuple(self.jobs)
        for job_id in ids:
            try:
                self.manager.cancel(job_id)
            except ProcessError:
                pass

    def _monitor(self):
        while not self.stop.wait(0.25):
            with self.lock:
                jobs = tuple(self.jobs.items())
            for job_id, job in jobs:
                try:
                    if self.manager.snapshot(job_id)["state"] in TERMINAL_STATES:
                        continue
                    try:
                        authorized = self.authorize(job["project_id"]) == job["grant"]
                    except SourceError:
                        authorized = False
                    if not authorized:
                        self.manager.cancel(job_id)
                except ProcessError:
                    continue

    def close(self):
        self.closed = True
        self.stop.set()
        self.monitor.join(timeout=1)
        return self.manager.close()

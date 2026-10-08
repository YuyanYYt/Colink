"""Bounded supervised process sessions, not an operating-system sandbox.

Only a trusted coordinator can supply and revalidate prepared sandbox argv.
There is deliberately no automatic plain-process/shell fallback. Each job has
an independent supervisor: its parent's control-pipe EOF requests shutdown.
Linux subreaping and macOS resource coalitions preserve descendant ownership.
macOS requires a dedicated launchd supervisor and validated libproc identities.
Status separates verified lifecycle cleanup from an isolation guarantee.
Unverifiable live descendants fail closed.
"""

import base64
import ctypes
import importlib.util
import json
import math
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

MAX_JOB_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_GLOBAL_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_FRAME_BYTES = 128 * 1024
MAX_CONFIG_BYTES = 256 * 1024
TERMINAL_STATES = frozenset(
    {"exited", "timeout", "cancelled", "interrupted", "resource_limit", "stop_failed", "failed"}
)


class ProcessError(ValueError):
    """Content-free process control errors."""


@dataclass
class _Job:
    job_id: str
    argv: tuple
    cwd: str
    env: dict
    service: bool
    timeout: float
    on_exit: object
    output: object
    created: float
    stdin_payload: bytes | None = None
    interactive: bool = False
    tty: bool = False
    state: str = "queued"
    started: float | None = None
    completed: float | None = None
    pid: int | None = None
    exit_code: int | None = None
    reason: str | None = None
    observation_error_code: str | None = None
    cleanup_verified: bool = False
    observed_tree_stopped: bool = False
    isolation_complete: bool = False
    tree_scope: str = "not_started"
    resource_id: int | None = None
    resource_peak_rss_bytes: int = 0
    resource_peak_processes: int = 0
    resource_peak_cpu_cores: float = 0.0
    resource_cpu_seconds_observed: float = 0.0
    stop_reason: str | None = None
    supervisor: object = None
    control_lock: object = field(default_factory=threading.Lock)
    worker: object = None
    reclaimed: str | None = None
    callback_error: bool = False
    callback_done: bool = False


class ProcessManager:
    def __init__(
        self,
        *,
        sandbox_check,
        max_services=2,
        max_finite=1,
        service_idle_seconds=1200,
        max_jobs=16,
        global_output_bytes=MAX_GLOBAL_OUTPUT_BYTES,
        job_output_bytes=MAX_JOB_OUTPUT_BYTES,
        output_retention_seconds=600,
        summary_retention_seconds=3600,
        termination_grace=(3, 2, 1),
        scope_root=None,
        max_rss_bytes=2 * 1024 * 1024 * 1024,
        max_processes=128,
        resource_sample_seconds=0.1,
        max_cpu_cores=4.0,
        cpu_window_seconds=30.0,
        clock=None,
    ):
        if sys.platform not in {"linux", "darwin"}:
            raise ProcessError("EXECUTION_PLATFORM_UNSUPPORTED")
        if (
            not callable(sandbox_check)
            or type(max_services) is not int
            or not 1 <= max_services <= 2
            or type(max_finite) is not int
            or max_finite != 1
            or type(max_jobs) is not int
            or not 1 <= max_jobs <= 16
            or type(global_output_bytes) is not int
            or not 4 <= global_output_bytes <= MAX_GLOBAL_OUTPUT_BYTES
            or type(job_output_bytes) is not int
            or not 4 <= job_output_bytes <= MAX_JOB_OUTPUT_BYTES
            or job_output_bytes > global_output_bytes
            or not self._seconds(service_idle_seconds, 1200)
            or not self._seconds(output_retention_seconds, 600)
            or not self._seconds(summary_retention_seconds, 3600)
            or not isinstance(termination_grace, (tuple, list))
            or len(termination_grace) != 3
            or any(not self._seconds(value, 4) for value in termination_grace)
            or type(max_rss_bytes) is not int
            or not 1 <= max_rss_bytes <= 2 * 1024 * 1024 * 1024
            or type(max_processes) is not int
            or not 1 <= max_processes <= 128
            or not self._seconds(resource_sample_seconds, 0.1)
            or resource_sample_seconds < 0.01
            or not self._seconds(max_cpu_cores, 4)
            or not self._seconds(cpu_window_seconds, 30)
            or cpu_window_seconds < resource_sample_seconds
        ):
            raise ProcessError("INVALID_EXECUTION_MANAGER_LIMITS")
        self._sandbox_check = sandbox_check
        self.max_services, self.max_finite, self.max_jobs = max_services, max_finite, max_jobs
        self.global_output_bytes, self.job_output_bytes = global_output_bytes, job_output_bytes
        self.service_idle_seconds = service_idle_seconds
        self.output_retention_seconds = output_retention_seconds
        self.summary_retention_seconds = summary_retention_seconds
        self.termination_grace = tuple(termination_grace)
        self.scope_root = scope_root
        if sys.platform == "darwin" and scope_root is not None:
            from code_context.local_control import private_directory

            try:
                # Create and pin the common root before concurrent dispatchers
                # begin creating their distinct private job directories.
                self.scope_root = private_directory(Path(scope_root)).root
            except (OSError, ValueError):
                raise ProcessError("EXECUTION_SCOPE_CONTROL_UNSAFE") from None
        self.resource_limits = {
            "max_rss_bytes": max_rss_bytes,
            "max_processes": max_processes,
            "sample_seconds": resource_sample_seconds,
            "max_cpu_cores": max_cpu_cores,
            "cpu_window_seconds": cpu_window_seconds,
            "cpu_accounting": "sampled_live_processes; exited_between_samples_can_be_missed",
        }
        self._clock = clock or time.monotonic
        self._condition = threading.Condition(threading.RLock())
        self._jobs = {}
        self._summaries = deque()
        self._closed = self._blocked = False
        self._dispatcher = threading.Thread(
            target=self._dispatch, name="execution-queue", daemon=True
        )
        self._dispatcher.start()

    @staticmethod
    def _seconds(value, maximum):
        return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum

    def _approved(self, job):
        try:
            return self._sandbox_check(job.argv, job.cwd, dict(job.env)) is True
        except Exception:
            return False

    def submit(
        self,
        job_id,
        argv,
        cwd,
        env,
        service=False,
        timeout_seconds=300,
        on_exit=None,
        stdin_payload=None,
        interactive=False,
        tty=False,
    ):
        from code_context.execution_output import OutputBuffer

        if (
            not isinstance(job_id, str)
            or not 1 <= len(job_id) <= 256
            or "\x00" in job_id
            or not isinstance(argv, (list, tuple))
            or not 1 <= len(argv) <= 256
            or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv)
            or not isinstance(env, dict)
            or any(
                not isinstance(k, str)
                or not isinstance(v, str)
                or not k
                or "\x00" in k + v
                or "=" in k
                for k, v in env.items()
            )
            or type(service) is not bool
            or type(interactive) is not bool
            or type(tty) is not bool
            or tty
            and not interactive
            or interactive
            and stdin_payload is not None
            or not self._seconds(timeout_seconds, 300)
            or on_exit is not None
            and not callable(on_exit)
            or stdin_payload is not None
            and (not isinstance(stdin_payload, bytes) or len(stdin_payload) > 64 * 1024)
        ):
            raise ProcessError("INVALID_EXECUTION_PROCESS")
        try:
            cwd = os.fspath(cwd)
            if not os.path.isabs(cwd) or not Path(cwd).is_dir():
                raise ValueError
            raw = json.dumps(
                {
                    "argv": argv,
                    "cwd": cwd,
                    "env": env,
                    "stdin_payload": (
                        base64.b64encode(stdin_payload).decode("ascii")
                        if stdin_payload is not None
                        else None
                    ),
                },
                allow_nan=False,
            ).encode()
            if len(raw) > MAX_CONFIG_BYTES - 2048:
                raise ValueError
        except (ValueError, TypeError, OSError, UnicodeError):
            raise ProcessError("INVALID_EXECUTION_PROCESS") from None
        job = _Job(
            job_id,
            tuple(argv),
            cwd,
            dict(env),
            service,
            float(timeout_seconds),
            on_exit,
            OutputBuffer(self.job_output_bytes, min(64 * 1024, self.job_output_bytes // 4)),
            self._clock(),
            stdin_payload=stdin_payload,
            interactive=interactive,
            tty=tty,
        )
        if not self._approved(job):
            raise ProcessError("EXECUTION_SANDBOX_REQUIRED")
        if sys.platform == "darwin" and self.scope_root is None:
            raise ProcessError("EXECUTION_SCOPE_REQUIRED")
        with self._condition:
            self._collect_locked()
            if self._closed or self._blocked:
                raise ProcessError("EXECUTION_MANAGER_UNAVAILABLE")
            if job_id in self._jobs:
                raise ProcessError("EXECUTION_JOB_ALREADY_EXISTS")
            if len(self._jobs) >= self.max_jobs:
                retired = next(
                    (
                        key
                        for key, value in self._jobs.items()
                        if value.state in TERMINAL_STATES
                        and value.output.retained_bytes == 0
                        and value.state != "stop_failed"
                        and not value.callback_error
                        and (value.on_exit is None or value.callback_done)
                    ),
                    None,
                )
                if retired is None:
                    raise ProcessError("EXECUTION_JOB_CAPACITY")
                del self._jobs[retired]
            self._jobs[job_id] = job
            self._condition.notify_all()
            return self._snapshot_locked(job)

    def _snapshot_locked(self, job):
        return {
            "job_id": job.job_id,
            "state": job.state,
            "service": job.service,
            "interactive": job.interactive,
            "tty": job.tty,
            "pid": job.pid,
            "exit_code": job.exit_code,
            "reason": job.reason,
            "observation_error_code": job.observation_error_code,
            "created": job.created,
            "started": job.started,
            "completed": job.completed,
            "cleanup_verified": job.cleanup_verified,
            "observed_tree_stopped": job.observed_tree_stopped,
            "isolation_complete": job.isolation_complete,
            "tree_scope": job.tree_scope,
            "resource_id": job.resource_id,
            "resource_peak_rss_bytes": job.resource_peak_rss_bytes,
            "resource_peak_processes": job.resource_peak_processes,
            "resource_peak_cpu_cores": job.resource_peak_cpu_cores,
            "resource_cpu_seconds_observed": job.resource_cpu_seconds_observed,
            "resource_limits_soft": True,
            "resource_limits": dict(self.resource_limits),
            "output_bytes": job.output.total_bytes,
            "retained_output_bytes": job.output.retained_bytes,
            "output_reclaimed": job.reclaimed,
            "callback_error": job.callback_error,
        }

    def _job(self, job_id):
        try:
            return self._jobs[job_id]
        except (KeyError, TypeError):
            raise ProcessError("EXECUTION_JOB_UNKNOWN") from None

    def snapshot(self, job_id):
        with self._condition:
            self._collect_locked()
            return self._snapshot_locked(self._job(job_id))

    def read_output(self, job_id, cursor=0, max_bytes=64 * 1024, wait_ms=0):
        with self._condition:
            self._collect_locked()
            job = self._job(job_id)
        # Never hold the manager lock while waiting for a subprocess producer.
        result = job.output.read(cursor, max_bytes, wait_ms)
        result["output_reclaimed"] = job.reclaimed
        return result

    def ack_output(self, job_id, cursor):
        with self._condition:
            job = self._job(job_id)
            if (
                type(cursor) is not int
                or cursor != job.output.total_bytes
                or job.state not in TERMINAL_STATES
                or not job.output.finished
            ):
                raise ProcessError("EXECUTION_OUTPUT_NOT_ACKNOWLEDGEABLE")
            job.output.clear()
            job.reclaimed = "acknowledged"
            return self._snapshot_locked(job)

    def _summary_bytes(self):
        return sum(
            sum(len(chunk["text"].encode()) for chunk in item["chunks"]) for item in self._summaries
        )

    def _collect_locked(self):
        now = self._clock()
        while (
            self._summaries
            and now - self._summaries[0]["completed"] >= self.summary_retention_seconds
        ):
            self._summaries.popleft()
        for job in self._jobs.values():
            if (
                job.state in TERMINAL_STATES
                and job.completed is not None
                and now - job.completed >= self.output_retention_seconds
            ):
                job.output.clear()
                job.reclaimed = job.reclaimed or "terminal_retention_expired"

    def _make_room_locked(self, job, required):
        self._collect_locked()
        other = sum(
            value.output.retained_bytes for value in self._jobs.values() if value is not job
        )
        other += self._summary_bytes()
        for value in self._jobs.values():
            if (
                other + min(self.job_output_bytes, job.output.retained_bytes + required)
                <= self.global_output_bytes
            ):
                break
            if value is not job and value.state in TERMINAL_STATES and value.output.retained_bytes:
                other -= value.output.retained_bytes
                value.output.clear()
                value.reclaimed = "global_budget"
        while (
            self._summaries
            and other + min(self.job_output_bytes, job.output.retained_bytes + required)
            > self.global_output_bytes
        ):
            old = self._summaries.popleft()
            other -= sum(len(chunk["text"].encode()) for chunk in old["chunks"])
        job.output.set_capacity(
            max(0, min(self.job_output_bytes, self.global_output_bytes - other))
        )

    def output_usage(self):
        with self._condition:
            self._collect_locked()
            logs = sum(job.output.retained_bytes for job in self._jobs.values())
            summaries = self._summary_bytes()
            return {
                "log_bytes": logs,
                "summary_bytes": summaries,
                "total_bytes": logs + summaries,
                "max_bytes": self.global_output_bytes,
            }

    def failure_summaries(self):
        with self._condition:
            self._collect_locked()
            return json.loads(json.dumps(list(self._summaries)))

    def _control(self, job, action, **values):
        with job.control_lock:
            child = job.supervisor
            if child is None or child.stdin is None or child.stdin.closed:
                return False
            try:
                child.stdin.write(json.dumps({"action": action, **values}).encode() + b"\n")
                child.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def send_input(self, job_id, data, *, eof=False):
        if not isinstance(data, bytes) or len(data) > 16 * 1024 or type(eof) is not bool:
            raise ProcessError("INVALID_TERMINAL_INPUT")
        with self._condition:
            job = self._job(job_id)
            if not job.interactive or job.state != "running":
                raise ProcessError("TERMINAL_NOT_READY: wait for running status")
            if job.tty and eof:
                raise ProcessError("TERMINAL_TTY_EOF: send exit or control-D instead")
        if not self._control(job, "input", data=base64.b64encode(data).decode("ascii"), eof=eof):
            raise ProcessError("TERMINAL_INPUT_UNCERTAIN: read status before continuing")
        return {"job_id": job_id, "delivery": "queued_to_supervisor", "executed": False}

    def touch(self, job_id):
        with self._condition:
            job = self._job(job_id)
            if not job.service or job.state not in {"queued", "starting", "running"}:
                raise ProcessError("EXECUTION_SERVICE_NOT_ACTIVE")
        if job.state == "running" and not self._control(job, "touch"):
            raise ProcessError("EXECUTION_SUPERVISOR_UNAVAILABLE")
        return self.snapshot(job_id)

    def cancel(self, job_id):
        notify = False
        with self._condition:
            job = self._job(job_id)
            if job.state in TERMINAL_STATES:
                return self._snapshot_locked(job)
            job.stop_reason = "cancelled"
            if job.state == "queued":
                self._finish_locked(
                    job,
                    "cancelled",
                    observed_tree_stopped=True,
                    cleanup_verified=True,
                    tree_scope="not_started",
                )
                notify = True
            self._condition.notify_all()
        if notify:
            self._notify_exit(job)
        else:
            self._control(job, "cancel")
        return self.snapshot(job_id)

    def _dispatch(self):
        while True:
            with self._condition:
                self._collect_locked()
                if self._closed:
                    return
                active = [
                    job for job in self._jobs.values() if job.state in {"starting", "running"}
                ]
                services = sum(job.service for job in active)
                finite = sum(not job.service for job in active)
                job = (
                    None
                    if self._blocked
                    else next(
                        (
                            value
                            for value in self._jobs.values()
                            if value.state == "queued"
                            and (
                                services < self.max_services
                                if value.service
                                else finite < self.max_finite
                            )
                        ),
                        None,
                    )
                )
                if job is None:
                    self._condition.wait(0.1)
                    continue
                job.state = "starting"
                job.worker = threading.Thread(
                    target=self._run, args=(job,), name="execution-" + job.job_id[:24], daemon=True
                )
                job.worker.start()

    def _finish_locked(self, job, state, **values):
        if job.state in TERMINAL_STATES:
            return
        job.state, job.completed = state, self._clock()
        for name in (
            "exit_code",
            "reason",
            "observation_error_code",
            "cleanup_verified",
            "observed_tree_stopped",
            "isolation_complete",
            "tree_scope",
            "resource_id",
            "resource_peak_rss_bytes",
            "resource_peak_processes",
            "resource_peak_cpu_cores",
            "resource_cpu_seconds_observed",
        ):
            if name in values:
                setattr(job, name, values[name])
        self._make_room_locked(job, 8)
        job.output.finish(state)
        if state != "exited" or job.exit_code != 0:
            summary = job.output.tail(min(64 * 1024, self.global_output_bytes))
            summary_bytes = sum(len(chunk["text"].encode()) for chunk in summary["chunks"])
            # Summaries share the global budget rather than creating a second
            # unaccounted cache; if necessary give up this job's full log.
            if (
                sum(value.output.retained_bytes for value in self._jobs.values())
                + self._summary_bytes()
                + summary_bytes
                > self.global_output_bytes
            ):
                job.output.clear()
                job.reclaimed = "global_budget"
            self._summaries.append(
                {
                    "job_id": job.job_id,
                    "state": state,
                    "completed": job.completed,
                    "chunks": summary["chunks"],
                    "omitted": summary["omitted"],
                    "total_bytes": summary["total_bytes"],
                }
            )
            while (
                len(self._summaries) > 16
                or self.output_usage()["total_bytes"] > self.global_output_bytes
            ):
                self._summaries.popleft()
        if state == "stop_failed":
            self._blocked = True
        self._condition.notify_all()

    def _notify_exit(self, job):
        if job.on_exit is not None:
            try:
                with self._condition:
                    snapshot = self._snapshot_locked(job)
                job.on_exit(snapshot)
            except Exception:
                with self._condition:
                    job.callback_error = self._blocked = True
                    self._condition.notify_all()
        with self._condition:
            job.callback_done = True
            self._condition.notify_all()

    def _run(self, job):
        final = None
        child = None
        try:
            if not self._approved(job):
                final = {
                    "state": "failed",
                    "reason": "sandbox_revalidation_failed",
                    "tree_scope": "not_started",
                    "cleanup_verified": True,
                    "observed_tree_stopped": True,
                }
                return
            with self._condition:
                if job.stop_reason:
                    final = {
                        "state": job.stop_reason,
                        "tree_scope": "not_started",
                        "cleanup_verified": True,
                        "observed_tree_stopped": True,
                    }
                    return
            with job.control_lock:
                if sys.platform == "darwin":
                    from code_context.execution_scope import LaunchdSupervisor

                    child = LaunchdSupervisor.spawn(
                        self.scope_root, Path(__file__).resolve(), job_id=job.job_id
                    )
                else:
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            "-I",
                            "-S",
                            "-X",
                            "utf8",
                            str(Path(__file__).resolve()),
                            "--supervise",
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                        start_new_session=True,
                        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
                    )
                job.supervisor = child
                config = {
                    "argv": job.argv,
                    "cwd": job.cwd,
                    "env": job.env,
                    "service": job.service,
                    "interactive": job.interactive,
                    "tty": job.tty,
                    "timeout": job.timeout,
                    "idle": self.service_idle_seconds,
                    "grace": self.termination_grace,
                    "resource_limits": self.resource_limits,
                    "stdin_payload": (
                        base64.b64encode(job.stdin_payload).decode("ascii")
                        if job.stdin_payload is not None
                        else None
                    ),
                }
                child.stdin.write(json.dumps(config).encode() + b"\n")
                child.stdin.flush()
                job.stdin_payload = None
                config["stdin_payload"] = None
            if job.stop_reason:
                self._control(job, "cancel")
            while True:
                raw = child.stdout.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                    raise ValueError
                event = json.loads(raw)
                kind = event.get("kind")
                if kind == "output":
                    data = base64.b64decode(event["data"], validate=True)
                    if len(data) > 64 * 1024:
                        raise ValueError
                    with self._condition:
                        self._make_room_locked(job, len(data) * 3 + 4)
                        job.output.append(event["stream"], data)
                elif kind == "started":
                    with self._condition:
                        job.state, job.pid, job.started = "running", event["pid"], self._clock()
                        job.tree_scope = event["tree_scope"]
                        job.resource_id = event.get("resource_id")
                        self._condition.notify_all()
                elif kind == "resources":
                    with self._condition:
                        job.resource_peak_rss_bytes = event["resource_peak_rss_bytes"]
                        job.resource_peak_processes = event["resource_peak_processes"]
                        job.resource_peak_cpu_cores = event.get("resource_peak_cpu_cores", 0.0)
                        job.resource_cpu_seconds_observed = event.get(
                            "resource_cpu_seconds_observed", 0.0
                        )
                        self._condition.notify_all()
                elif kind == "exit":
                    if event.get("state") not in TERMINAL_STATES:
                        raise ValueError
                    final = event
                    break
                else:
                    raise ValueError
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            final = {
                "state": "stop_failed" if child else "failed",
                "reason": "supervisor_unavailable",
            }
        finally:
            if child is not None:
                with job.control_lock:
                    if child.stdin and not child.stdin.closed:
                        try:
                            child.stdin.close()
                        except OSError:
                            pass
                try:
                    child.wait(timeout=sum(self.termination_grace) + 4)
                except subprocess.TimeoutExpired:
                    # Keep the supervisor alive to continue owning cleanup; do
                    # not kill it and knowingly orphan an unverified tree.
                    final = {"state": "stop_failed", "reason": "supervisor_shutdown_unverified"}
                except ValueError:
                    final = {"state": "stop_failed", "reason": "scope_shutdown_unverified"}
                if hasattr(child, "cleanup_verified"):
                    final = final or {
                        "state": "stop_failed",
                        "reason": "supervisor_exit_unverified",
                    }
                    final["cleanup_verified"] = final["observed_tree_stopped"] = (
                        child.cleanup_verified
                    )
                    final["tree_scope"] = "macos_resource_coalition"
                    final["resource_id"] = child.scope.resource_id
                    if not child.cleanup_verified:
                        final["state"] = "stop_failed"
                if child.stdout:
                    child.stdout.close()
            final = final or {"state": "stop_failed", "reason": "supervisor_exit_unverified"}
            with self._condition:
                self._finish_locked(
                    job,
                    final["state"],
                    **{k: v for k, v in final.items() if k not in {"state", "kind"}},
                )
                job.argv, job.env, job.stdin_payload = (), {}, None
            self._notify_exit(job)

    def close(self):
        with self._condition:
            self._closed = True
            jobs = list(self._jobs.values())
            self._condition.notify_all()
        for job in jobs:
            if job.state not in TERMINAL_STATES:
                self.cancel(job.job_id)
        self._dispatcher.join(timeout=2)
        deadline = time.monotonic() + sum(self.termination_grace) + 6
        for job in jobs:
            if job.worker and job.worker is not threading.current_thread():
                job.worker.join(timeout=max(0, deadline - time.monotonic()))
        with self._condition:
            failed = [
                job.job_id
                for job in jobs
                if job.state not in TERMINAL_STATES
                or job.state == "stop_failed"
                or job.callback_error
            ]
            return {
                "closed": True,
                "observed_tree_stopped": not failed,
                "failed_jobs": failed,
                "isolation_complete": False,
            }

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _linux_subreaper():
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            return False
        value = ctypes.c_int()
        return libc.prctl(37, ctypes.byref(value), 0, 0, 0) == 0 and value.value == 1
    except (OSError, AttributeError):
        return False


def _process_table():
    """Trusted host observation, never invoke tools from the project PATH."""
    if sys.platform == "linux":
        result = {}
        for item in Path("/proc").iterdir():
            if not item.name.isdigit():
                continue
            try:
                raw = (item / "stat").read_text()
                parts = raw[raw.rfind(")") + 2 :].split()
                result[int(item.name)] = (int(parts[1]), int(parts[2]), parts[19], parts[0])
            except (OSError, ValueError, IndexError):
                continue
            if len(result) > 16384:
                raise ProcessError("EXECUTION_PROCESS_OBSERVATION_LIMIT")
        return result
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        close_fds=True,
    )
    if len(completed.stdout) > 4 * 1024 * 1024:
        raise ProcessError("EXECUTION_PROCESS_OBSERVATION_LIMIT")
    result = {}
    for line in completed.stdout.decode("ascii", "replace").splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        result[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[4], parts[3])
    return result


class _ObservedTree:
    def __init__(self, child, subreaper):
        self.child, self.subreaper = child, subreaper
        self.known = {}
        self.failed = False
        self.escaped = False

    def scan(self):
        table = _process_table()
        roots = {self.child.pid}
        if self.subreaper:
            roots.add(os.getpid())
        changed = True
        while changed:
            changed = False
            for pid, row in table.items():
                if pid not in roots and row[0] in roots:
                    roots.add(pid)
                    changed = True
        for pid, row in table.items():
            if pid == os.getpid():
                continue
            if pid in roots or row[1] == self.child.pid:
                previous = self.known.get(pid)
                if previous is not None and previous != row[2]:
                    self.failed = True
                    continue
                self.known[pid] = row[2]
                if pid != self.child.pid and row[1] != self.child.pid:
                    self.escaped = True
        live = {}
        for pid, identity in self.known.items():
            row = table.get(pid)
            if row is not None and row[2] == identity:
                if row[3].startswith("Z"):
                    if self.subreaper and pid != self.child.pid and row[0] == os.getpid():
                        try:
                            os.waitpid(pid, os.WNOHANG)
                        except (OSError, ChildProcessError):
                            pass
                else:
                    live[pid] = row
        return live

    def send(self, requested_signal):
        live = self.scan()
        if any(row[1] == self.child.pid for row in live.values()):
            try:
                os.killpg(self.child.pid, requested_signal)
            except ProcessLookupError:
                pass
        for pid, row in live.items():
            if row[1] == self.child.pid:
                continue
            # Recheck identity immediately before signaling a tracked escaped
            # descendant. Linux pidfds additionally pin the target process.
            if sys.platform == "linux" and hasattr(os, "pidfd_open"):
                fd = None
                try:
                    fd = os.pidfd_open(pid)
                    if _process_table().get(pid, (None, None, None))[2] == row[2]:
                        signal.pidfd_send_signal(fd, requested_signal)
                except ProcessLookupError:
                    pass
                finally:
                    if fd is not None:
                        os.close(fd)
            elif _process_table().get(pid, (None, None, None))[2] == row[2]:
                try:
                    os.kill(pid, requested_signal)
                except ProcessLookupError:
                    pass

    def stop(self, grace):
        try:
            for requested_signal, seconds in zip(
                (signal.SIGINT, signal.SIGTERM, signal.SIGKILL), grace, strict=True
            ):
                self.send(requested_signal)
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    self.child.poll()
                    if not self.scan():
                        return not self.failed
                    if requested_signal == signal.SIGKILL:
                        self.send(signal.SIGKILL)
                    time.sleep(0.025)
            self.child.poll()
            return not self.scan() and not self.failed
        except (OSError, ValueError, subprocess.SubprocessError, ProcessError):
            self.failed = True
            return False


def _linux_metrics(live):
    resident = 0
    cpu = {}
    ticks = os.sysconf("SC_CLK_TCK")
    if ticks <= 0:
        raise ProcessError("EXECUTION_CPU_OBSERVATION_UNAVAILABLE")
    for pid, row in live.items():
        try:
            raw_stat = Path(f"/proc/{pid}/stat").read_text()
            parts = raw_stat[raw_stat.rfind(")") + 2 :].split()
            if parts[19] != row[2]:
                continue
            raw = Path(f"/proc/{pid}/status").read_text()
            if _process_table().get(pid, (None, None, None))[2] != row[2]:
                continue
            cpu[(pid, row[2])] = (int(parts[11]) + int(parts[12])) * 1_000_000_000 // ticks
            for line in raw.splitlines():
                if line.startswith("VmRSS:"):
                    resident += int(line.split()[1]) * 1024
                    break
        except FileNotFoundError:
            continue
        except (ValueError, IndexError):
            raise ProcessError("EXECUTION_CPU_OBSERVATION_UNAVAILABLE") from None
    return {"rss_bytes": resident, "processes": len(live), "cpu_nanoseconds": cpu}


class _CpuWindow:
    """A bounded rolling average of observed per-birth CPU counter increments."""

    def __init__(self, threshold_cores, seconds):
        self.threshold, self.seconds = threshold_cores, seconds
        self.first = self.last = None
        self.previous = {}
        self.intervals = deque()
        self.observed_seconds = self.peak_cores = 0.0

    def sample(self, now, counters):
        if self.last is None:
            self.first = self.last = now
            self.previous = counters
            return False
        width = now - self.last
        if width <= 0:
            raise ProcessError("EXECUTION_CPU_OBSERVATION_UNAVAILABLE")
        delta = sum(
            max(0, value - self.previous.get(identity, 0)) for identity, value in counters.items()
        )
        seconds = delta / 1_000_000_000
        self.observed_seconds += seconds
        self.intervals.append((self.last, now, seconds))
        self.previous, self.last = counters, now
        cutoff = now - self.seconds
        while self.intervals and self.intervals[0][1] <= cutoff:
            self.intervals.popleft()
        if len(self.intervals) > 4096:
            raise ProcessError("EXECUTION_CPU_OBSERVATION_LIMIT")
        total = 0.0
        for start, end, value in self.intervals:
            total += value * (end - max(start, cutoff)) / (end - start)
        window = min(self.seconds, now - self.first)
        cores = total / window
        self.peak_cores = max(self.peak_cores, cores)
        return now - self.first >= self.seconds and cores > self.threshold


def _supervise(input_stream=None, output_stream=None, native_scope=None, output_socket=None):
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    writer_lock = threading.Lock()
    stop = threading.Event()
    reason = [None]
    touched = [time.monotonic()]
    transport_failed = [False]
    output_fd = output_stream.fileno()
    if output_socket is None:
        # The helper owns this pipe's write descriptor, distinct from control
        # input. TCP uses per-call MSG_DONTWAIT to keep control reads blocking.
        os.set_blocking(output_fd, False)

    def emit(value):
        raw = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        if transport_failed[0]:
            return
        if not writer_lock.acquire(timeout=0.6):
            transport_failed[0] = True
            reason[0] = "interrupted"
            stop.set()
            return
        try:
            if transport_failed[0]:
                return
            try:
                deadline = time.monotonic() + 0.5
                pending = memoryview(raw)
                while pending:
                    try:
                        written = (
                            output_socket.send(pending, socket.MSG_DONTWAIT)
                            if output_socket is not None
                            else os.write(output_fd, pending)
                        )
                        if not written:
                            raise OSError from None
                        pending = pending[written:]
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise OSError from None
                        select.select([], [output_fd], [], min(0.02, remaining))
            except (OSError, ValueError):
                transport_failed[0] = True
                reason[0] = "interrupted"
                stop.set()
        finally:
            writer_lock.release()

    raw = input_stream.readline(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES or not raw.endswith(b"\n"):
        return 2
    try:
        config = json.loads(raw)
        raw = None
        payload = (
            base64.b64decode(config["stdin_payload"], validate=True)
            if config.get("stdin_payload") is not None
            else None
        )
        config["stdin_payload"] = None
        if payload is not None and len(payload) > 64 * 1024:
            raise ValueError
        subreaper = _linux_subreaper()
        master = slave = None
        tty = config.get("tty", False)
        interactive = config.get("interactive", False)
        if tty:
            import fcntl
            import pty
            import termios

            master, slave = pty.openpty()
            # Suppress input echo by default. Do not reflect entered credentials into logs.
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave, termios.TCSANOW, attributes)

        def attach_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            child = subprocess.Popen(
                config["argv"],
                cwd=config["cwd"],
                env=config["env"],
                stdin=slave
                if tty
                else subprocess.PIPE
                if interactive or payload is not None
                else subprocess.DEVNULL,
                stdout=slave if tty else subprocess.PIPE,
                stderr=slave if tty else subprocess.PIPE,
                start_new_session=not tty,
                preexec_fn=attach_terminal if tty else None,
                close_fds=True,
            )
            if tty:
                child.stdin = os.fdopen(os.dup(master), "wb", buffering=0)
                child.stdout = os.fdopen(master, "rb", buffering=0)
                master = None
            if interactive:
                os.set_blocking(child.stdin.fileno(), False)
        finally:
            if slave is not None:
                os.close(slave)
            if master is not None:
                os.close(master)
    except (OSError, ValueError, TypeError, KeyError):
        emit(
            {
                "kind": "exit",
                "state": "failed",
                "reason": "command_start_failed",
                "exit_code": None,
                "observed_tree_stopped": True,
                "cleanup_verified": True,
                "isolation_complete": False,
                "tree_scope": "not_started",
            }
        )
        return 1
    scope = (
        "macos_resource_coalition"
        if native_scope is not None
        else "linux_subreaper"
        if subreaper
        else "observed_process_tree"
    )
    tree = native_scope if native_scope is not None else _ObservedTree(child, subreaper)
    if native_scope is not None:
        native_scope.reap = child.poll
    streams_done = {name: threading.Event() for name in ("stdout", "stderr")}

    def pump(name, stream):
        try:
            while True:
                # PTY read/write duplicates share O_NONBLOCK; wait instead of treating
                # an idle prompt as a failed process.
                if tty and not select.select([stream], [], [], 0.1)[0]:
                    continue
                try:
                    data = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not data:
                    break
                emit(
                    {
                        "kind": "output",
                        "stream": name,
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                )
        except OSError as exc:
            # PTY masters report EIO after the slave closes normally.
            if not (tty and exc.errno == 5):
                reason[0] = reason[0] or "interrupted"
                stop.set()
        finally:
            stream.close()
            streams_done[name].set()

    def control():
        try:
            while True:
                raw = input_stream.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    reason[0] = reason[0] or "interrupted"
                    stop.set()
                    return
                if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                    raise ValueError
                frame = json.loads(raw)
                action = frame.get("action")
                if action == "touch":
                    touched[0] = time.monotonic()
                elif action == "input" and interactive:
                    data = base64.b64decode(frame.get("data", ""), validate=True)
                    if len(data) > 16 * 1024 or type(frame.get("eof")) is not bool:
                        raise ValueError
                    deadline = time.monotonic() + 2
                    while data:
                        if time.monotonic() >= deadline:
                            raise ValueError
                        if select.select([], [child.stdin], [], 0.05)[1]:
                            try:
                                data = data[os.write(child.stdin.fileno(), data) :]
                            except BlockingIOError:
                                continue
                    if frame["eof"]:
                        if tty:
                            raise ValueError
                        child.stdin.close()
                    touched[0] = time.monotonic()
                elif action == "cancel":
                    reason[0] = "cancelled"
                    stop.set()
                else:
                    raise ValueError
        except (OSError, ValueError, AttributeError):
            reason[0] = reason[0] or "interrupted"
            stop.set()

    def input_payload():
        try:
            child.stdin.write(payload)
            child.stdin.flush()
        except (OSError, ValueError):
            reason[0] = reason[0] or "command_input_failed"
            stop.set()
        finally:
            child.stdin.close()

    for name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
        if stream is None:
            streams_done[name].set()
        else:
            threading.Thread(target=pump, args=(name, stream), daemon=True).start()
    threading.Thread(target=control, daemon=True).start()
    if payload is not None:
        threading.Thread(target=input_payload, daemon=True).start()
    emit(
        {
            "kind": "started",
            "pid": child.pid,
            "tree_scope": scope,
            "resource_id": native_scope.resource_id if native_scope is not None else None,
        }
    )
    started, quiet_scans = time.monotonic(), 0
    leader_exited = None
    observed_stopped = False
    resource_reason = None
    observation_error_code = None
    peak_rss = peak_processes = 0
    sampled = 0.0
    cpu = _CpuWindow(
        config["resource_limits"].get("max_cpu_cores", 4.0),
        config["resource_limits"].get("cpu_window_seconds", 30.0),
    )
    try:
        while True:
            child.poll()
            live = tree.scan()
            now = time.monotonic()
            if now - sampled >= config["resource_limits"]["sample_seconds"]:
                metrics = (
                    tree.kernel.metrics(live) if native_scope is not None else _linux_metrics(live)
                )
                peak_rss = max(peak_rss, metrics["rss_bytes"])
                peak_processes = max(peak_processes, metrics["processes"])
                cpu_exceeded = cpu.sample(now, metrics["cpu_nanoseconds"])
                emit(
                    {
                        "kind": "resources",
                        "resource_peak_rss_bytes": peak_rss,
                        "resource_peak_processes": peak_processes,
                        "resource_peak_cpu_cores": cpu.peak_cores,
                        "resource_cpu_seconds_observed": cpu.observed_seconds,
                    }
                )
                sampled = now
                limits = config["resource_limits"]
                if metrics["rss_bytes"] > limits["max_rss_bytes"]:
                    resource_reason = "rss_limit"
                elif metrics["processes"] > limits["max_processes"]:
                    resource_reason = "process_limit"
                elif cpu_exceeded:
                    resource_reason = "cpu_limit"
                if resource_reason:
                    reason[0] = "resource_limit"
                    stop.set()
            expired = now - (touched[0] if config["service"] else started) >= (
                config["idle"] if config["service"] else config["timeout"]
            )
            if stop.is_set() or expired:
                reason[0] = reason[0] or "timeout"
                observed_stopped = tree.stop(config["grace"])
                break
            if child.returncode is not None:
                leader_exited = now if leader_exited is None else leader_exited
                if live or (
                    now - leader_exited >= 0.25
                    and not all(event.is_set() for event in streams_done.values())
                ):
                    reason[0] = "descendants_after_exit"
                    observed_stopped = tree.stop(config["grace"])
                    break
                if not all(event.is_set() for event in streams_done.values()):
                    time.sleep(0.025)
                    continue
                quiet_scans += 1
                if quiet_scans >= 2:
                    observed_stopped = not tree.failed
                    break
            time.sleep(min(0.025, config["resource_limits"]["sample_seconds"]))
    except (OSError, ValueError, subprocess.SubprocessError, ProcessError) as error:
        # Retain only our content-free enum/ABI codes, never arbitrary exception
        # text, file bodies, command arguments or connection environment values.
        code = error.args[0] if error.args else None
        if (
            isinstance(code, str)
            and len(code) <= 80
            and code.startswith(("EXECUTION_SCOPE_", "EXECUTION_CPU_", "EXECUTION_PROCESS_"))
            and all(char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for char in code)
        ):
            observation_error_code = code
        else:
            observation_error_code = "PROCESS_OBSERVATION_FAILED"
        reason[0] = "process_observation_failed"
        tree.stop(config["grace"])
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        observed_stopped = False
    if interactive and child.stdin and not child.stdin.closed:
        child.stdin.close()
    for event in streams_done.values():
        if not event.wait(1):
            observed_stopped = False
    state = (
        reason[0]
        if reason[0] in {"cancelled", "timeout", "interrupted", "resource_limit"}
        else "exited"
    )
    if not observed_stopped or reason[0] in {
        "descendants_after_exit",
        "process_observation_failed",
        "command_input_failed",
    }:
        state = "stop_failed"
    emit(
        {
            "kind": "exit",
            "state": state,
            "exit_code": child.returncode,
            "reason": resource_reason or reason[0],
            "observation_error_code": observation_error_code,
            "observed_tree_stopped": observed_stopped,
            "cleanup_verified": observed_stopped and (subreaper or native_scope is not None),
            "isolation_complete": False,
            "tree_scope": scope,
            "resource_id": native_scope.resource_id if native_scope is not None else None,
            "resource_peak_rss_bytes": peak_rss,
            "resource_peak_processes": peak_processes,
            "resource_peak_cpu_cores": cpu.peak_cores,
            "resource_cpu_seconds_observed": cpu.observed_seconds,
        }
    )
    return 0 if observed_stopped else 1


def _launchd_main(manifest):
    # The isolated, site-free helper needs only the adjacent stdlib-only scope
    # module. Never import project packages or a project-controlled PATH.
    spec = importlib.util.spec_from_file_location(
        "colink_execution_scope", Path(__file__).with_name("execution_scope.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    scope, connection, reader, writer, target = module.helper_connection(manifest)
    try:
        return _supervise(reader, writer, scope, connection)
    finally:
        scope.stop((0.1, 0.1, 1))
        # BufferedReader.close waits for a concurrent readline's lock. Wake the
        # control reader first even when the living parent kept its writer open.
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        reader.close()
        writer.close()
        connection.close()
        module.helper_bootout(target)


if __name__ == "__main__":
    if sys.argv[1:] == ["--supervise"]:
        # Plain Darwin observation is never an execution fallback.
        raise SystemExit(2 if sys.platform == "darwin" else _supervise())
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise-launchd":
        raise SystemExit(_launchd_main(sys.argv[2]))
    raise SystemExit(2)

"""Web-request-independent execution plans, local grants and durable job receipts."""

import hashlib
import json
import os
import secrets
import socket
import threading
import time
import urllib.request
from pathlib import Path

from code_context.execution_cache import ExecutionCache
from code_context.execution_defaults import DEFAULT_DEVELOPMENT_PORTS as DEFAULT_PORTS
from code_context.execution_disk_recovery import TaskDiskRecovery
from code_context.execution_process import ProcessError, ProcessManager
from code_context.execution_relay import LocalRelay
from code_context.execution_sandbox import BoundedDisk, NativeSandbox, export_input
from code_context.execution_scope import (
    expire_verified_job_scope,
    index_retired_job_scopes,
    verify_retired_job_scope,
)
from code_context.execution_store import TERMINAL_STATES, ExecutionStore
from code_context.local_control import private_directory
from code_context.policy import SECRET_PATTERNS, content_problem, validate_path
from code_context.source_access import SourceAccess, SourceError

PUBLIC_DOMAINS = (
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "repo.maven.apache.org",
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def identifier(prefix):
    return prefix + secrets.token_hex(16)


class ExecutionCoordinator:
    def __init__(
        self,
        root,
        source_for,
        write_coordinator,
        *,
        control_alive,
        protected_paths=(),
        gate=None,
        databases=None,
    ):
        self.state = private_directory(root)
        self.source_for, self.write = source_for, write_coordinator
        self.control_alive = control_alive
        self.databases = databases
        self.gate = gate or (lambda: False)
        self.lock = threading.RLock()
        self.grants, self.tasks, self.live = {}, {}, {}
        self.epoch = identifier("epoch-")
        self.store = ExecutionStore(self.state.root / "ledger")
        self.store.interrupt_unfinished()
        self.store.collect_expired(self.store.expired_job_candidates(limit=1000), limit=1000)
        scope_index = []

        def cleanup_proof(record, job):
            if not scope_index:
                scope_index.append(index_retired_job_scopes(self.state.root / "scope"))
            return verify_retired_job_scope(
                self.state.root / "scope", job["job_id"], index=scope_index[0]
            )

        self.disk_recovery = TaskDiskRecovery(
            self.state.root / "jobs",
            self.source_for,
            self.store.get_job,
            cleanup_proof=cleanup_proof,
            scope_expire=lambda record: expire_verified_job_scope(
                self.state.root / "scope", record["job_id"]
            ),
        )
        self.recovery_result = self.disk_recovery.recover()
        proofs = self.recovery_result.get("cleanup_proofs", {})
        for job_id in set(self.recovery_result.get("reclaimed", ())) | set(proofs):
            job = self.store.get_job(job_id)
            if job and job["state"] in TERMINAL_STATES:
                snapshot = {**job["snapshot"], "workspace_retired": True}
                if job_id in proofs and job["state"] == "interrupted":
                    snapshot.update(
                        cleanup_verified=True,
                        tree_scope="macos_resource_coalition",
                        resource_id=proofs[job_id]["resource_id"],
                        cleanup_origin="verified_retired_resource_scope",
                    )
                if snapshot.get("writeback", {}).get("state") == "awaiting_start":
                    snapshot["writeback"] = {"state": "unavailable", "reason": "runtime_restarted"}
                self.store.update_completed_snapshot(job_id, snapshot)
        self.sandbox = NativeSandbox(
            self.state.root / "helper",
            [
                *protected_paths,
                self.state.root / "scope",
                self.state.root / "ledger",
                self.state.root / "ports",
                self.state.root / "cache/cache.json",
                self.state.root / "cache/cache.sparsebundle",
            ],
        )
        for job_id in self.recovery_result.get("reclaimed", ()):
            try:
                self.sandbox.cleanup_job(job_id, verified=True)
            except SourceError:
                self.recovery_result["state"] = "blocked"
                self.recovery_result["blocked"].append(
                    {"job_id": job_id, "reason": "EXECUTION_HELPER_RETIREMENT_UNVERIFIED"}
                )
        self.cache = ExecutionCache(self.state.root / "cache")
        self.manager = ProcessManager(
            sandbox_check=self._check_launch, scope_root=self.state.root / "scope"
        )
        self.preparers = set()
        self.closed = False

    def _check_launch(self, argv, cwd, env):
        if not self.control_alive() or not self.grants or not self.gate():
            return False
        job_id = Path(argv[2]).stem if len(argv) == 3 else ""
        job = self.store.get_job(job_id) if job_id else None
        if (
            not job
            or job["epoch"] != self.epoch
            or job["project_id"] not in self.grants
            or job["source_id"] != self.grants[job["project_id"]]["source_id"]
        ):
            return False
        self.source_for(job["project_id"]).ensure_available()
        return self.sandbox.toolchain_matches(job["metadata"]["toolchain"]) and self.sandbox.check(
            argv, cwd, env
        )

    def enable(self, project_ids, *, ports=DEFAULT_PORTS, domains=PUBLIC_DOMAINS):
        if (
            not self.control_alive()
            or not self.sandbox.available()
            or not self.gate()
            or self.recovery_result["state"] != "ready"
            or not isinstance(project_ids, list)
            or not 1 <= len(project_ids) <= 64
            or len(set(project_ids)) != len(project_ids)
            or not isinstance(ports, (list, tuple))
            or len(set(ports)) > 16
            or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports)
            or tuple(domains) != PUBLIC_DOMAINS
        ):
            raise SourceError(
                "EXECUTION_GATE_CLOSED: native protection must pass before local authorization"
            )
        if self.databases and set(ports) & self.databases.raw_service_ports():
            raise SourceError("DATABASE_SERVICE_PORT_PROTECTED: project jobs use target proxies")
        self.disable()
        with self.lock:
            self.epoch = identifier("epoch-")
            self.grants = {
                pid: {
                    "source_id": self.source_for(pid).source_id,
                    "epoch": self.epoch,
                    "ports": tuple(ports),
                    "domains": tuple(domains),
                }
                for pid in project_ids
            }
        return self.status()

    def disable(self):
        with self.lock:
            self.grants = {}
            self.epoch = identifier("epoch-")
            active = tuple(self.live.items())
        # Cancellation is asynchronous and supervision continues after this call.
        for job_id, live in active:
            live["cancel_requested"] = True
            if not live.get("preparing"):
                job = self.store.get_job(job_id)
                if job and job["state"] not in TERMINAL_STATES:
                    try:
                        self.manager.cancel(job_id)
                    except ProcessError as exc:
                        completed = self.store.get_job(job_id)
                        if (
                            str(exc) != "EXECUTION_JOB_UNKNOWN"
                            or not completed
                            or completed["state"] not in TERMINAL_STATES
                        ):
                            raise

    def authorize(self, project_id):
        source = self.source_for(project_id)
        source.ensure_available()
        grant = self.grants.get(project_id)
        if (
            not self.control_alive()
            or not self.gate()
            or not grant
            or grant["source_id"] != source.source_id
        ):
            raise SourceError("EXECUTION_DISABLED: authorize this project in the local app")
        return dict(grant)

    def status(self):
        return {
            "execution_available": self.sandbox.available()
            and self.gate()
            and self.control_alive()
            and self.recovery_result["state"] == "ready",
            "execution_enabled": bool(self.grants) and self.control_alive(),
            "execution_projects": list(self.grants),
            "execution_gate": "passed" if self.gate() else "closed",
            "execution_jobs": [{"job_id": jid, **self._snapshot(jid)} for jid in tuple(self.live)],
            "execution_limits": {
                "services": 2,
                "finite_tasks": 1,
                "finite_seconds": 300,
                "service_idle_seconds": 1200,
                "task_rounds": 5,
                "task_seconds": 1200,
                "workspace_bytes": 2 * 1024**3,
                "output_job_bytes": 8 * 1024**2,
                "output_global_bytes": 64 * 1024**2,
                "cpu_memory_process_limits": "soft monitoring",
                "rss_bytes": 2 * 1024**3,
                "processes": 128,
                "cpu_cores": 4,
                "cpu_window_seconds": 30,
                "cpu_limit": "soft sampled average; short exited processes may be missed",
            },
            "execution_cache": self.cache.status(),
            "execution_recovery": self.recovery_result,
        }

    def plan(
        self,
        project_id,
        request_id,
        command,
        *,
        cwd="",
        operation="test",
        service=False,
        ports=None,
        connect_ports=None,
        network="none",
        development_task_id=None,
        writeback_paths=None,
        write_task_id=None,
        health_path=None,
        database=False,
        database_target_id=None,
        database_action=None,
    ):
        grant = self.authorize(project_id)
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 128
            or any(
                not isinstance(x, str) or not x or len(x.encode()) > 16384 or "\x00" in x
                for x in command
            )
            or sum(len(x.encode()) for x in command) > 32768
            or operation not in {"install", "test", "build", "serve", "generate", "shell"}
            or type(service) is not bool
            or type(database) is not bool
            or database_action not in {None, "create", "drop"}
            or database_action is not None
            and (not database or len(command) != 1)
            or network not in {"none", "packages"}
            or any(p.search(" ".join(command)) for p in SECRET_PATTERNS)
        ):
            raise SourceError(
                "INVALID_EXECUTION_PLAN: explicit bounded development command required"
            )
        if cwd:
            validate_path(cwd)
        ports, connect_ports, writeback_paths = (
            ports or [],
            connect_ports or [],
            writeback_paths or [],
        )
        if (
            not isinstance(ports, list)
            or not isinstance(connect_ports, list)
            or len(ports) > 2
            or len(connect_ports) > 4
            or any(type(p) is not int or p not in grant["ports"] for p in ports + connect_ports)
            or bool(ports)
            and not service
            or len(writeback_paths) > 100
        ):
            raise SourceError(
                "EXECUTION_SCOPE_EXPANSION: replan within locally authorized ports and paths"
            )
        if self.databases and set(ports + connect_ports) & self.databases.raw_service_ports():
            raise SourceError("DATABASE_SERVICE_PORT_PROTECTED: use the selected target proxy")
        for path in writeback_paths:
            validate_path(path)
            if cwd and not path.startswith(cwd + "/"):
                raise SourceError("GENERATED_RESULT_OUTSIDE_INPUT")
        if writeback_paths and not write_task_id:
            raise SourceError(
                "WRITE_TASK_REQUIRED: generated source must use an authorized write task"
            )
        if health_path is not None and (
            not isinstance(health_path, str)
            or not health_path.startswith("/")
            or len(health_path) > 256
            or "\r" in health_path
            or "\n" in health_path
        ):
            raise SourceError("INVALID_HEALTH_PATH")
        executable = command[0]
        if executable == "shell":
            if len(command) != 2:
                raise SourceError("INVALID_DEVELOPMENT_SHELL")
            argv = ["/bin/bash", "--noprofile", "--norc", "-c", command[1]]
        elif executable in self.sandbox.tools:
            argv = [self.sandbox.tools[executable], *command[1:]]
            if executable == "npm":
                argv.insert(0, self.sandbox.tools["node"])
        elif executable.startswith(("/usr/bin/", "/bin/")) and Path(executable).is_file():
            argv = command
        else:
            raise SourceError(
                "TOOLCHAIN_UNAVAILABLE: use an installed Python/npm/Maven tool or sandbox shell"
            )
        if database_action is not None:
            if self.databases and self.databases.scope_enforcement:
                raise SourceError(
                    "DATABASE_NATIVE_PROVISION_REQUIRED: approve the exact target locally"
                )
            if not self.databases:
                raise SourceError("DATABASE_NOT_CONFIGURED")
            argv += self.databases.administrative_argv(
                project_id, database_target_id, database_action, command[0]
            )
        metadata = {
            "command": command,
            "argv": argv,
            "cwd": cwd,
            "operation": operation,
            "service": service,
            "ports": ports,
            "connect_ports": connect_ports,
            "network": network,
            "development_task_id": development_task_id,
            "writeback_paths": writeback_paths,
            "write_task_id": write_task_id,
            "health_path": health_path,
            "database": database,
            "database_target_id": database_target_id,
            "database_action": database_action,
            "toolchain": self.sandbox.fingerprint(),
            "requires_rehearsal": operation in {"install", "generate"},
        }
        # Bind a source fingerprint without persisting bodies or mounting a disk.
        source = self.source_for(project_id)
        inputs = self._export(source, None, relative=cwd)
        entries = [(path, info["sha256"]) for path, info in inputs["files"].items()]
        metadata["source_digest"] = inputs["sha256"]
        key_files = [
            (path, sha)
            for path, sha in entries
            if Path(path).name
            in {
                "package-lock.json",
                "package.json",
                "pyproject.toml",
                "uv.lock",
                "requirements.txt",
                "pom.xml",
                "settings.xml",
                ".npmrc",
                ".mvn",
                "tsconfig.json",
                "vite.config.ts",
                "vite.config.js",
            }
        ]
        metadata["cache_key"] = digest([metadata["toolchain"], key_files, os.uname().machine])
        if database:
            if not self.databases:
                raise SourceError("DATABASE_NOT_CONFIGURED")
            profile = self.databases.validate_execution_target(
                project_id, database_target_id, database_action
            )
            metadata["database_digest"] = digest(profile)
            metadata["database_instance_identity"] = self.databases.grants.get(
                database_target_id, {}
            ).get("instance_identity")
        # Request identity uses explicit inputs; generated task IDs must never
        # turn an unchanged browser retry into a different request.
        binding = digest(metadata)
        with self.lock:
            self.collect_expired()
            receipt = self.store.get_receipt(
                project_id, grant["source_id"], self.epoch, request_id, binding
            )
            if receipt:
                if receipt["expired"] or receipt["kind"] != "plan":
                    raise SourceError("EXECUTION_REQUEST_EXPIRED")
                plan = self._plan(project_id, receipt["target_id"])
                metadata = plan["metadata"]
                development_task_id = metadata["development_task_id"]
            else:
                now = time.monotonic()
                self.tasks = {
                    key: value
                    for key, value in self.tasks.items()
                    if value["epoch"] == self.epoch and now - value["created"] < 1200
                }
                if development_task_id:
                    task = self.tasks.get(development_task_id)
                    if not task or (task["project_id"], task["epoch"]) != (project_id, self.epoch):
                        raise SourceError("DEVELOPMENT_TASK_UNAVAILABLE")
                    if task["rounds"] >= 5:
                        raise SourceError("DEVELOPMENT_TASK_BUDGET_EXHAUSTED")
                else:
                    if len(self.tasks) >= 128:
                        raise SourceError("DEVELOPMENT_TASK_CAPACITY")
                    development_task_id = identifier("dev-")
                    self.tasks[development_task_id] = {
                        "project_id": project_id,
                        "epoch": self.epoch,
                        "rounds": 0,
                        "created": now,
                    }
                metadata["development_task_id"] = development_task_id
                plan = self.store.create_plan(
                    identifier("plan-"),
                    project_id,
                    grant["source_id"],
                    self.epoch,
                    request_id,
                    binding,
                    metadata,
                )
        return {
            "plan_id": plan["plan_id"],
            "project_id": project_id,
            "development_task_id": development_task_id,
            "requires_rehearsal": metadata["requires_rehearsal"],
            "command": command,
            "ports": ports,
            "network": network,
            "expires_at": plan["expires_at"],
            "limits": self.status()["execution_limits"],
            "input_digest": metadata["source_digest"],
        }

    def _plan(self, project_id, plan_id, *, allow_expired=False):
        grant = self.authorize(project_id)
        plan = self.store.get_plan(plan_id)
        if (
            not plan
            or plan["expired"]
            and not allow_expired
            or (plan["project_id"], plan["source_id"], plan["epoch"])
            != (project_id, grant["source_id"], self.epoch)
        ):
            raise SourceError(
                "EXECUTION_PLAN_UNAVAILABLE: replan after source or authorization changes"
            )
        if not self.sandbox.toolchain_matches(plan["metadata"]["toolchain"]):
            raise SourceError("EXECUTION_TOOLCHAIN_CHANGED")
        return plan

    def _check_inputs(self, project_id, meta):
        source = self.source_for(project_id)
        if self._export(source, None, relative=meta["cwd"])["sha256"] != meta["source_digest"]:
            raise SourceError("EXECUTION_INPUT_CHANGED: read source and obtain a new plan")
        if meta["database"]:
            if not self.databases:
                raise SourceError("DATABASE_NOT_CONFIGURED")
            profile = self.databases.validate_execution_target(
                project_id, meta.get("database_target_id"), meta.get("database_action")
            )
            if digest(profile) != meta["database_digest"]:
                raise SourceError("DATABASE_PROFILE_CHANGED: obtain a new execution plan")
        return source

    def _export(self, source, destination, *, relative):
        return export_input(
            source,
            destination,
            relative=relative,
            protected_paths=[self.state.root, *getattr(self.sandbox, "protected_paths", ())],
        )

    def _snapshot(self, job_id):
        live = self.live.get(job_id)
        if live and live.get("preparing"):
            return {
                "state": "preparing",
                "service": live["metadata"]["service"],
                "cancel_requested": live.get("cancel_requested", False),
                "cleanup_verified": False,
            }
        job = self.store.get_job(job_id)
        if job and job["state"] in TERMINAL_STATES:
            return job["snapshot"] or {"state": job["state"]}
        try:
            return self.manager.snapshot(job_id)
        except ProcessError as exc:
            completed = self.store.get_job(job_id)
            if (
                str(exc) != "EXECUTION_JOB_UNKNOWN"
                or not completed
                or completed["state"] not in TERMINAL_STATES
            ):
                raise
            return completed["snapshot"] or {"state": completed["state"]}

    def rehearse(self, project_id, plan_id, request_id):
        return self._start(project_id, plan_id, request_id, rehearsal=True)

    def start(self, project_id, plan_id, request_id):
        return self._start(project_id, plan_id, request_id, rehearsal=False)

    def _start(self, project_id, plan_id, request_id, *, rehearsal):
        deadline = time.monotonic() + 2
        with self.lock:
            plan = self._plan(project_id, plan_id, allow_expired=True)
            meta = plan["metadata"]
            binding = digest({"plan": plan_id, "digest": plan["digest"], "rehearsal": rehearsal})
            receipt = self.store.get_receipt(
                project_id, plan["source_id"], self.epoch, request_id, binding
            )
            if receipt:
                if receipt["expired"]:
                    raise SourceError("EXECUTION_REQUEST_EXPIRED")
                return {**self.job_status(project_id, receipt["target_id"]), "duplicate": True}
            if plan["expired"]:
                raise SourceError("EXECUTION_PLAN_UNAVAILABLE: obtain a new execution plan")
            if plan["consumed_by"]:
                # A risk-triggered rehearsal is the same real isolated execution;
                # reuse its result instead of installing or generating twice.
                job_id = plan["consumed_by"]
                completed = self.store.get_job(job_id)
                writeback = completed["snapshot"].get("writeback", {})
                if writeback.get("state") != "applied":
                    self._check_inputs(project_id, meta)
                self.store.record_job_receipt(
                    job_id, project_id, plan["source_id"], self.epoch, request_id, binding
                )
                if not rehearsal and meta["writeback_paths"]:
                    self._apply_generated_result(job_id)
                return {**self.job_status(project_id, plan["consumed_by"]), "duplicate": True}
            if meta["requires_rehearsal"] and not rehearsal:
                raise SourceError("EXECUTION_REHEARSAL_REQUIRED")
            task = self.tasks.get(meta["development_task_id"])
            if not task:
                raise SourceError("DEVELOPMENT_TASK_UNAVAILABLE")
            if task["rounds"] >= 5 or time.monotonic() - task["created"] >= 1200:
                raise SourceError("DEVELOPMENT_TASK_BUDGET_EXHAUSTED")
            # Unverified cleanup and generated-result references also consume
            # the bounded working set, even after their processes have exited.
            if len(self.live) >= 8:
                raise SourceError("EXECUTION_QUEUE_FULL")
            source = self._check_inputs(project_id, meta)
            for port in meta["ports"]:
                with socket.socket() as test:
                    try:
                        test.bind(("127.0.0.1", port))
                    except OSError:
                        raise SourceError(
                            "PORT_IN_USE: inspect port_status or replan with a registered port"
                        ) from None
            job_id = identifier("job-")
            job, replay = self.store.reserve_job(
                job_id,
                plan_id,
                project_id,
                source.source_id,
                self.epoch,
                request_id,
                binding,
                meta,
                service=meta["service"],
            )
            if replay:
                return {**self.job_status(project_id, job["job_id"]), "duplicate": True}
            self.live[job_id] = {
                "disk": None,
                "cache": None,
                "project_id": project_id,
                "metadata": meta,
                "rehearsal": rehearsal,
                "preparing": True,
                "cancel_requested": False,
                "ready": threading.Event(),
                "epoch": self.epoch,
            }
            live = self.live[job_id]
            ready = live["ready"]
            task["rounds"] += 1
            worker = threading.Thread(
                target=self._prepare_job,
                args=(job_id,),
                daemon=True,
                name="CoLink-prepare-" + job_id[-8:],
            )
            self.preparers.add(worker)
            worker.start()
        ready.wait(max(0, deadline - time.monotonic()))
        output = {"chunks": [], "next_cursor": 0, "eof": False, "state": "preparing"}
        if not live.get("preparing"):
            remaining = int(max(0, deadline - time.monotonic()) * 1000)
            output = self.output(project_id, job_id, wait_ms=remaining)
        return {**self.job_status(project_id, job_id), "initial_output": output, "duplicate": False}

    def _prepare_job(self, job_id):
        live = self.live[job_id]
        project_id, meta = live["project_id"], live["metadata"]
        disk, cache = None, None
        try:
            self.authorize(project_id)
            if live["cancel_requested"] or live["epoch"] != self.epoch:
                raise SourceError("EXECUTION_CANCELLED_BEFORE_START")
            source = self._check_inputs(project_id, meta)
            # A failed constructor can already own an image/mount through its
            # durable disk record. None is not evidence that no disk exists.
            live["disk_creation_started"] = True
            disk = live["disk"] = BoundedDisk(
                self.state.root / "jobs",
                job_id,
                project_id=project_id,
                source_id=source.source_id,
            )
            relay = None
            if meta["ports"]:
                relay = live["relay"] = LocalRelay(
                    meta["ports"], disk.mount, scope_for=lambda: self._relay_scope(job_id)
                )
            workspace = disk.mount / "workspace"
            baseline = self._export(source, workspace, relative=meta["cwd"])
            if baseline["sha256"] != meta["source_digest"]:
                raise SourceError("EXECUTION_INPUT_CHANGED: read source and obtain a new plan")
            cache = live["cache"] = self.cache.acquire(project_id, meta["cache_key"])
            self.cache.restore_workspace(
                project_id, meta["cache_key"], workspace, input_digest=meta["source_digest"]
            )
            db = (
                self.databases.job_configuration(
                    project_id,
                    meta["database_target_id"],
                    require_authorized=True,
                    database_action=meta.get("database_action"),
                )
                if meta["database"]
                else None
            )
            if db and db.get("proxy"):
                live["database_proxy"] = db["proxy"]
            argv, env = self.sandbox.prepare(
                job_id,
                disk,
                workspace,
                meta["argv"],
                domains=PUBLIC_DOMAINS if meta["network"] == "packages" else (),
                bind_ports=meta["ports"],
                connect_ports=meta["connect_ports"],
                cache_paths=[cache],
                database=db,
                socket_endpoints=relay.endpoints if relay else None,
            )
            with self.lock:
                self.authorize(project_id)
                if live["cancel_requested"] or live["epoch"] != self.epoch or self.closed:
                    raise SourceError("EXECUTION_CANCELLED_BEFORE_START")
                live.update(baseline=baseline, workspace=workspace)
                self.store.mark_started(job_id, {"state": "queued"})
                payload = self.sandbox.take_payload(job_id)
                disk.mark_started()
                self.manager.submit(
                    job_id,
                    argv,
                    str(workspace),
                    env,
                    service=meta["service"],
                    stdin_payload=payload,
                    on_exit=lambda snap: self._completed(job_id, snap),
                )
                live["preparing"] = False
        except BaseException as exc:
            reason = "preparation_failed"
            if isinstance(exc, SourceError):
                reason = str(exc).split(":", 1)[0]
            state = "cancelled" if live["cancel_requested"] else "failed"
            snapshot = {
                "state": state,
                "reason": reason,
                "cleanup_verified": True,
                "tree_scope": "not_started",
                "service": meta["service"],
                "result": state,
            }
            self.store.complete_job(job_id, snapshot)
            self.sandbox.discard_payload(job_id)
            live.update(preparing=False, prepare_failed=True)
            try:
                self._release_workspace(job_id)
            except (SourceError, OSError) as cleanup_error:
                # Keep every remaining reference and make the failure visible;
                # a failed helper/disk retirement never becomes GC evidence.
                snapshot["resource_release"] = {
                    "state": "blocked",
                    "reason": str(cleanup_error).split(":", 1)[0]
                    if isinstance(cleanup_error, SourceError)
                    else "EXECUTION_RESOURCE_RELEASE_FAILED",
                }
                self.store.update_completed_snapshot(job_id, snapshot)
        finally:
            live["ready"].set()
            self.preparers.discard(threading.current_thread())
            if not live.get("preparing") and not live.get("prepare_failed"):
                self._release_workspace(job_id)

    def _completed(self, job_id, snapshot):
        live = self.live[job_id]
        meta = live["metadata"]
        snapshot = dict(snapshot)
        if snapshot.get("cleanup_verified") and live.get("disk"):
            live["disk"].mark_cleanup(True)
        self._database_proof(job_id)
        snapshot["result"] = (
            "success"
            if snapshot.get("state") == "exited" and snapshot.get("exit_code") == 0
            else snapshot["state"]
        )
        if snapshot.get("cleanup_verified") and snapshot["result"] == "success":
            try:
                snapshot["cache"] = self.cache.capture_workspace(
                    live["project_id"],
                    meta["cache_key"],
                    live["workspace"],
                    input_digest=meta["source_digest"],
                )
            except SourceError as exc:
                snapshot["cache"] = {"state": "unavailable", "reason": str(exc).split(":", 1)[0]}
                snapshot["result"] = "cache_capacity_error"
        if meta["writeback_paths"]:
            snapshot["writeback"] = {
                "state": "awaiting_start"
                if snapshot["result"] == "success" and snapshot.get("cleanup_verified")
                else "blocked",
            }
            if snapshot["result"] == "success" and not snapshot.get("cleanup_verified"):
                snapshot["result"] = "cleanup_unverified"
        self.store.complete_job(job_id, snapshot)
        if meta["writeback_paths"] and not live["rehearsal"]:
            self._apply_generated_result(job_id)
        # Rehearsed generator outputs remain mounted only until explicit start
        # applies them, or the bounded unread-result retention expires.
        recorded = self.store.get_job(job_id)["snapshot"]
        if recorded.get("writeback", {}).get("state") != "awaiting_start":
            self._release_workspace(job_id)
        else:
            timer = threading.Timer(600, self._expire_generated_result, args=(job_id,))
            timer.daemon = True
            live["retention_timer"] = timer
            timer.start()

    def _relay_scope(self, job_id):
        try:
            snapshot = self.manager.snapshot(job_id)
            if snapshot["state"] not in {"starting", "running"}:
                return None
            return snapshot.get("resource_id")
        except (ValueError, KeyError):
            return None

    def _database_proof(self, job_id):
        live = self.live.get(job_id)
        if not live or not live["metadata"]["database"] or live.get("database_verified"):
            return
        proof = self.sandbox.database_proof(job_id)
        if proof:
            self.databases.record_proof(
                live["project_id"], live["metadata"]["database_digest"], job_id, proof
            )
            live["database_verified"] = True

    def _release_workspace(self, job_id):
        with self.lock:
            live = self.live.get(job_id)
            if not live or live.get("preparing"):
                return
            job = self.store.get_job(job_id)
            snapshot = job["snapshot"]
            if (
                job["state"] not in TERMINAL_STATES
                or snapshot.get("cleanup_verified") is not True
                or snapshot.get("writeback", {}).get("state") in {"awaiting_start", "applying"}
            ):
                return
            try:
                if live.get("disk_creation_started") and not live.get("disk"):
                    raise SourceError("TASK_DISK_CREATION_UNVERIFIED")
                self._database_proof(job_id)
                if live["metadata"].get("database") and not live.get("database_lifecycle_checked"):
                    if live["metadata"].get("database_action") == "drop":
                        self.databases.revoke(live["metadata"]["database_target_id"])
                    else:
                        self.databases.check_connection(live["project_id"])
                    live["database_lifecycle_checked"] = True
                if live.get("database_proxy") and not live.get("database_proxy_closed"):
                    self.databases.release_configuration({"proxy": live["database_proxy"]})
                    live["database_proxy_closed"] = True
                if not live.get("helper_retired"):
                    self.sandbox.cleanup_job(job_id, verified=True)
                    live["helper_retired"] = True
                if live.get("relay") and not live.get("relay_closed"):
                    live["relay"].close()
                    live["relay_closed"] = True
                if live.get("disk") and not live.get("disk_retired"):
                    live["disk"].mark_cleanup(True)
                    live["disk"].close()
                    live["disk"].retire()
                    live["disk_retired"] = True
                if live.get("cache") and not live.get("cache_released"):
                    self.cache.release(live["project_id"], live["metadata"]["cache_key"])
                    live["cache_released"] = True
            except (SourceError, OSError) as exc:
                self.store.update_completed_snapshot(
                    job_id,
                    {
                        **snapshot,
                        "resource_release": {
                            "state": "blocked",
                            "reason": str(exc).split(":", 1)[0]
                            if isinstance(exc, SourceError)
                            else "EXECUTION_RESOURCE_RELEASE_FAILED",
                        },
                    },
                )
                raise
            self.store.update_completed_snapshot(
                job_id,
                {**snapshot, "workspace_retired": True, "resource_release": {"state": "retired"}},
            )
            live["detached"] = True
            if live.get("retention_timer"):
                live["retention_timer"].cancel()
            # Logs have their own bounded lifetime in ProcessManager. Keeping a
            # completed source manifest here would retain up to 20,000 entries.
            del self.live[job_id]

    def _expire_generated_result(self, job_id):
        with self.lock:
            job = self.store.get_job(job_id)
            if job and job["snapshot"].get("writeback", {}).get("state") == "awaiting_start":
                snapshot = {**job["snapshot"], "writeback": {"state": "expired"}}
                self.store.update_completed_snapshot(job_id, snapshot)
            self._release_workspace(job_id)

    def _apply_generated_result(self, job_id):
        with self.lock:
            job = self.store.get_job(job_id)
            snapshot = dict(job["snapshot"])
            if job["state"] not in TERMINAL_STATES:
                return
            state = snapshot.get("writeback", {}).get("state")
            if state != "awaiting_start":
                return
            live = self.live.get(job_id)
            if not live or live.get("detached") or not snapshot.get("cleanup_verified"):
                snapshot["writeback"] = {"state": "unavailable"}
                self.store.update_completed_snapshot(job_id, snapshot)
                return
            # Durable intent prevents a crash/retry from reapplying a partly
            # completed multi-file write. Existing write recovery owns each file.
            snapshot["writeback"] = {"state": "applying"}
            self.store.update_completed_snapshot(job_id, snapshot)
            try:
                self._check_inputs(live["project_id"], live["metadata"])
                snapshot["writeback"] = self._writeback(live, job_id)
            except SourceError as exc:
                snapshot["writeback"] = {"state": "conflict", "reason": str(exc)}
                snapshot["result"] = "writeback_conflict"
            self.store.update_completed_snapshot(job_id, snapshot)
            self._release_workspace(job_id)

    def _writeback(self, live, job_id="generated"):
        project, meta = live["project_id"], live["metadata"]
        self.authorize(project)
        generated_source = SourceAccess(live["workspace"])
        source = self.source_for(project)
        prepared, results = [], []
        with self.write.lock, source.lock:
            for path in meta["writeback_paths"]:
                try:
                    relative = str(Path(path).relative_to(meta["cwd"])) if meta["cwd"] else path
                except ValueError:
                    raise SourceError("GENERATED_RESULT_OUTSIDE_INPUT") from None
                with generated_source.parent_fd(relative) as (parent, name):
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if info.st_nlink != 1:
                        raise SourceError("GENERATED_RESULT_UNSUPPORTED")
                document = generated_source.read(relative)
                if content_problem(document.content):
                    raise SourceError("GENERATED_RESULT_EXCLUDED")
                previous = live["baseline"]["files"].get(path)
                current = source.read(path) if previous else None
                if current and current.sha256 != previous["sha256"]:
                    raise SourceError("GENERATED_SOURCE_CONFLICT")
                self.write.check_first_touch(project, meta["write_task_id"], path, current)
                if current and (
                    len(current.content.encode()) > 256 * 1024
                    or len(document.content.encode()) > 256 * 1024
                ):
                    raise SourceError(
                        "GENERATED_EDIT_LIMIT: split larger changes into bounded edits"
                    )
                prepared.append((path, document.content, current))
            for path, text, current in prepared:
                request = "generated-" + digest([job_id, path])[:32]
                if current and current.content == text:
                    continue
                if current:
                    edit = {
                        "kind": "replace_fragment",
                        "old_text": current.content,
                        "new_text": text,
                    }
                    if not current.content:
                        edit = {
                            "kind": "insert_lines",
                            "line": 1,
                            "position": "before",
                            "text": text,
                            "expected_context": "",
                        }
                    results.append(
                        self.write.apply_edit(
                            project, meta["write_task_id"], request, path, current.sha256, edit
                        )
                    )
                else:
                    results.append(
                        self.write.create_file(project, meta["write_task_id"], request, path, text)
                    )
        return {"state": "applied", "files": len(results)}

    def _job(self, project_id, job_id):
        source = self.source_for(project_id)
        source.ensure_available()
        job = self.store.get_job(job_id)
        if not job or (job["project_id"], job["source_id"]) != (project_id, source.source_id):
            raise SourceError("EXECUTION_JOB_UNKNOWN")
        return job

    def _renew_service(self, job_id):
        live = self.live.get(job_id)
        if live and live.get("cancel_requested"):
            return False
        try:
            self.manager.touch(job_id)
            return True
        except ProcessError as exc:
            if str(exc) not in {
                "EXECUTION_SUPERVISOR_UNAVAILABLE",
                "EXECUTION_SERVICE_NOT_ACTIVE",
                "EXECUTION_JOB_UNKNOWN",
            }:
                raise
            # Exit acknowledgement can trail a closed control pipe. Keep
            # reporting the observed state without claiming a renewed lease.
            return False

    def job_status(self, project_id, job_id):
        job = self._job(project_id, job_id)
        self._database_proof(job_id)
        if job_id in self.live:
            snapshot = self._snapshot(job_id)
            if snapshot.get("service") and snapshot["state"] in {"queued", "starting", "running"}:
                renewed = self._renew_service(job_id)
                snapshot = {**self._snapshot(job_id), "service_lease_renewed": renewed}
            if (
                self.live.get(job_id, {}).get("cancel_requested")
                and snapshot["state"] not in TERMINAL_STATES
                and snapshot["state"] != "preparing"
            ):
                snapshot = {**snapshot, "state": "stopping", "cancel_requested": True}
        else:
            snapshot = job["snapshot"] or {"state": job["state"]}
        health = []
        if snapshot.get("state") == "running":
            for port in job["metadata"]["ports"]:
                connected = False
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        connected = True
                except OSError:
                    pass
                relay = self.live.get(job_id, {}).get("relay")
                backend_ready = relay.ready(port) if relay else connected
                item = {
                    "port": port,
                    "tcp_ready": connected,
                    "backend_ready": backend_ready,
                    "application_healthy": None,
                    "listener": "CoLink loopback relay" if relay else "project",
                }
                path = job["metadata"].get("health_path")
                if connected and backend_ready and path:
                    try:
                        opener = urllib.request.build_opener(
                            urllib.request.ProxyHandler({}), _NoRedirect()
                        )
                        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=0.5) as response:
                            item["application_healthy"] = 200 <= response.status < 300
                    except (OSError, ValueError):
                        item["application_healthy"] = False
                health.append(item)
        return {
            "job_id": job_id,
            "project_id": project_id,
            **snapshot,
            "health": health,
            "development_task_id": job["metadata"]["development_task_id"],
            "writeback": job["snapshot"].get("writeback"),
            "output_limits": self.manager.output_usage(),
        }

    def output(
        self, project_id, job_id, cursor=0, max_bytes=65536, wait_ms=0, acknowledge_final=False
    ):
        if (
            type(cursor) is not int
            or cursor < 0
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 65536
            or type(wait_ms) is not int
            or not 0 <= wait_ms <= 20000
            or type(acknowledge_final) is not bool
        ):
            raise SourceError("INVALID_EXECUTION_OUTPUT_REQUEST")
        job = self._job(project_id, job_id)
        live = self.live.get(job_id)
        if live and live.get("preparing"):
            return {"chunks": [], "eof": False, "state": "preparing", "next_cursor": cursor}
        try:
            result = self.manager.read_output(job_id, cursor, max_bytes, wait_ms)
        except ProcessError as exc:
            if str(exc) != "EXECUTION_JOB_UNKNOWN":
                raise
            job = self._job(project_id, job_id)
            if job["state"] not in TERMINAL_STATES:
                raise
            return {
                "chunks": [],
                "eof": True,
                "state": job["state"],
                "next_cursor": cursor,
                "output_reclaimed": "not_retained",
                "snapshot": job["snapshot"],
                **({"acknowledged": True} if acknowledge_final else {}),
            }
        if acknowledge_final:
            try:
                result["acknowledged"] = self.manager.ack_output(job_id, result["next_cursor"])
            except ProcessError as exc:
                completed = self._job(project_id, job_id)
                if str(exc) != "EXECUTION_JOB_UNKNOWN" or completed["state"] not in TERMINAL_STATES:
                    raise
                # A concurrent acknowledgement/eviction cannot erase the
                # already-read chunks in this response.
                result["acknowledged"] = bool(result["eof"])
                result["output_reclaimed"] = "not_retained"
        if job["service"] and result["state"] not in TERMINAL_STATES:
            result["service_lease_renewed"] = self._renew_service(job_id)
            if self.live.get(job_id, {}).get("cancel_requested"):
                result.update(state="stopping", cancel_requested=True)
        return result

    def revoke_database(self, database_target_id):
        """Stop only owned database jobs matching the concrete revoked target."""
        results = []
        with self.lock:
            for job_id, live in list(self.live.items()):
                if live["metadata"].get("database_target_id") != database_target_id:
                    continue
                live["cancel_requested"] = True
                if live.get("preparing"):
                    results.append(
                        {"job_id": job_id, "state": "preparing", "cancel_requested": True}
                    )
                    continue
                try:
                    result = self.manager.cancel(job_id)
                    results.append({"job_id": job_id, **result})
                except ProcessError:
                    # Revocation is durable even when owned-process stop needs attention.
                    results.append({"job_id": job_id, "state": "stop_pending"})
        return results

    def cancel(self, project_id, job_id):
        job = self._job(project_id, job_id)
        live = self.live.get(job_id)
        if live and live.get("preparing"):
            live["cancel_requested"] = True
            return {"job_id": job_id, "state": "preparing", "cancel_requested": True}
        if job["state"] in TERMINAL_STATES:
            return {"job_id": job_id, **job["snapshot"], "state": job["state"]}
        if live:
            live["cancel_requested"] = True
        try:
            result = self.manager.cancel(job_id)
            if result["state"] not in TERMINAL_STATES:
                result.update(state="stopping", cancel_requested=True)
            return result
        except ProcessError as exc:
            completed = self._job(project_id, job_id)
            if str(exc) != "EXECUTION_JOB_UNKNOWN" or completed["state"] not in TERMINAL_STATES:
                raise
            return {"job_id": job_id, **completed["snapshot"], "state": completed["state"]}

    def list_jobs(self, project_id):
        source = self.source_for(project_id)
        source.ensure_available()
        return {
            "project_id": project_id,
            "jobs": [
                self.job_status(project_id, x["job_id"])
                for x in self.store.list_jobs(
                    project_id=project_id,
                    source_id=source.source_id,
                    limit=50,
                    active_first=True,
                    completed_since=self.store.now() - self.store.receipt_seconds,
                )
            ],
        }

    def collect_expired(self):
        """Retire only expired ledger work with no remaining runtime references."""
        with self.lock:
            candidates = self.store.expired_job_candidates()
            reclaimable = [job_id for job_id in candidates if job_id not in self.live]
            result = self.store.collect_expired(reclaimable)
            result["disk_records"] = self.disk_recovery.expire_retired_records(limit=2)
            return result

    def close(self):
        self.disable()
        self.closed = True
        deadline = time.monotonic() + 60
        for worker in tuple(self.preparers):
            worker.join(max(0, deadline - time.monotonic()))
        stopped = self.manager.close()
        if self.preparers:
            raise SourceError("EXECUTION_PREPARATION_STOP_PENDING")
        if stopped and stopped.get("failed_jobs"):
            raise SourceError("EXECUTION_STOP_UNVERIFIED: retain referenced resources for recovery")
        for job_id in tuple(self.live):
            job = self.store.get_job(job_id)
            if (
                stopped
                and stopped.get("observed_tree_stopped") is True
                and job["state"] in TERMINAL_STATES
                and job["snapshot"].get("cleanup_verified") is True
                and job["snapshot"].get("writeback", {}).get("state") == "awaiting_start"
            ):
                self.store.update_completed_snapshot(
                    job_id,
                    {
                        **job["snapshot"],
                        "writeback": {"state": "unavailable", "reason": "runtime_closed"},
                    },
                )
            self._release_workspace(job_id)
        try:
            self.cache.close()
        finally:
            self.store.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise SourceError("HEALTH_REDIRECT_BLOCKED")

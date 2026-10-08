"""Coordinator contracts with real source/recovery and simulated native jobs.

These tests verify permissions, idempotency and protected writeback. They do not
establish Seatbelt isolation, APFS quotas, process cleanup or webpage behavior.
"""

import os
import sys
from types import SimpleNamespace

import pytest

import code_context.execution_coordinator as implementation
from code_context.execution_coordinator import ExecutionCoordinator
from code_context.execution_process import ProcessError
from code_context.execution_store import TERMINAL_STATES
from code_context.local_control import private_directory
from code_context.policy import MAX_FILE_BYTES
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess, SourceError
from code_context.write_coordinator import WriteCoordinator


class FakeDisk:
    instances = []

    def __init__(self, root, name, *, project_id=None, source_id=None):
        self.mount = root / (name + "-mount")
        self.mount.mkdir(parents=True, mode=0o700)
        self.image = root / (name + ".fixture")
        self.closed = False
        self.retired = False
        self.cleanup_verified = True
        self.project_id, self.source_id = project_id, source_id
        type(self).instances.append(self)

    def verify(self):
        return None

    def close(self):
        self.closed = True

    def retire(self):
        assert self.closed
        assert self.cleanup_verified
        self.retired = True

    def mark_started(self):
        self.cleanup_verified = False

    def mark_cleanup(self, verified):
        self.cleanup_verified = verified is True


class FakeSandbox:
    def __init__(self, root, protected_paths):
        self.state = private_directory(root)
        self.tools = {"python3": sys.executable, "node": sys.executable}
        self.calls = []
        self.failure = None
        self.cleaned = []
        self.cleanup_failure = None

    def available(self):
        return True

    def fingerprint(self):
        return dict(self.tools)

    def toolchain_matches(self, bound):
        return bound == self.fingerprint()

    def database_proof(self, job_id):
        return None

    def prepare(self, job_id, disk, workspace, argv, **kwargs):
        self.calls.append({"job_id": job_id, "argv": argv, "workspace": workspace, **kwargs})
        if self.failure:
            raise SourceError(self.failure)
        return [sys.executable, "fixture-helper", str(self.state.root / (job_id + ".json"))], {
            "CI": "true"
        }

    def check(self, argv, cwd, env):
        return True

    def take_payload(self, job_id):
        return None

    def discard_payload(self, job_id):
        return None

    def cleanup_job(self, job_id, *, verified):
        assert verified is True
        if self.cleanup_failure:
            raise SourceError(self.cleanup_failure)
        self.cleaned.append(job_id)


class FakeCache:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.references = {}
        self.releases = []
        self.failure = None

    def acquire(self, project, key):
        if self.failure:
            raise SourceError(self.failure)
        identity = (project, key)
        self.references[identity] = self.references.get(identity, 0) + 1
        path = self.root / project / key
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return path

    def release(self, project, key):
        identity = (project, key)
        self.references[identity] -= 1
        self.releases.append(identity)

    def status(self):
        return {"fixture": True}

    def restore_workspace(self, project, key, workspace, *, input_digest=None):
        return {"state": "empty", "artifacts": []}

    def capture_workspace(self, project, key, workspace, *, input_digest=None):
        return {"state": "unchanged", "artifacts": []}

    def close(self):
        return None


class FakeDiskRecovery:
    def __init__(self, *args, **kwargs):
        pass

    def recover(self):
        return {
            "state": "ready",
            "reclaimed": [],
            "expired_records": [],
            "blocked": [],
            "unknown": 0,
        }

    def expire_retired_records(self, *, job_ids=None, limit=100):
        return {"expired_records": list(job_ids or ()), "blocked": []}


class FakeManager:
    def __init__(self, *, sandbox_check, scope_root=None):
        self.sandbox_check = sandbox_check
        self.jobs = {}
        self.cancelled = []
        self.touched = []
        self.failure = None

    def submit(self, job_id, argv, cwd, env, *, service, on_exit, stdin_payload=None):
        if self.failure:
            raise SourceError(self.failure)
        assert self.sandbox_check(argv, cwd, env)
        self.jobs[job_id] = {
            "snapshot": {"state": "running", "service": service, "exit_code": None},
            "on_exit": on_exit,
        }

    def snapshot(self, job_id):
        if job_id not in self.jobs:
            raise ProcessError("EXECUTION_JOB_UNKNOWN")
        return dict(self.jobs[job_id]["snapshot"])

    def complete(self, job_id, *, exit_code=0, cleanup_verified=True):
        job = self.jobs[job_id]
        job["snapshot"].update(
            state="exited",
            exit_code=exit_code,
            cleanup_verified=cleanup_verified,
            tree_scope="fixture",
        )
        job["on_exit"](job["snapshot"])

    def read_output(self, job_id, cursor=0, max_bytes=65536, wait_ms=0):
        state = self.snapshot(job_id)["state"]
        return {
            "chunks": [],
            "next_cursor": cursor,
            "eof": state in TERMINAL_STATES,
            "state": state,
        }

    def output_usage(self):
        return {"retained_bytes": 0}

    def ack_output(self, job_id, cursor):
        return self.snapshot(job_id)["state"] in TERMINAL_STATES

    def touch(self, job_id):
        self.touched.append(job_id)

    def cancel(self, job_id):
        self.snapshot(job_id)
        self.cancelled.append(job_id)
        return {"job_id": job_id, "state": "stopping"}

    def close(self):
        return {"observed_tree_stopped": True, "failed_jobs": []}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    for name, replacement in (
        ("NativeSandbox", FakeSandbox),
        ("BoundedDisk", FakeDisk),
        ("ExecutionCache", FakeCache),
        ("ProcessManager", FakeManager),
        ("TaskDiskRecovery", FakeDiskRecovery),
    ):
        monkeypatch.setattr(implementation, name, replacement)
    FakeDisk.instances = []
    roots, sources = {}, {}
    for project in ("first", "second"):
        root = tmp_path / project
        root.mkdir()
        (root / "a.py").write_text("original\n")
        (root / "package.json").write_text('{"name":"fixture"}\n')
        roots[project], sources[project] = root, SourceAccess(root)
    recovery = RecoveryStore(tmp_path / "recovery")
    alive, gate = [True], [True]
    writes = WriteCoordinator(recovery, sources.__getitem__, control_alive=lambda: alive[0])
    writes.enable(["first", "second"])
    coordinator = ExecutionCoordinator(
        tmp_path / "execution",
        sources.__getitem__,
        writes,
        control_alive=lambda: alive[0],
        gate=lambda: gate[0],
    )
    coordinator.enable(["first", "second"], ports=[])
    parts = SimpleNamespace(
        coordinator=coordinator,
        roots=roots,
        sources=sources,
        writes=writes,
        recovery=recovery,
        alive=alive,
        gate=gate,
    )
    yield parts
    if not coordinator.closed:
        coordinator.close()
    writes.close()
    recovery.close()


def plan(parts, request="plan_request_0001", **kwargs):
    return parts.coordinator.plan("first", request, ["python3", "a.py"], **kwargs)


def write_task(parts, paths):
    return parts.writes.begin_write_task(
        "first", parts.writes.status()["next_task_request_id"], paths=paths
    )["task_id"]


def started(parts, prepared, request="start_request_0001"):
    return parts.coordinator.start("first", prepared["plan_id"], request)


def test_identical_plan_retry_returns_original_plan_and_development_task(harness):
    prepared = plan(harness)
    repeated = plan(harness)
    assert repeated["plan_id"] == prepared["plan_id"]
    assert repeated["development_task_id"] == prepared["development_task_id"]
    assert len(harness.coordinator.tasks) == 1


def test_database_raw_ports_and_other_active_target_proxy_ports_cannot_be_registered(harness):
    harness.coordinator.databases = SimpleNamespace(raw_service_ports=lambda: {5432, 3306, 33333})
    for port in (5432, 3306, 33333):
        with pytest.raises(SourceError, match="DATABASE_SERVICE_PORT_PROTECTED"):
            harness.coordinator.enable(["first"], ports=[port])
    # A previously registered development port becoming a database endpoint is protected too.
    harness.coordinator.grants["first"]["ports"] = (33333,)
    with pytest.raises(SourceError, match="DATABASE_SERVICE_PORT_PROTECTED"):
        plan(harness, connect_ports=[33333])


@pytest.mark.parametrize("changed", ["parameters", "project"])
def test_plan_request_cannot_cross_parameters_or_project(harness, changed):
    prepared = plan(harness)
    project = "second" if changed == "project" else "first"
    command = ["python3", "different.py"] if changed == "parameters" else ["python3", "a.py"]
    with pytest.raises(SourceError, match="CONFLICT"):
        harness.coordinator.plan(project, "plan_request_0001", command)
    assert harness.coordinator.store.get_plan(prepared["plan_id"])
    assert not harness.coordinator.manager.jobs


def test_start_retry_same_request_or_consumed_plan_runs_once(harness):
    prepared = plan(harness)
    initial = started(harness, prepared)
    retry = started(harness, prepared)
    consumed = started(harness, prepared, "another_start_0001")
    assert retry["duplicate"] and consumed["duplicate"]
    assert initial["job_id"] == retry["job_id"] == consumed["job_id"]
    assert len(harness.coordinator.manager.jobs) == 1
    assert harness.coordinator.tasks[prepared["development_task_id"]]["rounds"] == 1


def test_start_request_cannot_cross_plan_or_project(harness):
    prepared = plan(harness)
    started(harness, prepared)
    second = harness.coordinator.plan("second", "other_plan_0001", ["python3", "a.py"])
    with pytest.raises(SourceError, match="CONFLICT"):
        harness.coordinator.start("second", second["plan_id"], "start_request_0001")
    assert len(harness.coordinator.manager.jobs) == 1


def test_consumed_plan_alias_request_cannot_be_reused_with_other_plan(harness):
    prepared = plan(harness)
    run = started(harness, prepared)
    harness.coordinator.manager.complete(run["job_id"])
    alias = started(harness, prepared, "alias_start_request01")
    assert alias["duplicate"] and alias["job_id"] == run["job_id"]
    another = plan(harness, "another_plan_request", operation="build")
    with pytest.raises(SourceError, match="CONFLICT"):
        started(harness, another, "alias_start_request01")
    assert len(harness.coordinator.manager.jobs) == 1


def test_completed_writeback_retry_has_one_change_and_one_recovery_record(harness):
    task = write_task(harness, ["a.py"])
    prepared = plan(harness, write_task_id=task, writeback_paths=["a.py"])
    run = started(harness, prepared)
    (harness.coordinator.live[run["job_id"]]["workspace"] / "a.py").write_text("generated\n")
    harness.coordinator.manager.complete(run["job_id"])
    before = harness.recovery.db.execute(
        "SELECT count(*) FROM operations WHERE task_id=?", (task,)
    ).fetchone()[0]
    first = started(harness, prepared)
    second = started(harness, prepared, "writeback_retry_req1")
    after = harness.recovery.db.execute(
        "SELECT count(*) FROM operations WHERE task_id=?", (task,)
    ).fetchone()[0]
    assert first["duplicate"] and second["duplicate"]
    assert (harness.roots["first"] / "a.py").read_text() == "generated\n"
    assert before == after == 1
    assert len(harness.coordinator.manager.jobs) == 1


@pytest.mark.parametrize("changed", ["input", "config"])
def test_changed_source_or_configuration_cannot_launch_old_plan(harness, changed):
    prepared = plan(harness)
    path = "a.py" if changed == "input" else "package.json"
    (harness.roots["first"] / path).write_text("changed after plan\n")
    with pytest.raises(SourceError, match="INPUT_CHANGED|SOURCE_CHANGED"):
        started(harness, prepared)
    assert not harness.coordinator.manager.jobs
    assert all(disk.closed for disk in FakeDisk.instances)
    assert all(disk.retired for disk in FakeDisk.instances)
    assert all(value == 0 for value in harness.coordinator.cache.references.values())


@pytest.mark.parametrize("changed", ["input", "config"])
def test_changed_input_invalidates_rehearsal_before_promotion(harness, changed):
    prepared = plan(harness, operation="install")
    run = harness.coordinator.rehearse("first", prepared["plan_id"], "rehearse_request_01")
    harness.coordinator.manager.complete(run["job_id"])
    path = "a.py" if changed == "input" else "package.json"
    (harness.roots["first"] / path).write_text("changed after rehearsal\n")
    with pytest.raises(SourceError, match="INPUT_CHANGED|SOURCE_CHANGED"):
        started(harness, prepared)
    assert len(harness.coordinator.manager.jobs) == 1


@pytest.mark.parametrize("changed", ["epoch", "source", "gate", "control"])
def test_old_plan_cannot_start_after_authorization_identity_change(harness, changed):
    prepared = plan(harness)
    if changed == "epoch":
        harness.coordinator.enable(["first", "second"], ports=[])
    elif changed == "source":
        replacement = harness.roots["first"].parent / "replacement"
        replacement.mkdir()
        (replacement / "a.py").write_text("replacement\n")
        harness.sources["first"] = SourceAccess(replacement)
    elif changed == "gate":
        harness.gate[0] = False
    else:
        harness.alive[0] = False
    with pytest.raises(SourceError, match="UNAVAILABLE|DISABLED"):
        started(harness, prepared)
    assert not harness.coordinator.manager.jobs


def test_expired_plan_requires_new_plan_without_starting(harness):
    prepared = plan(harness)
    harness.coordinator.store._clock = lambda: prepared["expires_at"] + 1
    with pytest.raises(SourceError, match="UNAVAILABLE|EXPIRED"):
        started(harness, prepared)
    assert not harness.coordinator.manager.jobs


def test_changed_toolchain_invalidates_plan_before_launch(harness):
    prepared = plan(harness)
    harness.coordinator.sandbox.tools["python3"] = "/changed/python3"
    with pytest.raises(SourceError, match="TOOLCHAIN_CHANGED"):
        started(harness, prepared)
    assert not harness.coordinator.manager.jobs


def test_execution_permission_change_invalidates_bound_input(harness):
    prepared = plan(harness)
    (harness.roots["first"] / "a.py").chmod(0o755)
    with pytest.raises(SourceError, match="INPUT_CHANGED|SOURCE_CHANGED"):
        started(harness, prepared)
    assert not harness.coordinator.manager.jobs


@pytest.mark.parametrize("stage", ["cache", "sandbox", "manager"])
def test_partial_preparation_is_durable_failure_and_releases_own_resources(harness, stage):
    prepared = plan(harness)
    getattr(harness.coordinator, stage).failure = "FIXTURE_PREPARATION_FAILED"
    initial = started(harness, prepared)
    assert initial["state"] == "failed"
    assert initial["reason"] == "FIXTURE_PREPARATION_FAILED"
    replay = started(harness, prepared)
    assert replay["duplicate"] and replay["state"] == "failed"
    assert all(live.get("detached") for live in harness.coordinator.live.values())
    assert not harness.coordinator.manager.jobs
    assert all(disk.closed for disk in FakeDisk.instances)
    assert all(value == 0 for value in harness.coordinator.cache.references.values())
    assert (harness.roots["first"] / "a.py").read_text() == "original\n"


def test_generate_rehearsal_promotes_generated_result_without_reexecuting(harness):
    task = write_task(harness, ["a.py"])
    prepared = plan(harness, operation="generate", write_task_id=task, writeback_paths=["a.py"])
    run = harness.coordinator.rehearse("first", prepared["plan_id"], "rehearse_request_01")
    (harness.coordinator.live[run["job_id"]]["workspace"] / "a.py").write_text("generated\n")
    harness.coordinator.manager.complete(run["job_id"])
    promoted = started(harness, prepared)
    assert promoted["job_id"] == run["job_id"]
    assert len(harness.coordinator.manager.jobs) == 1
    assert (harness.roots["first"] / "a.py").read_text() == "generated\n"
    assert promoted["writeback"]["state"] == "applied"


def test_successful_job_writeback_uses_recovery_coordinator(harness):
    task = write_task(harness, ["a.py"])
    prepared = plan(harness, write_task_id=task, writeback_paths=["a.py"])
    run = started(harness, prepared)
    (harness.coordinator.live[run["job_id"]]["workspace"] / "a.py").write_text("generated\n")
    disk = harness.coordinator.live[run["job_id"]]["disk"]
    harness.coordinator.manager.complete(run["job_id"])
    status = harness.coordinator.job_status("first", run["job_id"])
    assert status["writeback"]["state"] == "applied"
    assert disk.retired
    assert run["job_id"] not in harness.coordinator.live
    assert (harness.roots["first"] / "a.py").read_text() == "generated\n"
    file_record = harness.recovery.db.execute(
        "SELECT kind,origin_hash,last_hash FROM files WHERE task_id=? AND path='a.py'", (task,)
    ).fetchone()
    assert file_record["kind"] == "modified"
    assert file_record["origin_hash"] != file_record["last_hash"]


def test_unconfirmed_execution_tree_cannot_write_generated_source(harness):
    task = write_task(harness, ["a.py"])
    prepared = plan(harness, write_task_id=task, writeback_paths=["a.py"])
    run = started(harness, prepared)
    (harness.coordinator.live[run["job_id"]]["workspace"] / "a.py").write_text("unsafe result\n")
    harness.coordinator.manager.complete(run["job_id"], cleanup_verified=False)
    assert (harness.roots["first"] / "a.py").read_text() == "original\n"
    assert not harness.coordinator.live[run["job_id"]]["disk"].closed
    assert not harness.coordinator.live[run["job_id"]]["disk"].retired
    assert sum(harness.coordinator.cache.references.values()) == 1


def test_external_source_edit_during_generation_is_not_overwritten(harness):
    task = write_task(harness, ["a.py"])
    prepared = plan(harness, write_task_id=task, writeback_paths=["a.py"])
    run = started(harness, prepared)
    (harness.coordinator.live[run["job_id"]]["workspace"] / "a.py").write_text("generated\n")
    (harness.roots["first"] / "a.py").write_text("external source\n")
    harness.coordinator.manager.complete(run["job_id"])
    status = harness.coordinator.job_status("first", run["job_id"])
    assert status["writeback"]["state"] == "conflict"
    assert (harness.roots["first"] / "a.py").read_text() == "external source\n"


@pytest.mark.parametrize("unsafe", ["final_symlink", "parent_symlink", "hardlink", "oversize"])
def test_generated_result_rejects_links_and_excess_size(harness, unsafe):
    root = harness.roots["first"]
    (root / "sub").mkdir()
    (root / "sub" / "b.py").write_text("original nested\n")
    path = "sub/b.py" if unsafe == "parent_symlink" else "a.py"
    task = write_task(harness, [path])
    prepared = plan(harness, write_task_id=task, writeback_paths=[path])
    run = started(harness, prepared)
    workspace = harness.coordinator.live[run["job_id"]]["workspace"]
    original = (root / path).read_text()
    other = workspace / "unsafe-fixture"
    other.mkdir()
    (other / "b.py").write_text("linked generated result\n")
    if unsafe == "parent_symlink":
        (workspace / "sub" / "b.py").rename(workspace / "retained-original.py")
        (workspace / "sub").rmdir()
        (workspace / "sub").symlink_to(other, target_is_directory=True)
    else:
        generated = workspace / path
        generated.rename(workspace / "retained-original.py")
        if unsafe == "final_symlink":
            generated.symlink_to(other / "b.py")
        elif unsafe == "hardlink":
            os.link(other / "b.py", generated)
        else:
            generated.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    harness.coordinator.manager.complete(run["job_id"])
    status = harness.coordinator.job_status("first", run["job_id"])
    assert status["writeback"]["state"] == "conflict"
    assert (root / path).read_text() == original


def test_writeback_path_outside_selected_cwd_is_rejected_before_launch(harness):
    root = harness.roots["first"]
    (root / "sub").mkdir()
    (root / "sub" / "a.py").write_text("nested\n")
    task = write_task(harness, ["a.py"])
    with pytest.raises(SourceError, match="SCOPE|PATH|WRITEBACK|OUTSIDE_INPUT"):
        plan(harness, cwd="sub", write_task_id=task, writeback_paths=["a.py"])
    assert not harness.coordinator.manager.jobs


def test_restart_marks_unfinished_job_interrupted_and_never_launches(harness):
    prepared = plan(harness)
    run = started(harness, prepared)
    coordinator = harness.coordinator
    coordinator.close()
    restarted = ExecutionCoordinator(
        coordinator.state.root,
        harness.sources.__getitem__,
        harness.writes,
        control_alive=lambda: True,
        gate=lambda: True,
    )
    try:
        status = restarted.job_status("first", run["job_id"])
        assert status["state"] == "interrupted"
        assert not restarted.grants and not restarted.manager.jobs
        assert restarted.output("first", run["job_id"])["output_reclaimed"] == "not_retained"
    finally:
        restarted.close()


def test_cancel_reports_unfinished_stop_instead_of_completed(harness):
    run = started(harness, plan(harness))
    result = harness.coordinator.cancel("first", run["job_id"])
    assert result["state"] == "stopping"
    assert harness.coordinator.manager.cancelled == [run["job_id"]]


def test_service_status_and_output_extend_idle_lease(harness):
    run = started(harness, plan(harness, operation="serve", service=True))
    before = len(harness.coordinator.manager.touched)
    harness.coordinator.job_status("first", run["job_id"])
    harness.coordinator.output("first", run["job_id"])
    assert len(harness.coordinator.manager.touched) == before + 2


def test_closed_supervisor_does_not_turn_observed_status_or_output_into_error(harness, monkeypatch):
    coordinator = harness.coordinator
    run = started(harness, plan(harness, service=True))

    def closed(*args):
        raise ProcessError("EXECUTION_SUPERVISOR_UNAVAILABLE")

    monkeypatch.setattr(coordinator.manager, "touch", closed)
    status = coordinator.job_status("first", run["job_id"])
    assert status["state"] == "running" and status["service_lease_renewed"] is False
    assert status.get("cleanup_verified") is not True
    output = coordinator.output("first", run["job_id"])
    assert output["eof"] is False and output["service_lease_renewed"] is False


def test_service_completion_during_renewal_reports_actual_terminal_receipt(harness, monkeypatch):
    coordinator = harness.coordinator
    run = started(harness, plan(harness, service=True))

    def completed(job_id):
        coordinator.manager.complete(job_id)
        raise ProcessError("EXECUTION_SERVICE_NOT_ACTIVE")

    monkeypatch.setattr(coordinator.manager, "touch", completed)
    status = coordinator.job_status("first", run["job_id"])
    assert status["state"] == "exited" and status["cleanup_verified"] is True
    assert status["workspace_retired"] is True
    assert status["service_lease_renewed"] is False


def test_cancel_pending_is_explicit_and_queries_do_not_renew_stopping_service(harness):
    coordinator = harness.coordinator
    run = started(harness, plan(harness, service=True))
    before = len(coordinator.manager.touched)
    requested = coordinator.cancel("first", run["job_id"])
    assert requested["state"] == "stopping" and requested["cancel_requested"] is True
    status = coordinator.job_status("first", run["job_id"])
    assert status["state"] == "stopping" and status.get("cleanup_verified") is not True
    output = coordinator.output("first", run["job_id"])
    assert output["state"] == "stopping" and not output["eof"]
    assert len(coordinator.manager.touched) == before
    coordinator.manager.complete(run["job_id"])
    assert coordinator.job_status("first", run["job_id"])["state"] == "exited"


def test_retired_workspace_releases_manifest_but_unread_output_stays_readable(harness, monkeypatch):
    coordinator = harness.coordinator
    run = started(harness, plan(harness))
    disk = coordinator.live[run["job_id"]]["disk"]
    coordinator.manager.complete(run["job_id"])
    assert not coordinator.live and disk.retired
    assert coordinator.store.get_job(run["job_id"])["snapshot"]["workspace_retired"] is True
    assert coordinator.sandbox.cleaned == [run["job_id"]]
    monkeypatch.setattr(
        coordinator.manager,
        "read_output",
        lambda job_id, cursor, max_bytes, wait_ms: {
            "chunks": [{"text": "retained final output\n", "stream": "stdout"}],
            "next_cursor": 22,
            "state": "exited",
            "eof": True,
        },
    )
    output = coordinator.output("first", run["job_id"])
    assert output["chunks"][0]["text"] == "retained final output\n"
    assert "output_reclaimed" not in output
    assert coordinator.status()["execution_jobs"] == []


def test_manager_eviction_keeps_terminal_receipt_output_and_cancel_idempotent(harness):
    coordinator = harness.coordinator
    prepared = plan(harness)
    run = started(harness, prepared)
    coordinator.manager.complete(run["job_id"])
    del coordinator.manager.jobs[run["job_id"]]
    for _ in range(2):
        output = coordinator.output("first", run["job_id"], acknowledge_final=True)
        assert output["eof"] and output["output_reclaimed"] == "not_retained"
        assert output["acknowledged"] is True
        assert coordinator.cancel("first", run["job_id"])["state"] == "exited"
    assert started(harness, prepared)["duplicate"]
    coordinator.disable()
    assert coordinator.status()["execution_jobs"] == []
    assert not coordinator.manager.cancelled


@pytest.mark.parametrize("operation", ["output", "cancel", "disable", "status"])
def test_concurrent_completion_and_manager_eviction_recheck_durable_terminal_state(
    harness, monkeypatch, operation
):
    coordinator = harness.coordinator
    run = started(harness, plan(harness))
    method = {
        "output": "read_output",
        "cancel": "cancel",
        "disable": "cancel",
        "status": "snapshot",
    }[operation]

    def complete_before_lookup(job_id, *args):
        coordinator.manager.complete(job_id)
        del coordinator.manager.jobs[job_id]
        raise ProcessError("EXECUTION_JOB_UNKNOWN")

    monkeypatch.setattr(coordinator.manager, method, complete_before_lookup)
    if operation == "output":
        assert coordinator.output("first", run["job_id"])["state"] == "exited"
    elif operation == "cancel":
        assert coordinator.cancel("first", run["job_id"])["state"] == "exited"
    elif operation == "disable":
        coordinator.disable()
        assert not coordinator.grants
    else:
        assert coordinator.status()["execution_jobs"][0]["state"] == "exited"
    assert coordinator.store.get_job(run["job_id"])["snapshot"]["workspace_retired"] is True


def test_eviction_during_final_ack_preserves_already_read_log_chunks(harness, monkeypatch):
    coordinator = harness.coordinator
    run = started(harness, plan(harness))
    coordinator.manager.complete(run["job_id"])
    monkeypatch.setattr(
        coordinator.manager,
        "read_output",
        lambda *args: {
            "chunks": [{"text": "last output", "stream": "stderr"}],
            "next_cursor": 11,
            "state": "exited",
            "eof": True,
        },
    )

    def evict_before_ack(job_id, cursor):
        del coordinator.manager.jobs[job_id]
        raise ProcessError("EXECUTION_JOB_UNKNOWN")

    monkeypatch.setattr(coordinator.manager, "ack_output", evict_before_ack)
    result = coordinator.output("first", run["job_id"], acknowledge_final=True)
    assert result["chunks"] == [{"text": "last output", "stream": "stderr"}]
    assert result["acknowledged"] and result["output_reclaimed"] == "not_retained"


def test_completion_inside_submit_does_not_lose_start_ready_reference(harness, monkeypatch):
    coordinator = harness.coordinator
    submit = coordinator.manager.submit

    def finish_immediately(job_id, *args, **kwargs):
        submit(job_id, *args, **kwargs)
        coordinator.manager.complete(job_id)

    monkeypatch.setattr(coordinator.manager, "submit", finish_immediately)
    run = started(harness, plan(harness))
    assert run["state"] == "exited" and run["initial_output"]["eof"]
    assert coordinator.store.get_job(run["job_id"])["snapshot"]["cleanup_verified"] is True
    coordinator._release_workspace(run["job_id"])
    assert not coordinator.live


def test_unsafe_terminal_references_fill_working_set_instead_of_allocating_more(harness):
    coordinator = harness.coordinator
    for index in range(8):
        prepared = plan(harness, f"unsafe_plan_{index:04}")
        run = started(harness, prepared, f"unsafe_start_{index:04}")
        coordinator.manager.complete(run["job_id"], cleanup_verified=False)
    assert len(coordinator.live) == 8
    assert all(not disk.retired for disk in FakeDisk.instances)
    prepared = plan(harness, "unsafe_plan_overflow")
    with pytest.raises(SourceError, match="EXECUTION_QUEUE_FULL"):
        started(harness, prepared, "unsafe_start_overflow")
    assert len(FakeDisk.instances) == 8


def test_preparation_cleanup_error_is_visible_and_keeps_private_references(harness):
    coordinator = harness.coordinator
    coordinator.sandbox.failure = "FIXTURE_PREPARATION_FAILED"
    coordinator.sandbox.cleanup_failure = "FIXTURE_HELPER_IDENTITY_CHANGED"
    try:
        run = started(harness, plan(harness))
        assert run["state"] == "failed"
        assert run["resource_release"] == {
            "state": "blocked",
            "reason": "FIXTURE_HELPER_IDENTITY_CHANGED",
        }
        assert run["job_id"] in coordinator.live
        assert not coordinator.live[run["job_id"]]["disk"].retired
        assert sum(coordinator.cache.references.values()) == 1
        assert not coordinator.store.expired_job_candidates()
    finally:
        coordinator.sandbox.cleanup_failure = None


def test_failed_disk_constructor_cannot_claim_retirement_or_release_working_set(
    harness, monkeypatch
):
    coordinator = harness.coordinator

    def partially_created_disk(root, name, **kwargs):
        root.mkdir(mode=0o700, exist_ok=True)
        (root / (name + ".partial-fixture")).write_text("owned constructor evidence")
        raise SourceError("FIXTURE_DISK_ATTACH_FAILED")

    monkeypatch.setattr(implementation, "BoundedDisk", partially_created_disk)
    run = started(harness, plan(harness))
    assert run["state"] == "failed"
    assert run["resource_release"] == {
        "state": "blocked",
        "reason": "TASK_DISK_CREATION_UNVERIFIED",
    }
    assert "workspace_retired" not in run
    assert run["job_id"] in coordinator.live
    assert coordinator.live[run["job_id"]]["disk_creation_started"] is True
    assert coordinator.live[run["job_id"]]["disk"] is None
    assert not coordinator.store.expired_job_candidates()
    with pytest.raises(SourceError, match="TASK_DISK_CREATION_UNVERIFIED"):
        coordinator.close()
    # Simulate host shutdown without deleting the unconfirmed fixture evidence.
    coordinator.store.close()
    coordinator.closed = True


def test_rehearsed_generated_result_remains_referenced_until_promotion(harness):
    coordinator = harness.coordinator
    task = write_task(harness, ["a.py"])
    prepared = plan(harness, operation="generate", write_task_id=task, writeback_paths=["a.py"])
    run = coordinator.rehearse("first", prepared["plan_id"], "rehearse_reference01")
    disk = coordinator.live[run["job_id"]]["disk"]
    coordinator.manager.complete(run["job_id"])
    coordinator._release_workspace(run["job_id"])
    assert run["job_id"] in coordinator.live and not disk.retired
    assert coordinator.store.get_job(run["job_id"])["snapshot"]["writeback"]["state"] == (
        "awaiting_start"
    )
    coordinator._expire_generated_result(run["job_id"])
    assert run["job_id"] not in coordinator.live and disk.retired


def test_reconnect_list_keeps_old_running_service_before_recent_completed_jobs(harness):
    coordinator = harness.coordinator
    service = started(harness, plan(harness, service=True, operation="serve"))
    for index in range(55):
        run = started(harness, plan(harness, f"later_plan_{index:04}"), f"later_start_{index:04}")
        coordinator.manager.complete(run["job_id"])
    jobs = coordinator.list_jobs("first")["jobs"]
    assert len(jobs) == 50
    assert jobs[0]["job_id"] == service["job_id"] and jobs[0]["state"] == "running"
    assert len(coordinator.live) == 1


def test_start_same_receipt_survives_plan_expiry_but_new_request_cannot_launch(harness):
    coordinator = harness.coordinator
    now = [1000.0]
    coordinator.store._clock = lambda: now[0]
    prepared = plan(harness)
    run = started(harness, prepared)
    coordinator.manager.complete(run["job_id"])
    now[0] += 901
    assert started(harness, prepared)["job_id"] == run["job_id"]
    with pytest.raises(SourceError, match="PLAN_UNAVAILABLE"):
        started(harness, prepared, "expired_plan_new_req")
    assert len(coordinator.manager.jobs) == 1


def test_explicit_gc_requires_new_plan_and_allows_old_request_with_fresh_plan(harness):
    coordinator = harness.coordinator
    now = [1000.0]
    coordinator.store._clock = lambda: now[0]
    prepared = plan(harness)
    run = started(harness, prepared)
    coordinator.manager.complete(run["job_id"])
    now[0] += coordinator.store.receipt_seconds + 1
    assert coordinator.collect_expired()["job_ids"] == [run["job_id"]]
    with pytest.raises(SourceError, match="PLAN_UNAVAILABLE"):
        started(harness, prepared)
    new_plan = plan(harness)
    assert new_plan["plan_id"] != prepared["plan_id"]
    new_run = started(harness, new_plan)
    assert new_run["job_id"] != run["job_id"] and not new_run["duplicate"]
    assert len(coordinator.manager.jobs) == 2


def test_expired_ledger_with_live_reference_is_not_collected(harness):
    coordinator = harness.coordinator
    now = [1000.0]
    coordinator.store._clock = lambda: now[0]
    run = started(harness, plan(harness))
    live = coordinator.live[run["job_id"]]
    coordinator.manager.complete(run["job_id"])
    # Model another coordinator-owned pending follow-up reference after native
    # retirement. The store proof alone cannot authorize deleting its receipt.
    coordinator.live[run["job_id"]] = live
    now[0] += coordinator.store.receipt_seconds + 1
    try:
        assert coordinator.collect_expired()["jobs"] == 0
        assert coordinator.store.get_job(run["job_id"]) is not None
    finally:
        del coordinator.live[run["job_id"]]
    assert coordinator.collect_expired()["jobs"] == 1


def test_explicit_close_invalidates_generated_result_then_retires_workspace(harness):
    coordinator = harness.coordinator
    task = write_task(harness, ["a.py"])
    prepared = plan(harness, operation="generate", write_task_id=task, writeback_paths=["a.py"])
    run = coordinator.rehearse("first", prepared["plan_id"], "rehearse_closed_result")
    disk = coordinator.live[run["job_id"]]["disk"]
    coordinator.manager.complete(run["job_id"])
    coordinator.close()
    assert disk.retired and not coordinator.live
    restarted = ExecutionCoordinator(
        coordinator.state.root,
        harness.sources.__getitem__,
        harness.writes,
        control_alive=lambda: True,
        gate=lambda: True,
    )
    try:
        status = restarted.job_status("first", run["job_id"])
        assert status["writeback"] == {"state": "unavailable", "reason": "runtime_closed"}
        assert status["workspace_retired"] is True
        assert (harness.roots["first"] / "a.py").read_text() == "original\n"
    finally:
        restarted.close()


def test_reclaimed_generator_after_restart_reports_unavailable_and_keeps_receipt(
    harness, monkeypatch
):
    coordinator = harness.coordinator
    task = write_task(harness, ["a.py"])
    prepared = plan(harness, operation="generate", write_task_id=task, writeback_paths=["a.py"])
    run = coordinator.rehearse("first", prepared["plan_id"], "rehearse_restart_result")
    coordinator.manager.complete(run["job_id"])
    # Simulate a lost runtime: it cannot run normal close's state transition.
    coordinator.live[run["job_id"]]["retention_timer"].cancel()
    coordinator.store.close()
    coordinator.closed = True
    monkeypatch.setattr(
        FakeDiskRecovery,
        "recover",
        lambda self: {
            "state": "ready",
            "reclaimed": [run["job_id"]],
            "expired_records": [],
            "blocked": [],
            "unknown": 0,
        },
    )
    restarted = ExecutionCoordinator(
        coordinator.state.root,
        harness.sources.__getitem__,
        harness.writes,
        control_alive=lambda: True,
        gate=lambda: True,
    )
    try:
        status = restarted.job_status("first", run["job_id"])
        assert status["writeback"] == {"state": "unavailable", "reason": "runtime_restarted"}
        assert status["workspace_retired"] is True
        assert restarted.store.get_plan(prepared["plan_id"])["consumed_by"] == run["job_id"]
        assert not restarted.manager.jobs
        assert (harness.roots["first"] / "a.py").read_text() == "original\n"
    finally:
        restarted.close()


def test_independently_reclaimed_interruption_updates_receipt_without_launching(
    harness, monkeypatch
):
    coordinator = harness.coordinator
    run = started(harness, plan(harness))
    coordinator.store.close()
    coordinator.closed = True
    proof = {
        "job_id": run["job_id"],
        "resource_id": 1234,
        "cleanup_verified": True,
        "launchd_retired": True,
    }
    monkeypatch.setattr(
        FakeDiskRecovery,
        "recover",
        lambda self: {
            "state": "ready",
            "reclaimed": [run["job_id"]],
            "expired_records": [],
            "blocked": [],
            "unknown": 0,
            "cleanup_proofs": {run["job_id"]: proof},
        },
    )
    restarted = ExecutionCoordinator(
        coordinator.state.root,
        harness.sources.__getitem__,
        harness.writes,
        control_alive=lambda: True,
        gate=lambda: True,
    )
    try:
        status = restarted.job_status("first", run["job_id"])
        assert status["state"] == "interrupted"
        assert status["reason"] == "runtime_restarted"
        assert status["cleanup_verified"] is True
        assert status["workspace_retired"] is True
        assert status["resource_id"] == 1234
        assert status["cleanup_origin"] == "verified_retired_resource_scope"
        assert restarted.output("first", run["job_id"])["output_reclaimed"] == "not_retained"
        assert not restarted.manager.jobs and not restarted.live
        assert restarted.sandbox.cleaned == [run["job_id"]]
        assert (harness.roots["first"] / "a.py").read_text() == "original\n"
    finally:
        restarted.close()

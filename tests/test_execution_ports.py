"""Port operations target only fresh explicitly controlled fixture processes."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_context.execution_ports import NativeProcesses, PortError, PortsCoordinator
from code_context.execution_scope import MacKernel, ProcessIdentity
from code_context.source_access import SourceAccess

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS native process identity")


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    state = tmp_path / "execution"
    state.mkdir(mode=0o700)
    source = SourceAccess(root)
    epoch, alive = [1], [True]

    class Execution:
        def __init__(self):
            self.state = SimpleNamespace(root=state)
            self.grants = {"project": {"ports": (3000, 5173, 8000, 8080, 18080)}}
            self.live = {}
            self.manager = SimpleNamespace(snapshot=lambda _: {})
            self.store = SimpleNamespace(get_job=lambda _: None)
            self.cancellations = []

        def authorize(self, project):
            if project != "project" or not epoch[0] or not alive[0]:
                raise PortError("EXECUTION_DISABLED: enable locally")
            return {"epoch": epoch[0], "source_id": source.source_id}

        def cancel(self, project, job_id):
            self.cancellations.append((project, job_id))

    execution = Execution()
    coordinator = PortsCoordinator(
        lambda _: source,
        execution,
        lambda: alive[0],
        term_wait_seconds=0.25,
        kill_wait_seconds=0.25,
    )
    yield root, source, execution, coordinator, epoch, alive
    coordinator.close()


@pytest.fixture
def listeners(parts):
    processes = []

    def start(*, cwd=None, ignore_term=False):
        script = (
            "import signal,socket,time; "
            + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "")
            + "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
            + "print(s.getsockname()[1],flush=True); time.sleep(600)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=cwd or parts[0],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
        processes.append(process)
        port = int(process.stdout.readline())
        return process, port

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        process.stdout.close()


def test_native_microsecond_identity_and_cwd_match_fixture(parts, listeners):
    process, _ = listeners()
    info = NativeProcesses().inspect(process.pid)
    assert info["pid"] == process.pid
    assert info["uid"] == info["ruid"] == info["svuid"] == os.getuid()
    assert info["start_sec"] > 0 and 0 <= info["start_usec"] < 1_000_000
    assert info["unique"] > 0 and info["version"] > 0
    assert Path(info["cwd"]) == parts[0]
    assert NativeProcesses().inspect(process.pid) == info


def test_atomic_signal_rejects_wrong_native_generation_without_numeric_fallback(
    parts, listeners, monkeypatch
):
    process, _ = listeners()
    native = parts[3].native
    original = native.inspect(process.pid)
    native.kernel = MacKernel()
    wrong = {**original, "version": original["version"] + 1}
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("no numeric PID fallback"))
    with pytest.raises(PortError, match="PORT_IDENTITY_CHANGED"):
        native.signal(wrong, signal.SIGTERM)
    assert process.poll() is None


def test_confirmed_release_uses_pinned_native_identity_not_numeric_kill(
    parts, listeners, monkeypatch
):
    process, _ = listeners()
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid)
    coordinator.confirm_release(plan["port_plan_id"])
    expected = coordinator.plans[plan["port_plan_id"]]["process"]
    kernel = MacKernel()
    signal_identity = kernel.signal_identity
    seen = []

    def pinned(identity, requested_signal):
        seen.append((identity, requested_signal))
        return signal_identity(identity, requested_signal)

    monkeypatch.setattr(kernel, "signal_identity", pinned)
    coordinator.native.kernel = kernel
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("no numeric PID fallback"))
    result = coordinator.release("project", plan["port_plan_id"], "atomic_release_req01")
    assert result["process_stopped"]
    assert seen == [
        (ProcessIdentity(process.pid, expected["unique"], expected["version"]), signal.SIGTERM)
    ]


def test_status_includes_non_default_ports_without_argv_cwd_or_environment(parts, listeners):
    process, port = listeners()
    report = parts[3].status("project")
    row = next(row for row in report["listeners"] if row["pid"] == process.pid)
    assert row["port"] == port and row["ownership"] == "project_process"
    assert row["requires_local_confirmation"] and row["release_available"]
    assert port in report["observed_ports"]
    assert "cwd" not in row and "argv" not in row and "environment" not in row
    assert "time.sleep" not in json.dumps(report)


def test_read_only_status_works_before_execution_is_enabled(parts, listeners):
    process, port = listeners()
    parts[4][0] = 0
    parts[2].grants = {}
    report = parts[3].status("project")
    assert any(row["pid"] == process.pid and row["port"] == port for row in report["listeners"])
    with pytest.raises(PortError, match="EXECUTION_DISABLED"):
        parts[3].plan_release("project", process.pid)


def test_external_process_requires_exact_local_confirmation_and_replays_once(parts, listeners):
    process, _ = listeners()
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid)
    with pytest.raises(PortError, match="PORT_LOCAL_CONFIRMATION_REQUIRED"):
        coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert process.poll() is None
    coordinator.confirm_release(plan["port_plan_id"])
    result = coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert result["process_stopped"] and result["ports_available"]
    assert result["state"] == "released" and not result["force_used"]
    assert process.wait(timeout=2) == -signal.SIGTERM
    replay = coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert replay["duplicate"] and replay["state"] == "released"


@pytest.mark.parametrize("force_confirmed", [False, True])
def test_force_kill_requires_separate_local_permission(parts, listeners, force_confirmed):
    process, _ = listeners(ignore_term=True)
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid, force=True)
    coordinator.confirm_release(plan["port_plan_id"], allow_force=force_confirmed)
    result = coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert result["force_used"] is force_confirmed
    assert result["force_authorized"] is force_confirmed
    if force_confirmed:
        assert result["state"] == "released" and process.wait(timeout=2) == -signal.SIGKILL
    else:
        assert result["state"] == "stopping" and process.poll() is None
        assert coordinator.release("project", plan["port_plan_id"], "release_req_0001")[
            "requires_new_local_confirmation"
        ]


def test_force_cannot_be_added_to_a_plan_that_did_not_request_it(parts, listeners):
    process, _ = listeners()
    plan = parts[3].plan_release("project", process.pid)
    with pytest.raises(PortError, match="PORT_FORCE_NOT_PLANNED"):
        parts[3].confirm_release(plan["port_plan_id"], allow_force=True)
    assert process.poll() is None


@pytest.mark.parametrize("change", ["epoch", "identity", "listeners"])
def test_pid_start_identity_grant_and_listener_cas_prevent_signaling(
    parts, listeners, monkeypatch, change
):
    process, _ = listeners()
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid)
    coordinator.confirm_release(plan["port_plan_id"])
    if change == "epoch":
        parts[4][0] += 1
    elif change == "identity":
        real = coordinator.native.inspect

        def replaced(pid):
            info = real(pid)
            if pid == process.pid:
                info = {**info, "start_usec": (info["start_usec"] + 1) % 1_000_000}
            return info

        monkeypatch.setattr(coordinator.native, "inspect", replaced)
    else:
        monkeypatch.setattr(coordinator, "_listeners", lambda *args: [])
    with pytest.raises(
        PortError, match="PORT_PLAN_SCOPE|PORT_IDENTITY_CHANGED|PORT_LISTENERS_CHANGED"
    ):
        coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert process.poll() is None


@pytest.mark.parametrize("other", ["outside", "nested", "system"])
def test_other_project_system_or_uncertain_cwd_is_never_releaseable(
    parts, listeners, tmp_path, monkeypatch, other
):
    if other == "outside":
        directory = tmp_path / "other_project"
        directory.mkdir()
    else:
        directory = parts[0] / "nested"
        directory.mkdir()
        if other == "nested":
            (directory / ".git").mkdir()
    process, _ = listeners(cwd=directory)
    coordinator = parts[3]
    if other == "system":
        real = coordinator.native.inspect

        def system(pid):
            info = real(pid)
            return {**info, "uid": 0} if pid == process.pid else info

        monkeypatch.setattr(coordinator.native, "inspect", system)
    report = coordinator.status("project")
    row = next(row for row in report["listeners"] if row["pid"] == process.pid)
    assert row["ownership"] == "other_or_unknown" and not row["release_available"]
    with pytest.raises(PortError, match="PORT_PROCESS_SCOPE"):
        coordinator.plan_release("project", process.pid)
    assert process.poll() is None


def test_control_loss_before_confirmation_or_signal_preserves_external_process(parts, listeners):
    process, _ = listeners()
    plan = parts[3].plan_release("project", process.pid)
    parts[5][0] = False
    with pytest.raises(PortError, match="CONTROL|DISABLED"):
        parts[3].confirm_release(plan["port_plan_id"])
    assert process.poll() is None


def test_restart_does_not_continue_external_killing_and_completed_receipt_survives(
    parts, listeners
):
    process, _ = listeners(ignore_term=True)
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid, force=True)
    coordinator.confirm_release(plan["port_plan_id"])
    result = coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert result["state"] == "stopping"
    coordinator.close()
    restarted = PortsCoordinator(
        lambda _: parts[1], parts[2], lambda: True, term_wait_seconds=0.25, kill_wait_seconds=0.25
    )
    try:
        retry = restarted.release("project", plan["port_plan_id"], "release_req_0001")
        assert retry["duplicate"] and retry["requires_new_local_confirmation"]
        assert process.poll() is None
        with pytest.raises(PortError, match="PORT_PLAN_EXPIRED"):
            restarted.confirm_release(plan["port_plan_id"], allow_force=True)
        fresh = restarted.plan_release("project", process.pid, force=True)
        restarted.confirm_release(fresh["port_plan_id"], allow_force=True)
        done = restarted.release("project", fresh["port_plan_id"], "release_req_0002")
        assert done["state"] == "released"
    finally:
        restarted.close()


def test_existing_colink_job_uses_job_cancel_authority(parts, listeners):
    process, _ = listeners()
    _, source, execution, coordinator, _, _ = parts
    execution.live = {"job-owned": {}}
    execution.manager.snapshot = lambda _: {"state": "running", "pid": process.pid}
    execution.store.get_job = lambda _: {"project_id": "project", "source_id": source.source_id}

    def cancel(project, job_id):
        execution.cancellations.append((project, job_id))
        process.terminate()

    execution.cancel = cancel
    plan = coordinator.plan_release("project", process.pid)
    assert not plan["requires_local_confirmation"] and plan["ownership"] == "colink_job"
    result = coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert result["state"] == "released"
    assert execution.cancellations == [("project", "job-owned")]


def test_plan_expiry_and_request_reuse_refuse_without_new_signals(parts, listeners):
    first, _ = listeners()
    second, _ = listeners()
    coordinator = parts[3]
    plan = coordinator.plan_release("project", first.pid)
    other = coordinator.plan_release("project", second.pid)
    coordinator.confirm_release(plan["port_plan_id"])
    coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    with pytest.raises(PortError, match="REQUEST_ID_CONFLICT"):
        coordinator.release("project", other["port_plan_id"], "release_req_0001")
    coordinator.plans[other["port_plan_id"]]["created"] -= 301
    with pytest.raises(PortError, match="PORT_PLAN_EXPIRED"):
        coordinator.confirm_release(other["port_plan_id"])
    assert second.poll() is None


def test_receipts_contain_no_executable_cwd_argv_or_environment(parts, listeners):
    process, _ = listeners()
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid)
    coordinator.confirm_release(plan["port_plan_id"])
    coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    raw = coordinator.db.execute("SELECT data FROM receipts").fetchone()[0]
    assert "cwd" not in raw and "executable" not in raw and "argv" not in raw
    assert (coordinator.root / "ports.sqlite3").stat().st_mode & 0o077 == 0


def test_control_revocation_after_term_prevents_a_following_force_kill(
    parts, listeners, monkeypatch
):
    process, _ = listeners(ignore_term=True)
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid, force=True)
    coordinator.confirm_release(plan["port_plan_id"], allow_force=True)
    original_signal = coordinator.native.signal
    delivered = []

    def revoke_after_term(identity, requested_signal):
        delivered.append(requested_signal)
        original_signal(identity, requested_signal)
        parts[5][0] = False

    monkeypatch.setattr(coordinator.native, "signal", revoke_after_term)
    with pytest.raises(PortError, match="PORT_LOCAL_CONTROL_REQUIRED"):
        coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert delivered == [signal.SIGTERM] and process.poll() is None


def test_old_pid_receipt_becomes_released_without_signaling_reused_pid(
    parts, listeners, monkeypatch
):
    process, _ = listeners(ignore_term=True)
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid)
    coordinator.confirm_release(plan["port_plan_id"])
    coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    real = coordinator.native.inspect

    def reused(pid):
        info = real(pid)
        if pid == process.pid:
            info = {**info, "start_sec": info["start_sec"] + 1}
        return info

    monkeypatch.setattr(coordinator.native, "inspect", reused)
    monkeypatch.setattr(
        os, "kill", lambda *args: pytest.fail("old receipts must never signal a reused PID")
    )
    result = coordinator.release("project", plan["port_plan_id"], "release_req_0001")
    assert result["state"] == "released" and result["process_stopped"]
    assert not result["ports_available"] and process.poll() is None


def test_pending_local_review_contains_exact_process_and_port_without_private_fields(
    parts, listeners
):
    process, port = listeners()
    coordinator = parts[3]
    plan = coordinator.plan_release("project", process.pid, force=True)
    pending = coordinator.pending_confirmations()
    assert len(pending) == 1
    assert pending[0]["port_plan_id"] == plan["port_plan_id"]
    assert pending[0]["pid"] == process.pid and pending[0]["ports"] == [port]
    assert pending[0]["force_requested"] and "cwd" not in pending[0]
    coordinator.confirm_release(plan["port_plan_id"], allow_force=True)
    assert coordinator.pending_confirmations() == []

"""Synthetic host-process tests, using an explicit test-only sandbox checker.

These test lifecycle mechanics. The checker deliberately trusts only the test
Python executable; it is not production OS isolation or a passed sandbox gate.
Every process writes, if at all, only into this run's new temporary directory.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from code_context.execution_process import (
    TERMINAL_STATES,
    ProcessError,
    ProcessManager,
    _CpuWindow,
    _process_table,
)

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX lifecycle")

FAST_GRACE = (0.08, 0.08, 0.3)


def _manager(tmp_path, **extra):
    return ProcessManager(
        sandbox_check=lambda argv, cwd, env: argv[0] == sys.executable,
        termination_grace=FAST_GRACE,
        scope_root=tmp_path / "private-scope",
        **extra,
    )


def _submit(manager, tmp_path, job_id="job", code="print('ready')", **extra):
    return manager.submit(
        job_id,
        [sys.executable, "-c", code],
        tmp_path,
        {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
        **extra,
    )


def _wait(manager, job_id="job", states=TERMINAL_STATES, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        snapshot = manager.snapshot(job_id)
        if snapshot["state"] in states:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(manager.snapshot(job_id))


def test_cpu_window_requires_full_duration_and_preserves_observed_use_across_births():
    budget = _CpuWindow(0.5, 1.0)
    first = (100, "first-birth")
    second = (100, "different-birth")
    assert not budget.sample(0.0, {first: 0})
    assert not budget.sample(0.5, {first: 400_000_000})
    assert budget.sample(1.1, {first: 1_000_000_000})
    assert budget.observed_seconds == pytest.approx(1.0)
    assert not budget.sample(2.2, {second: 10_000_000})
    assert budget.observed_seconds == pytest.approx(1.01)
    assert budget.peak_cores >= 0.8


def test_cpu_threshold_and_window_are_bounded_positive_configuration(tmp_path):
    for args in (
        {"max_cpu_cores": False},
        {"max_cpu_cores": 4.01},
        {"cpu_window_seconds": 30.01},
        {"cpu_window_seconds": 0.01},
        {"resource_sample_seconds": 0.001},
    ):
        with pytest.raises(ProcessError, match="INVALID_EXECUTION_MANAGER_LIMITS"):
            _manager(tmp_path, **args)


def test_real_exit_streams_clean_env_and_closed_descriptors(tmp_path, monkeypatch):
    fd = os.open(tmp_path / "owned-descriptor", os.O_RDWR | os.O_CREAT, 0o600)
    os.set_inheritable(fd, True)
    monkeypatch.setenv("COLINK_SYNTHETIC_PARENT_ONLY", "synthetic")
    code = (
        "import os,sys; "
        "assert 'COLINK_SYNTHETIC_PARENT_ONLY' not in os.environ; "
        "assert os.getsid(0)==os.getpid(); "
        f"\ntry: os.fstat({fd})\nexcept OSError: print('closed')\nelse: raise SystemExit(98)\n"
        "os.write(1,b'\\xe4'); os.write(1,b'\\xb8\\xad'); "
        "os.write(2,b'err'); raise SystemExit(7)"
    )
    callbacks = []
    try:
        with _manager(tmp_path) as manager:
            _submit(manager, tmp_path, code=code, on_exit=callbacks.append)
            done = _wait(manager)
            assert done["state"] == "exited" and done["exit_code"] == 7
            assert done["observed_tree_stopped"]
            assert done["isolation_complete"] is False
            page = manager.read_output("job")
            assert page["eof"]
            assert (
                "".join(c["text"] for c in page["chunks"] if c["stream"] == "stdout")
                == "closed\n中"
            )
            assert "".join(c["text"] for c in page["chunks"] if c["stream"] == "stderr") == "err"
            for _ in range(100):
                if callbacks:
                    break
                time.sleep(0.005)
            assert callbacks[0]["exit_code"] == 7
            manager.ack_output("job", page["next_cursor"])
            assert manager.snapshot("job")["retained_output_bytes"] == 0
            assert manager.read_output("job")["omitted"]
    finally:
        os.close(fd)


def test_timeout_escalates_ignored_interrupt_and_term_to_real_kill(tmp_path):
    code = (
        "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready'); time.sleep(100)"
    )
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, code=code, timeout_seconds=0.25)
        done = _wait(manager)
        assert done["state"] == "timeout"
        assert done["exit_code"] == -signal.SIGKILL
        assert done["observed_tree_stopped"] and not done["isolation_complete"]


def test_cancel_and_parent_control_eof_are_distinct_terminal_states(tmp_path):
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, code="import time; time.sleep(100)", service=True)
        _wait(manager, states={"running"})
        with pytest.raises(ProcessError, match="EXECUTION_OUTPUT_NOT_ACKNOWLEDGEABLE"):
            manager.ack_output("job", 0)
        manager.cancel("job")
        assert _wait(manager)["state"] == "cancelled"
        _submit(manager, tmp_path, "eof", "import time; time.sleep(100)", service=True)
        _wait(manager, "eof", {"running"})
        job = manager._jobs["eof"]
        with job.control_lock:
            job.supervisor.stdin.close()
        done = _wait(manager, "eof")
        assert done["state"] == "interrupted" and done["observed_tree_stopped"]


def test_two_service_one_finite_slots_queue_and_cancel_before_spawn(tmp_path):
    code = "import time; time.sleep(100)"
    with _manager(tmp_path) as manager:
        for job_id in ("service1", "service2", "service3"):
            _submit(manager, tmp_path, job_id, code, service=True)
        for job_id in ("finite1", "finite2"):
            _submit(manager, tmp_path, job_id, code)
        for job_id in ("service1", "service2", "finite1"):
            _wait(manager, job_id, {"running"})
        assert manager.snapshot("service3")["state"] == "queued"
        assert manager.snapshot("finite2")["state"] == "queued"
        queued = manager.cancel("service3")
        assert queued["state"] == "cancelled" and queued["pid"] is None
        manager.cancel("finite1")
        _wait(manager, "finite2", {"running"})
        assert manager.close()["observed_tree_stopped"]


def test_service_touch_renews_idle_lease_then_idle_timeout(tmp_path):
    with _manager(tmp_path, service_idle_seconds=0.3) as manager:
        _submit(manager, tmp_path, code="import time; time.sleep(100)", service=True)
        _wait(manager, states={"running"})
        for _ in range(5):
            time.sleep(0.1)
            manager.touch("job")
        assert manager.snapshot("job")["state"] == "running"
        assert _wait(manager)["state"] == "timeout"
        with pytest.raises(ProcessError, match="EXECUTION_SERVICE_NOT_ACTIVE"):
            manager.touch("job")


def test_sandbox_is_mandatory_revalidated_and_never_falls_back(tmp_path):
    with pytest.raises(TypeError):
        ProcessManager()
    with ProcessManager(sandbox_check=lambda *args: False) as manager:
        with pytest.raises(ProcessError, match="EXECUTION_SANDBOX_REQUIRED"):
            _submit(manager, tmp_path)
    checks = []

    def approved(*args):
        checks.append(True)
        return len(checks) == 1

    with ProcessManager(
        sandbox_check=approved,
        termination_grace=FAST_GRACE,
        scope_root=tmp_path / "private-scope",
    ) as manager:
        _submit(manager, tmp_path)
        done = _wait(manager)
        assert done["state"] == "failed" and done["pid"] is None
        assert done["reason"] == "sandbox_revalidation_failed"


def test_observed_setsid_descendant_is_stopped_and_containment_not_claimed(tmp_path):
    code = (
        "import os,signal,time; pid=os.fork(); "
        "\nif pid==0:\n os.setsid(); signal.signal(signal.SIGINT,signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "open('descendant.pid','w').write(str(os.getpid())); time.sleep(100)"
        "\nelse:\n time.sleep(100)\n"
    )
    pid = None
    try:
        with _manager(tmp_path) as manager:
            _submit(manager, tmp_path, code=code, service=True)
            _wait(manager, states={"running"})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    pid = int((tmp_path / "descendant.pid").read_text())
                    break
                except (FileNotFoundError, ValueError):
                    pass
                time.sleep(0.01)
            assert pid is not None
            # Keep the original parent alive long enough to observe the relation.
            time.sleep(0.2)
            manager.cancel("job")
            done = _wait(manager)
            assert done["state"] == "cancelled" and done["observed_tree_stopped"]
            assert not done["isolation_complete"]
            if sys.platform == "darwin":
                assert done["tree_scope"] == "macos_resource_coalition"
                assert done["cleanup_verified"] is True
            else:
                assert done["tree_scope"] == "linux_subreaper"
    finally:
        # Test-owned child only; retain its PID evidence file and all artifacts.
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_global_budget_ack_terminal_expiry_and_failure_summary_retention(tmp_path):
    now = [100.0]
    with _manager(
        tmp_path,
        global_output_bytes=8192,
        job_output_bytes=4096,
        output_retention_seconds=1,
        summary_retention_seconds=2,
        clock=lambda: now[0],
    ) as manager:
        for number in range(4):
            job_id = f"finite-{number}"
            _submit(manager, tmp_path, job_id, "import os; os.write(1,b'x'*20000)")
            _wait(manager, job_id)
            assert manager.output_usage()["total_bytes"] <= 8192
        assert manager.snapshot("finite-0")["output_reclaimed"] == "global_budget"
        assert manager.read_output("finite-0")["omitted"]
        _submit(
            manager, tmp_path, "failed", "import os; os.write(2,b'e'*20000); raise SystemExit(1)"
        )
        _wait(manager, "failed")
        summaries = manager.failure_summaries()
        assert summaries[-1]["job_id"] == "failed"
        assert sum(len(c["text"].encode()) for c in summaries[-1]["chunks"]) <= 64 * 1024
        assert manager.output_usage()["total_bytes"] <= 8192
        now[0] += 1.1
        assert manager.snapshot("failed")["retained_output_bytes"] == 0
        assert manager.failure_summaries()
        now[0] += 1
        assert manager.failure_summaries() == []


def test_duplicate_job_and_invalid_deadline_are_refused(tmp_path):
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path)
        with pytest.raises(ProcessError, match="EXECUTION_JOB_ALREADY_EXISTS"):
            _submit(manager, tmp_path)
        with pytest.raises(ProcessError, match="INVALID_EXECUTION_PROCESS"):
            _submit(manager, tmp_path, "invalid", timeout_seconds=301)
        _wait(manager)


def test_actual_owner_exit_closes_supervision_pipe_and_stops_child(tmp_path):
    runner = tmp_path / "synthetic-owner.py"
    runner.write_text(
        "import os,sys,time\n"
        "from code_context.execution_process import ProcessManager\n"
        "m=ProcessManager(sandbox_check=lambda *args: True, "
        "scope_root=os.path.join(os.getcwd(),'private-scope'), "
        "termination_grace=(0.08,0.08,0.3))\n"
        "m.submit('owned',[sys.executable,'-c','import time; time.sleep(100)'],"
        "os.getcwd(),{'PYTHONUTF8':'1'},service=True)\n"
        "deadline=time.monotonic()+5\n"
        "while time.monotonic()<deadline:\n"
        " s=m.snapshot('owned')\n"
        " if s['state']=='running':\n"
        "  print(s['pid'],m._jobs['owned'].supervisor.pid,flush=True); os._exit(0)\n"
        " time.sleep(0.01)\n"
        "raise SystemExit(2)\n"
    )
    owner = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(runner)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONUTF8": "1"},
    )
    raw, _ = owner.communicate(timeout=8)
    assert owner.returncode == 0
    pids = [int(value) for value in raw.split()]
    assert len(pids) == 2
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        table = _process_table()
        if all(pid not in table or table[pid][3].startswith("Z") for pid in pids):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("test-owned supervisor or command survived owner EOF")


def test_finite_command_leaving_a_descendant_fails_and_blocks_new_launch(tmp_path):
    code = (
        "import os,time; pid=os.fork()\n"
        "if pid==0:\n os.setsid(); time.sleep(100)\n"
        "else:\n open('owned.pid','w').write(str(pid)); time.sleep(0.3)\n"
    )
    pid = None
    try:
        with _manager(tmp_path) as manager:
            _submit(manager, tmp_path, code=code)
            done = _wait(manager)
            pid = int((tmp_path / "owned.pid").read_text())
            assert done["state"] == "stop_failed"
            assert done["reason"] == "descendants_after_exit"
            assert done["isolation_complete"] is False
            with pytest.raises(ProcessError, match="EXECUTION_MANAGER_UNAVAILABLE"):
                _submit(manager, tmp_path, "refused")
            assert manager.close()["failed_jobs"] == ["job"]
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_completion_callback_failure_blocks_additional_execution(tmp_path):
    def unavailable(_):
        raise RuntimeError("synthetic journal unavailable")

    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, on_exit=unavailable)
        _wait(manager)
        deadline = time.monotonic() + 1
        while not manager.snapshot("job")["callback_error"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.snapshot("job")["callback_error"]
        with pytest.raises(ProcessError, match="EXECUTION_MANAGER_UNAVAILABLE"):
            _submit(manager, tmp_path, "refused")

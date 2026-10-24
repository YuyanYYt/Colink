"""Idle scheduling contracts with synthetic jobs in independent temporary roots.

No real command, user workspace, service lease or OS process tree is changed.
Existing process tests exercise the actual supervision and lease behavior.
"""

import sys
import threading
import time
from types import SimpleNamespace

import pytest

import code_context.terminal as terminal_module
from code_context.execution_process import ProcessManager
from code_context.source_access import SourceError
from code_context.terminal import HostTerminal

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX lifecycle")


class _WaitTrace:
    def __init__(self):
        self.condition = threading.Condition()
        self.timeouts = []

    def wrap(self, wait):
        def traced(timeout=None):
            with self.condition:
                self.timeouts.append(timeout)
                self.condition.notify_all()
            return wait(timeout)

        return traced

    def until(self, predicate, timeout=2):
        with self.condition:
            assert self.condition.wait_for(lambda: predicate(self.timeouts), timeout)
            return tuple(self.timeouts)


class _SyntheticManager(ProcessManager):
    def __init__(self, tmp_path, *, cleanup=True, **kwargs):
        self.wait_trace = _WaitTrace()
        self.started = threading.Event()
        self.release = threading.Event()
        self.cleanup = cleanup
        super().__init__(sandbox_check=lambda *_: True, scope_root=tmp_path / "scope", **kwargs)

    def _dispatch(self):
        self._condition.wait = self.wait_trace.wrap(self._condition.wait)
        super()._dispatch()

    def _run(self, job):
        with self._condition:
            job.state = "running"
            self._condition.notify_all()
        self.started.set()
        self.release.wait()
        with self._condition:
            self._finish_locked(
                job,
                "exited",
                exit_code=0,
                cleanup_verified=self.cleanup,
                observed_tree_stopped=self.cleanup,
            )
        self._notify_exit(job)

    def close(self):
        self.release.set()
        return super().close()


def _submit(manager, tmp_path, job_id="synthetic", **kwargs):
    return manager.submit(job_id, [sys.executable], tmp_path, {}, **kwargs)


def test_dispatcher_blocks_without_work_and_wakes_for_submit_finish_and_close(tmp_path):
    manager = _SyntheticManager(tmp_path)
    try:
        assert manager.wait_trace.until(bool) == (None,)
        time.sleep(0.32)
        assert manager.wait_trace.timeouts == [None]
        _submit(manager, tmp_path)
        assert manager.started.wait(2)
        manager.wait_trace.until(lambda values: 0.1 in values)
        manager.release.set()
        manager.wait_trace.until(lambda values: values.count(None) == 2)
        previous = tuple(manager.wait_trace.timeouts)
        time.sleep(0.32)
        assert tuple(manager.wait_trace.timeouts) == previous
    finally:
        result = manager.close()
    assert result["observed_tree_stopped"]
    assert not manager._dispatcher.is_alive()


def test_activity_observer_runs_without_manager_lock_and_protects_exit_callback(tmp_path):
    callback_entered, callback_release = threading.Event(), threading.Event()
    activities = []
    with _SyntheticManager(tmp_path) as manager:
        manager.on_activity = lambda: activities.append(
            (manager._condition._is_owned(), manager.active_count())
        )

        def on_exit(_):
            callback_entered.set()
            assert callback_release.wait(2)

        _submit(manager, tmp_path, on_exit=on_exit)
        assert manager.active_count() == 1
        assert activities == [(False, 1)]
        manager.release.set()
        assert callback_entered.wait(2)
        assert manager.snapshot("synthetic")["state"] == "exited"
        assert manager.active_count() == 1
        callback_release.set()
        with manager._condition:
            assert manager._condition.wait_for(lambda: manager._jobs["synthetic"].callback_done, 2)
        deadline = time.monotonic() + 2
        while len(activities) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert activities == [(False, 1), (False, 0)]


def test_dispatcher_preserves_output_retention_then_returns_to_indefinite_wait(tmp_path):
    now = [100.0]
    with _SyntheticManager(tmp_path, clock=lambda: now[0]) as manager:
        manager.wait_trace.until(bool)
        _submit(manager, tmp_path)
        assert manager.started.wait(2)
        with manager._condition:
            manager._jobs["synthetic"].output.append("stdout", b"synthetic output")
        manager.release.set()
        with manager._condition:
            assert manager._condition.wait_for(lambda: manager._jobs["synthetic"].callback_done, 2)
        assert manager.snapshot("synthetic")["retained_output_bytes"] > 0
        manager.wait_trace.until(lambda values: 0.1 in values)
        with manager._condition:
            now[0] += 601
            manager._condition.notify_all()
        manager.wait_trace.until(lambda values: values.count(None) == 2)
        done = manager.snapshot("synthetic")
        assert done["retained_output_bytes"] == 0
        assert done["output_reclaimed"] == "terminal_retention_expired"


@pytest.mark.parametrize("cleanup,callback_error", [(False, False), (True, True)])
def test_unverified_cleanup_or_failed_exit_callback_remains_busy(tmp_path, cleanup, callback_error):
    with _SyntheticManager(tmp_path, cleanup=cleanup) as manager:

        def on_exit(_):
            if callback_error:
                raise RuntimeError("synthetic completion failure")

        _submit(manager, tmp_path, on_exit=on_exit)
        manager.release.set()
        with manager._condition:
            assert manager._condition.wait_for(lambda: manager._jobs["synthetic"].callback_done, 2)
        assert manager.active_count() == 1


def test_queued_activity_count_never_collects_outputs_or_renews_service(tmp_path, monkeypatch):
    with _SyntheticManager(tmp_path, max_services=1) as manager:
        _submit(manager, tmp_path, "running", service=True)
        assert manager.started.wait(2)
        _submit(manager, tmp_path, service=True)
        assert manager.snapshot("synthetic")["state"] == "queued"

        def forbidden(*_):
            pytest.fail("occupancy read must not collect or renew")

        with monkeypatch.context() as patch:
            # The queue is locked only to keep this assertion independent from
            # its unrelated timed log-collection loop.
            with manager._condition:
                patch.setattr(manager, "_collect_locked", forbidden)
                patch.setattr(manager, "_control", forbidden)
                assert manager.active_count() == 2
        manager.cancel("synthetic")
        assert manager.active_count() == 1
        assert manager.service_idle_seconds == 1200


def test_advisory_observer_failure_does_not_block_process_lifecycle(tmp_path):
    with _SyntheticManager(tmp_path) as manager:

        def unavailable():
            raise RuntimeError("synthetic cache observer unavailable")

        manager.on_activity = unavailable
        _submit(manager, tmp_path)
        manager.release.set()
        with manager._condition:
            assert manager._condition.wait_for(lambda: manager._jobs["synthetic"].callback_done, 2)
        assert manager.active_count() == 0
        assert not manager._blocked


class _TerminalManager:
    def __init__(self, *, sandbox_check, scope_root, on_activity=None):
        self.jobs = {}
        self.cancelled = threading.Event()
        self.on_activity = on_activity

    def submit(self, job_id, *_args, on_exit, **_kwargs):
        self.jobs[job_id] = {"state": "running", "service": False, "on_exit": on_exit}
        if self.on_activity:
            self.on_activity()

    def snapshot(self, job_id):
        job = self.jobs[job_id]
        return {"job_id": job_id, "state": job["state"], "service": job["service"]}

    def cancel(self, job_id):
        job = self.jobs[job_id]
        job["state"] = "cancelled"
        job["on_exit"](self.snapshot(job_id))
        self.cancelled.set()

    def close(self):
        return {"observed_tree_stopped": True}


def test_terminal_monitor_blocks_idle_then_preserves_grant_checks_and_close_wakeup(
    tmp_path, monkeypatch
):
    trace, checks, grant = _WaitTrace(), [], ["synthetic-grant"]
    original = HostTerminal._monitor

    def traced_monitor(self):
        self.condition.wait = trace.wrap(self.condition.wait)
        original(self)

    def authorize(_):
        checks.append(time.monotonic())
        if grant[0] is None:
            raise SourceError("DEVELOPMENT_NOT_AUTHORIZED")
        return grant[0]

    monkeypatch.setattr(HostTerminal, "_monitor", traced_monitor)
    monkeypatch.setattr(terminal_module, "ProcessManager", _TerminalManager)
    activity = threading.Event()
    source = SimpleNamespace(root=tmp_path, ensure_available=lambda: None)
    terminal = HostTerminal(
        tmp_path / "terminal",
        lambda _: source,
        authorize,
        lambda _: None,
        {},
    )
    # Workspace attaches its energy scheduler after both managers exist.
    terminal.on_activity = activity.set
    try:
        assert trace.until(bool) == (None,)
        time.sleep(0.32)
        assert trace.timeouts == [None]
        assert checks == []
        terminal.start("synthetic", "request", [sys.executable], tty=False, service=False)
        assert activity.wait(1)
        trace.until(lambda values: 0.25 in values)
        checked_before_revoke = len(checks)
        grant[0] = None
        assert terminal.manager.cancelled.wait(0.5)
        assert len(checks) > checked_before_revoke
        trace.until(lambda values: values.count(None) == 2)
        previous = tuple(trace.timeouts)
        time.sleep(0.32)
        assert tuple(trace.timeouts) == previous
    finally:
        assert terminal.close()["observed_tree_stopped"]
    assert not terminal.monitor.is_alive()

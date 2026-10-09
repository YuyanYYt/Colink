"""Exercise the real idle waiter with a controlled clock and isolated MCP calls."""

import asyncio
import threading
import time
from contextlib import contextmanager

import pytest

from code_context.energy_idle import EnergyIdle, continuous_clock


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("idle worker did not reach the expected state")
        threading.Event().wait(0.005)


class ManualClock:
    def __init__(self):
        self.value = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def set(self, value):
        with self.lock:
            self.value = float(value)


class Harness:
    def __init__(self, reasons=()):
        self.clock = ManualClock()
        self.lock = threading.Lock()
        self.reasons = list(reasons)
        self.fail_provider = False
        self.providers = 0
        self.trims = 0
        self.activity = []
        self.energy = EnergyIdle(self.busy, self.trim, self.active, clock=self.clock)
        wait_for(lambda: bool(self.activity))
        if not reasons:
            wait_for(lambda: self.trims == 1)

    def busy(self):
        with self.lock:
            self.providers += 1
            if self.fail_provider:
                raise RuntimeError("synthetic unavailable activity observer")
            return list(self.reasons)

    def trim(self):
        with self.lock:
            self.trims += 1

    def active(self, value):
        with self.lock:
            self.activity.append(value)

    def advance(self, value):
        self.clock.set(value)
        self.energy.changed()

    def state(self, value):
        wait_for(lambda: self.energy.status()["power_state"] == value)


@pytest.fixture
def idle():
    harness = Harness()
    yield harness
    harness.energy.close()
    assert not harness.energy.thread.is_alive()


def test_successful_requests_slide_five_minutes_without_accumulating(idle):
    with idle.energy.request():
        pass
    idle.state("warm")
    assert idle.energy.deadline == 300
    idle.advance(250)
    assert idle.energy.status()["warm_remaining"] == 50
    with idle.energy.request():
        with idle.energy.request():
            pass
    idle.state("warm")
    assert idle.energy.deadline == 550
    assert idle.energy.status()["warm_remaining"] == 300
    idle.advance(549)
    assert idle.energy.status()["warm_remaining"] == 1
    idle.advance(550)
    idle.state("idle")
    wait_for(lambda: idle.trims == 2)


def test_long_request_stays_pinned_and_warms_from_response_time(idle):
    with idle.energy.request():
        idle.state("busy")
        idle.advance(900)
        assert idle.energy.status()["power_state"] == "busy"
        assert idle.energy.status()["busy_reasons"] == ["requests"]
        assert idle.trims == 1
        assert idle.energy.deadline == 0
    idle.state("warm")
    assert idle.energy.deadline == 1200
    assert idle.energy.status()["warm_remaining"] == 300


def test_rejected_request_releases_its_pin_without_renewing_a_warm_window(idle):
    with idle.energy.request():
        pass
    idle.state("warm")
    idle.advance(250)
    with pytest.raises(ValueError, match="synthetic rejection"), idle.energy.request():
        idle.state("busy")
        raise ValueError("synthetic rejection")
    idle.state("warm")
    assert idle.energy.requests == 0
    assert idle.energy.deadline == 300
    idle.advance(300)
    idle.state("idle")


def test_silent_job_is_protected_and_completion_starts_a_fresh_window():
    idle = Harness(["jobs"])
    try:
        idle.state("busy")
        for now in (300, 600, 1200):
            before = idle.providers
            idle.advance(now)
            wait_for(lambda observed=before: idle.providers > observed)
            assert idle.energy.status()["busy_reasons"] == ["jobs"]
            assert idle.trims == 0
        with idle.lock:
            idle.reasons.clear()
        idle.energy.changed()
        idle.state("warm")
        assert idle.energy.deadline == 1500
        assert idle.energy.status()["warm_remaining"] == 300
        idle.advance(1500)
        idle.state("idle")
        wait_for(lambda: idle.trims == 1)
    finally:
        idle.energy.close()


def test_unavailable_busy_provider_protects_work_and_does_not_poll(idle):
    with idle.lock:
        idle.fail_provider = True
    idle.energy.changed()
    idle.state("busy")
    assert idle.energy.status()["busy_reasons"] == ["activity_unknown"]
    before = idle.providers
    idle.advance(1200)
    wait_for(lambda: idle.providers > before)
    observed = idle.providers
    threading.Event().wait(0.05)
    assert idle.providers == observed
    assert idle.trims == 1
    with idle.lock:
        idle.fail_provider = False
    idle.energy.changed()
    idle.state("warm")
    assert idle.energy.deadline == 1500


def test_request_entering_during_deadline_observation_discards_stale_idle_decision(idle):
    with idle.energy.request():
        pass
    idle.state("warm")
    entered, release = threading.Event(), threading.Event()
    original = idle.energy.busy_provider

    def blocked_provider():
        entered.set()
        assert release.wait(2)
        return original()

    idle.energy.busy_provider = blocked_provider
    idle.advance(300)
    assert entered.wait(2)
    try:
        with idle.energy.request():
            release.set()
            idle.state("busy")
            assert idle.trims == 1
    finally:
        release.set()
    idle.state("warm")
    assert idle.energy.deadline == 600


def test_maintenance_claim_finishes_before_a_new_request_and_keeps_condition_free(idle):
    with idle.energy.request():
        pass
    idle.state("warm")
    maintenance, release = threading.Event(), threading.Event()
    admitted, complete = threading.Event(), threading.Event()
    active = idle.energy.set_active

    def block_inactive(value):
        if not value:
            maintenance.set()
            assert release.wait(2)
        active(value)

    def request():
        with idle.energy.request():
            admitted.set()
            complete.wait(2)

    idle.energy.set_active = block_inactive
    idle.advance(300)
    assert maintenance.wait(2)
    thread = threading.Thread(target=request)
    thread.start()
    try:
        assert not admitted.wait(0.05)
        # Holding a transition claim must not hold the condition lock.
        assert idle.energy.condition.acquire(timeout=0.5)
        idle.energy.condition.release()
        release.set()
        assert admitted.wait(2)
        idle.state("busy")
        assert idle.trims == 2
    finally:
        release.set()
        complete.set()
        thread.join(2)
    assert not thread.is_alive()
    idle.state("warm")


def test_provider_and_maintenance_callbacks_run_outside_the_condition_lock():
    ready, done = threading.Event(), threading.Event()
    holder, checks = {}, []
    check_lock = threading.Lock()

    def check(name):
        assert ready.wait(2)
        acquired = threading.Event()

        def acquire():
            with holder["energy"].condition:
                acquired.set()

        thread = threading.Thread(target=acquire)
        thread.start()
        free = acquired.wait(0.5)
        with check_lock:
            checks.append((name, free))
            if len(checks) >= 3:
                done.set()
        thread.join(1)

    def provider():
        check("provider")
        return []

    energy = EnergyIdle(provider, lambda: check("trim"), lambda _: check("active"))
    holder["energy"] = energy
    ready.set()
    try:
        assert done.wait(3)
        assert {name for name, _ in checks} == {"provider", "active", "trim"}
        assert all(free for _, free in checks)
    finally:
        energy.close()


def test_close_waits_for_claimed_maintenance_then_cannot_be_revived(idle):
    with idle.energy.request():
        pass
    idle.state("warm")
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    trim = idle.energy.trim_idle

    def blocked_trim():
        entered.set()
        assert release.wait(2)
        trim()

    def close():
        idle.energy.close()
        closed.set()

    idle.energy.trim_idle = blocked_trim
    idle.advance(300)
    assert entered.wait(2)
    thread = threading.Thread(target=close)
    thread.start()
    try:
        assert not closed.wait(0.05)
    finally:
        release.set()
        thread.join(2)
    assert closed.is_set() and not idle.energy.thread.is_alive()
    before = (idle.providers, idle.trims, list(idle.activity), idle.energy.deadline)
    idle.advance(1200)
    with idle.energy.request():
        pass
    assert idle.energy.status()["power_state"] == "closed"
    assert (idle.providers, idle.trims, idle.activity, idle.energy.deadline) == before


def test_maintenance_failure_is_reported_without_disabling_business(idle):
    with idle.energy.request():
        pass
    idle.state("warm")

    def failure():
        raise RuntimeError("synthetic cache maintenance failure")

    idle.energy.trim_idle = failure
    idle.advance(300)
    idle.state("idle")
    wait_for(lambda: idle.energy.status()["idle_trim_failed"])
    with idle.energy.request():
        pass
    idle.state("warm")
    wait_for(lambda: not idle.energy.status()["idle_trim_failed"])
    assert idle.energy.thread.is_alive()


def test_native_continuous_clock_is_positive_and_monotonic():
    clock = continuous_clock()
    first, second = clock(), clock()
    assert isinstance(first, float)
    assert 0 < first <= second


@contextmanager
def mirror_mcp(tmp_path, idle):
    from code_context.models import FileChange, SyncBatch, content_hash
    from code_context.server import build_mcp
    from code_context.storage import MirrorStore

    store = MirrorStore(tmp_path / "mirror.sqlite3")
    store.apply(
        "sample",
        SyncBatch(
            request_id="initial",
            base_revision=0,
            mode="full",
            changes=[
                FileChange(op="upsert", path="a.py", content="x=1\n", sha256=content_hash("x=1\n"))
            ],
        ),
    )
    server = build_mcp(store)
    server._energy_idle = idle.energy
    yield server


def test_mcp_health_probe_does_not_renew_business_window(tmp_path, idle):
    with mirror_mcp(tmp_path, idle) as server:
        asyncio.run(server.call_tool("connection_status", {}))
        assert idle.energy.deadline == 0
        with idle.energy.request():
            pass
        idle.state("warm")
        idle.advance(250)
        asyncio.run(server.call_tool("connection_status", {}))
        assert idle.energy.deadline == 300


def test_mcp_business_read_warms_cache_but_rejection_does_not(tmp_path, idle):
    from mcp.server.mcpserver.exceptions import ToolError

    with mirror_mcp(tmp_path, idle) as server:
        result = asyncio.run(
            server.call_tool("read_file", {"project_id": "sample", "path": "a.py"})
        )
        assert result is not None
        idle.state("warm")
        assert idle.energy.deadline == 300
        idle.advance(250)
        with pytest.raises(ToolError):
            asyncio.run(server.call_tool("read_file", {"project_id": "missing", "path": "a.py"}))
        with pytest.raises(ToolError, match="refresh"):
            asyncio.run(
                server.call_tool(
                    "read_file", {"project_id": "sample", "path": "a.py", "revision": 1}
                )
            )
        assert idle.energy.requests == 0
        assert idle.energy.deadline == 300

"""Soft cache lifetime, independent of grants, connection and job deadlines.

One condition waiter owns the deadline. Observers only wake it; provider and
maintenance callbacks run outside its lock. Nothing here cancels a process,
changes authorization, deletes persistent data or starts another connection.
"""

import ctypes
import math
import sys
import threading
import time
from contextlib import contextmanager


def continuous_clock():
    """Return a monotonic seconds callable including system sleep on macOS."""
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")

        class Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        info = Timebase()
        library.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
        library.mach_timebase_info.restype = ctypes.c_int
        if library.mach_timebase_info(ctypes.byref(info)) or not info.denom:
            raise RuntimeError("continuous clock unavailable")
        library.mach_continuous_time.argtypes = []
        library.mach_continuous_time.restype = ctypes.c_uint64
        factor = info.numer / info.denom / 1_000_000_000
        return lambda: library.mach_continuous_time() * factor
    if hasattr(time, "CLOCK_BOOTTIME"):
        return lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic


class EnergyIdle:
    def __init__(self, busy_provider, trim_idle, set_active, *, window_seconds=300, clock=None):
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("invalid idle window")
        self.window = window_seconds
        self.clock = clock or continuous_clock()
        self.busy_provider = busy_provider
        self.trim_idle = trim_idle
        self.set_active = set_active
        self.condition = threading.Condition()
        self.transition = threading.RLock()
        self.requests = 0
        self.deadline = 0.0
        self.generation = 0
        self.closed = False
        self.state = "idle"
        self.reasons = []
        self.trim_failed = False
        self.thread = threading.Thread(target=self._run, name="colink-idle", daemon=True)
        self.thread.start()

    def changed(self):
        with self.condition:
            if not self.closed:
                self.generation += 1
                self.condition.notify_all()

    @contextmanager
    def request(self):
        # Pin even requests later rejected by the existing business checks.
        # They do not warm the cache unless normal processing succeeds.
        with self.transition:
            with self.condition:
                pinned = not self.closed
                if pinned:
                    self.requests += 1
                    self.generation += 1
                    self.condition.notify_all()
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            with self.condition:
                if pinned:
                    self.requests -= 1
                    if succeeded and not self.closed:
                        self.deadline = max(self.deadline, self.clock() + self.window)
                    self.generation += 1
                    self.condition.notify_all()

    def status(self):
        with self.condition:
            remaining = max(0.0, min(self.window, self.deadline - self.clock()))
            # A continuous clock also expires across suspend. Wake a waiter
            # whose platform condition timeout did not include that sleep.
            if not self.closed and self.state == "warm" and not remaining:
                self.generation += 1
                self.condition.notify_all()
            return {
                "power_state": "closed" if self.closed else self.state,
                "warm_remaining": remaining,
                "busy_reasons": list(self.reasons),
                "idle_window_seconds": self.window,
                "idle_trim_failed": self.trim_failed,
            }

    def _run(self):
        seen = -1
        observed_busy = False
        previous_state = None
        timeout = None
        while True:
            with self.condition:
                if self.generation == seen and not self.closed:
                    self.condition.wait(timeout)
                if self.closed:
                    return
                generation = self.generation
            try:
                reasons = list(self.busy_provider())
            except Exception:
                # Unknown work is protected; an observer cannot end a job.
                reasons = ["activity_unknown"]
            with self.condition:
                if self.closed:
                    return
                if generation != self.generation:
                    continue
                now = self.clock()
                busy = bool(reasons)
                if observed_busy and not busy:
                    self.deadline = max(self.deadline, now + self.window)
                observed_busy = busy
                if self.requests:
                    reasons.append("requests")
                self.reasons = reasons
                state = "busy" if reasons else "warm" if now < self.deadline else "idle"
                self.state = state
                timeout = self.deadline - now if state == "warm" else None
                seen = generation
            if state != previous_state:
                with self.transition:
                    with self.condition:
                        if self.closed:
                            return
                        if generation != self.generation:
                            continue
                    try:
                        self.set_active(state != "idle")
                        if state == "idle":
                            self.trim_idle()
                        self.trim_failed = False
                    except Exception:
                        # Retain data/resources on maintenance failure. Business
                        # operations and the existing shutdown path remain usable.
                        self.trim_failed = True
                    previous_state = state

    def close(self):
        with self.transition:
            with self.condition:
                self.closed = True
                self.condition.notify_all()
        if threading.current_thread() is not self.thread:
            self.thread.join()

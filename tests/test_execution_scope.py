"""Native coalition lifecycle fixtures, not proof of the project sandbox.

Every launchd job is ephemeral and belongs to a new private fixture directory.
Only that job's fixed kernel coalition is signalled. Evidence is retained.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_context import execution_scope as native_scope
from code_context.execution_process import TERMINAL_STATES, ProcessManager
from code_context.execution_scope import (
    CoalitionMember,
    CoalitionScope,
    LaunchdSupervisor,
    MacKernel,
    ProcessIdentity,
    ScopeError,
    expire_verified_job_scope,
    index_retired_job_scopes,
    verify_retired_job_scope,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native macOS coalition")
GRACE = (0.08, 0.08, 0.3)


def _manager(tmp_path, **extra):
    return ProcessManager(
        sandbox_check=lambda argv, cwd, env: argv[0] == sys.executable,
        scope_root=tmp_path / "private-scope",
        termination_grace=GRACE,
        **extra,
    )


def _submit(manager, tmp_path, code, **extra):
    return manager.submit(
        "native",
        [sys.executable, "-I", "-S", "-X", "utf8", "-c", code],
        tmp_path,
        {"PYTHONUTF8": "1"},
        **extra,
    )


def _wait(manager, states=TERMINAL_STATES, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = manager.snapshot("native")
        if result["state"] in states:
            return result
        time.sleep(0.01)
    raise AssertionError(manager.snapshot("native"))


def _pid_file(tmp_path):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        path = tmp_path / "doublefork.pid"
        if path.exists():
            try:
                return int(path.read_text())
            except ValueError:
                pass
        time.sleep(0.01)
    raise AssertionError("synthetic daemon did not write its retained PID evidence")


def _daemon_code(leader_exit=False):
    child = (
        "import json,os,signal,time; "
        "assert 'PYTHONUTF8' not in os.environ; "
        "open('daemon-environment.json','w').write(json.dumps(sorted(os.environ))); "
        "signal.signal(signal.SIGINT,signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "open('doublefork.pid','w').write(str(os.getpid())); time.sleep(100)"
    )
    return (
        "import os,signal,sys,time\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "first=os.fork()\n"
        "if first==0:\n"
        " os.setsid()\n"
        " if os.fork(): os._exit(0)\n"
        " for fd in (0,1,2): os.close(fd)\n"
        " null=os.open(os.devnull,os.O_RDWR)\n"
        " for fd in (0,1,2):\n"
        "  if null!=fd: os.dup2(null,fd)\n"
        " if null>2: os.close(null)\n"
        " os.environ.clear()\n"
        f" os.execve(sys.executable,['changed-name','-I','-S','-X','utf8','-c',{child!r}],"
        "{'PATH':'/usr/bin:/bin'})\n"
        + ("else: time.sleep(0.5)\n" if leader_exit else "else: time.sleep(100)\n")
    )


def _record(tmp_path, result):
    (tmp_path / "result.json").write_text(json.dumps(result, indent=2))


def test_supervisor_interpreter_does_not_write_adjacent_loaded_module_bytecode(tmp_path):
    module = tmp_path / "execution_scope.py"
    module.write_text("VALUE = 42\n")
    helper = tmp_path / "execution_process.py"
    helper.write_text(
        "import importlib.util,json,sys\n"
        "from pathlib import Path\n"
        "spec=importlib.util.spec_from_file_location('colink_execution_scope',"
        "Path(__file__).with_name('execution_scope.py'))\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name]=module\n"
        "spec.loader.exec_module(module)\n"
        "print(json.dumps({'value':module.VALUE,"
        "'dont_write_bytecode':sys.dont_write_bytecode}))\n"
    )
    plist = native_scope._supervisor_plist(tmp_path, helper)
    completed = subprocess.run(
        plist["ProgramArguments"],
        cwd=plist["WorkingDirectory"],
        env=plist["EnvironmentVariables"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert json.loads(completed.stdout) == {"value": 42, "dont_write_bytecode": True}
    assert not (tmp_path / "__pycache__").exists()
    assert not list(tmp_path.rglob("*.pyc"))
    # A negative control proves the directory is writable and this loader
    # really generates a cache if the production launch stops passing -B.
    control = subprocess.run(
        [argument for argument in plist["ProgramArguments"] if argument != "-B"],
        cwd=plist["WorkingDirectory"],
        env=plist["EnvironmentVariables"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert json.loads(control.stdout) == {"value": 42, "dont_write_bytecode": False}
    assert list((tmp_path / "__pycache__").glob("execution_scope.*.pyc"))


def test_fast_doublefork_setsid_close_stdio_clear_env_exec_is_owned_until_timeout(tmp_path):
    kernel = MacKernel()
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, _daemon_code(), timeout_seconds=1)
        running = _wait(manager, {"running"})
        pid = _pid_file(tmp_path)
        assert kernel.resource_id(pid) == running["resource_id"]
        assert kernel.resource_id(os.getpid()) != running["resource_id"]
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "timeout"
        assert done["cleanup_verified"] and done["observed_tree_stopped"]
        assert not done["isolation_complete"]
        assert kernel.members(done["resource_id"]) == {}
        assert kernel.identity(pid) is None


def test_two_execing_service_children_stay_observed_until_cancel(tmp_path):
    child = (
        "import os,sys; "
        "os.execv(sys.executable,[sys.executable,'-c','import time;time.sleep(100)'])"
    )
    code = (
        "import subprocess,sys,time\n"
        f"child={child!r}\n"
        "subprocess.Popen([sys.executable,'-c',child])\n"
        "subprocess.Popen([sys.executable,'-c',child])\n"
        "print('children started',flush=True)\n"
        "time.sleep(100)\n"
    )
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, code, service=True)
        running = _wait(manager, {"running"})
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            assert manager.snapshot("native")["state"] == "running"
            time.sleep(0.01)
        manager.cancel("native")
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "cancelled"
        assert done["resource_peak_processes"] >= 3
        assert done["cleanup_verified"] and done["observed_tree_stopped"]
        assert MacKernel().members(running["resource_id"]) == {}


def test_leader_exit_with_unannounced_daemon_is_a_failure_after_full_cleanup(tmp_path):
    kernel = MacKernel()
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, _daemon_code(leader_exit=True))
        pid = _pid_file(tmp_path)
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "stop_failed"
        assert done["reason"] == "descendants_after_exit"
        assert done["cleanup_verified"]
        assert kernel.members(done["resource_id"]) == {}
        assert kernel.identity(pid) is None


def test_helper_forced_exit_is_detected_and_parent_cleans_fixed_coalition(tmp_path):
    kernel = MacKernel()
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, _daemon_code(), service=True)
        _wait(manager, {"running"})
        pid = _pid_file(tmp_path)
        helper = manager._jobs["native"].supervisor
        member = helper.scope.scan(False)[helper.pid]
        kernel.signal_member(member, signal.SIGKILL)
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "stop_failed"
        assert done["cleanup_verified"]
        assert kernel.members(done["resource_id"]) == {}
        assert kernel.identity(pid) is None


def test_control_eof_stops_closed_stdio_daemon_and_unloads_helper(tmp_path):
    kernel = MacKernel()
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, _daemon_code(), service=True)
        _wait(manager, {"running"})
        pid = _pid_file(tmp_path)
        helper = manager._jobs["native"].supervisor
        helper.stdin.close()
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "interrupted" and done["cleanup_verified"]
        assert kernel.identity(helper.pid) is None and kernel.identity(pid) is None
        assert kernel.members(done["resource_id"]) == {}


def test_stopped_command_is_really_killed_on_timeout(tmp_path):
    kernel = MacKernel()
    with _manager(tmp_path) as manager:
        code = (
            "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(100)"
        )
        _submit(manager, tmp_path, code, timeout_seconds=1)
        running = _wait(manager, {"running"})
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if manager.read_output("native")["chunks"]:
                break
            time.sleep(0.01)
        assert manager.read_output("native")["chunks"]
        member = kernel.members(running["resource_id"])[running["pid"]]
        kernel.signal_member(member, signal.SIGSTOP)
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "timeout" and done["exit_code"] == -signal.SIGKILL
        assert done["cleanup_verified"]
        assert kernel.members(done["resource_id"]) == {}


def test_bounded_forking_fixture_triggers_soft_process_budget_and_is_collected(tmp_path):
    code = (
        "import os,signal,time\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "for number in range(32):\n"
        " pid=os.fork()\n"
        " if pid==0:\n  os.setsid(); time.sleep(100); os._exit(0)\n"
        " time.sleep(.01)\n"
        "time.sleep(100)\n"
    )
    with _manager(tmp_path, max_processes=6) as manager:
        _submit(manager, tmp_path, code, timeout_seconds=3)
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "resource_limit" and done["reason"] == "process_limit"
        assert 6 < done["resource_peak_processes"] <= 33
        assert done["resource_limits_soft"] and done["cleanup_verified"]
        assert MacKernel().members(done["resource_id"]) == {}


def test_rss_budget_stops_real_resident_allocation(tmp_path):
    with _manager(tmp_path, max_rss_bytes=32 * 1024 * 1024) as manager:
        _submit(manager, tmp_path, "import time; memory=bytearray(64*1024*1024); time.sleep(100)")
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "resource_limit" and done["reason"] == "rss_limit"
        assert done["resource_peak_rss_bytes"] > 32 * 1024 * 1024
        assert done["cleanup_verified"]
        assert MacKernel().members(done["resource_id"]) == {}


def test_real_busy_forked_processes_trigger_soft_cpu_window_and_entire_scope_is_collected(tmp_path):
    code = (
        "import os,signal\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "for number in range(2):\n"
        " if os.fork()==0:\n"
        "  os.setsid()\n"
        "  while True: pass\n"
        "while True: pass\n"
    )
    with _manager(
        tmp_path, max_cpu_cores=0.2, cpu_window_seconds=0.4, resource_sample_seconds=0.05
    ) as manager:
        _submit(manager, tmp_path, code, timeout_seconds=3)
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "resource_limit" and done["reason"] == "cpu_limit"
        assert done["resource_peak_cpu_cores"] > 0.2
        assert done["resource_cpu_seconds_observed"] > 0.08
        assert done["resource_peak_processes"] == 3
        assert done["resource_limits_soft"] and done["cleanup_verified"]
        assert MacKernel().members(done["resource_id"]) == {}


def test_sleeping_process_cpu_remains_under_threshold_until_its_real_timeout(tmp_path):
    with _manager(
        tmp_path, max_cpu_cores=0.2, cpu_window_seconds=0.4, resource_sample_seconds=0.05
    ) as manager:
        _submit(manager, tmp_path, "import time;time.sleep(100)", timeout_seconds=1)
        done = _wait(manager)
        _record(tmp_path, done)
        assert done["state"] == "timeout" and done["reason"] == "timeout"
        assert done["resource_cpu_seconds_observed"] < 0.2
        assert done["cleanup_verified"]
        assert MacKernel().members(done["resource_id"]) == {}


def test_native_task_cpu_uses_actual_mach_timebase_instead_of_assuming_nanoseconds(tmp_path):
    kernel = MacKernel()
    member = kernel.members(kernel.resource_id(os.getpid()))[os.getpid()]
    birth = (member.identity.pid, member.identity.unique)
    initial = kernel.metrics({os.getpid(): member})["cpu_nanoseconds"][birth]
    started = time.process_time_ns()
    while time.process_time_ns() - started < 50_000_000:
        pass
    elapsed = time.process_time_ns() - started
    final = kernel.metrics({os.getpid(): member})["cpu_nanoseconds"][birth]
    observed = final - initial
    result = {
        "process_cpu_ns": elapsed,
        "scope_observed_cpu_ns": observed,
        "mach_timebase_numer": kernel.timebase_numer,
        "mach_timebase_denom": kernel.timebase_denom,
    }
    (tmp_path / "cpu-calibration.json").write_text(json.dumps(result, indent=2))
    assert 0.8 * elapsed <= observed <= 1.2 * elapsed


def test_process_exiting_after_task_measurement_is_skipped_without_adopting_a_new_birth(
    monkeypatch,
):
    kernel = MacKernel()
    member = kernel.members(kernel.resource_id(os.getpid()))[os.getpid()]
    monkeypatch.setattr(kernel, "identity", lambda pid: None)
    metrics = kernel.metrics({os.getpid(): member})
    assert metrics == {"rss_bytes": 0, "processes": 0, "cpu_nanoseconds": {}}


def test_exec_during_member_scan_rechecks_same_birth_and_rejects_pid_reuse(monkeypatch):
    kernel = MacKernel()
    pid = os.getpid()
    resource = kernel.resource_id(pid)
    first = kernel.identity(pid)
    after_exec = ProcessIdentity(pid, first.unique, first.version + 1)
    monkeypatch.setattr(kernel, "pids", lambda: {pid})
    monkeypatch.setattr(kernel, "identity", lambda _pid: next(identities))
    identities = iter((first, after_exec, after_exec, after_exec))
    assert kernel.members(resource)[pid].identity == after_exec
    reused = ProcessIdentity(pid, first.unique + 1, first.version + 1)
    identities = iter((first, reused))
    with pytest.raises(ScopeError, match="EXECUTION_SCOPE_IDENTITY_CHANGED"):
        kernel.members(resource)


def test_metrics_and_signal_tolerate_exec_but_refuse_a_new_birth(monkeypatch):
    kernel = MacKernel()
    pid = os.getpid()
    resource = kernel.resource_id(pid)
    current = kernel.identity(pid)
    old = ProcessIdentity(pid, current.unique, current.version - 1)
    member = CoalitionMember(old, resource, 2)
    metrics = kernel.metrics({pid: member})
    assert metrics["rss_bytes"] == 0 and metrics["processes"] == 0
    assert list(metrics["cpu_nanoseconds"]) == [(pid, current.unique)]
    sent = []
    monkeypatch.setattr(
        kernel, "_audit_signal", lambda identity, sig: sent.append((identity, sig)) or 0
    )
    assert kernel.signal_member(member, signal.SIGCONT)
    assert sent == [(current, signal.SIGCONT)]
    changed_again = ProcessIdentity(pid, current.unique, current.version + 1)
    identities = iter((current, changed_again))
    monkeypatch.setattr(kernel, "identity", lambda _pid: next(identities))
    assert not kernel.signal_member(member, signal.SIGCONT)
    assert sent == [(current, signal.SIGCONT)]
    monkeypatch.setattr(
        kernel, "identity", lambda _pid: ProcessIdentity(pid, current.unique + 1, current.version)
    )
    with pytest.raises(ScopeError, match="EXECUTION_SCOPE_IDENTITY_CHANGED"):
        kernel.signal_member(member, signal.SIGCONT)
    assert sent == [(current, signal.SIGCONT)]
    with pytest.raises(ScopeError, match="EXECUTION_SCOPE_IDENTITY_FAILED"):
        kernel.signal_member(member, True)


def test_private_input_payload_uses_only_pipe_and_never_persistent_scope_state(tmp_path):
    payload = b"synthetic-private-input-for-pipe-only" * 1000
    digest = hashlib.sha256(payload).hexdigest()
    code = (
        "import hashlib,sys; data=sys.stdin.buffer.read(); "
        f"assert hashlib.sha256(data).hexdigest()=={digest!r}; "
        "print('input checked')"
    )
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, code, stdin_payload=payload)
        done = _wait(manager)
        assert done["state"] == "exited" and done["exit_code"] == 0
        assert manager._jobs["native"].stdin_payload is None
        assert "input checked" in manager.read_output("native")["chunks"][0]["text"]
        assert "synthetic-private-input" not in json.dumps(manager.failure_summaries())
        for file in (tmp_path / "private-scope").rglob("*"):
            if file.is_file():
                assert b"synthetic-private-input" not in file.read_bytes()


def test_wrong_coalition_or_birth_identity_is_refused_without_signalling():
    kernel = MacKernel()
    own = kernel.identity(os.getpid())
    with pytest.raises(ScopeError, match="EXECUTION_SCOPE_IDENTITY_FAILED"):
        CoalitionScope(kernel, own, kernel.resource_id(os.getpid()) + 1)
    false_birth = type(own)(own.pid, own.unique + 1, own.version)
    with pytest.raises(ScopeError, match="EXECUTION_SCOPE_IDENTITY_FAILED"):
        CoalitionScope(kernel, false_birth, kernel.resource_id(os.getpid()))
    with pytest.raises(ScopeError, match="EXECUTION_SCOPE_IDENTITY_CHANGED"):
        kernel.signal_identity(false_birth, signal.SIGCONT)
    assert kernel.signal_identity(own, signal.SIGCONT)


def test_unsupported_kernel_and_enumeration_failure_never_use_process_tree(monkeypatch):
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(release="999.0.0"))
    with pytest.raises(ScopeError, match="EXECUTION_SCOPE_ABI_UNSUPPORTED"):
        MacKernel()

    class Unreadable:
        def identity(self, pid):
            return SimpleNamespace(pid=pid)

        def resource_id(self, pid):
            return 999 if pid == 123 else 888

        def members(self, *args, **kwargs):
            raise ScopeError("EXECUTION_SCOPE_ENUMERATION_FAILED")

    scope = CoalitionScope(Unreadable(), SimpleNamespace(pid=123), 999)
    assert scope.stop(GRACE) is False
    assert scope.failed


def test_posix_spawn_cannot_choose_another_resource_coalition(tmp_path):
    foreign = MacKernel().resource_id(os.getpid())
    code = (
        "import ctypes,json,os\n"
        "lib=ctypes.CDLL(None)\n"
        "attr=ctypes.c_void_p()\n"
        "lib.posix_spawnattr_init.argtypes=[ctypes.c_void_p]\n"
        "lib.posix_spawnattr_setcoalition_np.argtypes=[ctypes.c_void_p,ctypes.c_uint64,"
        "ctypes.c_int,ctypes.c_int]\n"
        "lib.posix_spawn.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_void_p,"
        "ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]\n"
        "assert lib.posix_spawnattr_init(ctypes.byref(attr))==0\n"
        f"assert lib.posix_spawnattr_setcoalition_np(ctypes.byref(attr),{foreign},0,0)==0\n"
        "pid=ctypes.c_int()\n"
        "argv=(ctypes.c_char_p*2)(b'/usr/bin/true',None)\n"
        "env=(ctypes.c_char_p*1)(None)\n"
        "result=lib.posix_spawn(ctypes.byref(pid),b'/usr/bin/true',None,ctypes.byref(attr),"
        "argv,env)\n"
        "lib.posix_spawnattr_destroy(ctypes.byref(attr))\n"
        "open('spawn-result.json','w').write(json.dumps({'result':result,'pid':pid.value}))\n"
        "if result==0: os.waitpid(pid.value,0)\n"
        "assert result==1, 'kernel did not reject coalition selection with EPERM'\n"
    )
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, code)
        done = _wait(manager)
        result = json.loads((tmp_path / "spawn-result.json").read_text())
        _record(tmp_path, done)
        assert result["result"] == 1
        assert done["state"] == "exited" and done["exit_code"] == 0
        assert done["cleanup_verified"]


def test_unread_full_output_cannot_hold_the_supervisor_or_timeout_cleanup(tmp_path):
    helper = LaunchdSupervisor.spawn(
        tmp_path / "private-scope",
        Path(
            __import__("code_context.execution_process", fromlist=["__file__"]).__file__
        ).resolve(),
    )
    config = {
        "argv": [sys.executable, "-I", "-S", "-c", "import os\nwhile True: os.write(1,b'x'*65536)"],
        "cwd": str(tmp_path),
        "env": {},
        "service": False,
        "timeout": 1,
        "idle": 1200,
        "grace": GRACE,
        "stdin_payload": None,
        "resource_limits": {
            "max_rss_bytes": 2 * 1024**3,
            "max_processes": 128,
            "sample_seconds": 0.1,
        },
    }
    try:
        helper.stdin.write(json.dumps(config).encode() + b"\n")
        helper.stdin.flush()
        # Deliberately keep the control writer alive without consuming output.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and helper.scope.kernel.identity(helper.pid) is not None:
            time.sleep(0.025)
        assert helper.scope.kernel.identity(helper.pid) is None
        assert helper.scope.kernel.members(helper.scope.resource_id) == {}
        _record(tmp_path, {"scope_empty": True, "resource_id": helper.scope.resource_id})
    finally:
        helper.stdin.close()
        helper.wait(timeout=0.1)
        helper.stdout.close()


def test_completed_native_scope_is_bound_to_job_and_verifiable_after_manager_restart(tmp_path):
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, "print('done')")
        done = _wait(manager)
        assert done["state"] == "exited" and done["cleanup_verified"]
        helper = manager._jobs["native"].supervisor
        control_path = helper.directory.root / "control.json"
        identity_path = helper.directory.root / "identity.json"
        control = json.loads(control_path.read_bytes())
        identity = json.loads(identity_path.read_bytes())
        assert control["job_id"] == identity["job_id"] == "native"
        assert control["kernel_boot"] == identity["kernel_boot"]
        assert len(control_path.read_bytes()) <= 500 and len(identity_path.read_bytes()) <= 500
    root = tmp_path / "private-scope"
    index = index_retired_job_scopes(root)
    proof = verify_retired_job_scope(root, "native", index=index)
    assert proof == {
        "job_id": "native",
        "resource_id": done["resource_id"],
        "cleanup_verified": True,
        "launchd_retired": True,
    }
    _record(tmp_path, {"snapshot": done, "retirement_proof": proof})


def test_recovery_never_boots_out_or_signals_a_still_live_native_job(tmp_path, monkeypatch):
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, "import time;time.sleep(100)", service=True)
        running = _wait(manager, {"running"})
        calls = []
        original = native_scope._launchctl

        def launchctl(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(native_scope, "_launchctl", launchctl)
        with pytest.raises(ScopeError, match="RETIREMENT_UNVERIFIED"):
            verify_retired_job_scope(tmp_path / "private-scope", "native")
        assert calls == []
        assert MacKernel().members(running["resource_id"])
        manager.cancel("native")
        done = _wait(manager)
        assert done["state"] == "cancelled" and done["cleanup_verified"]


def test_retired_own_inactive_launchd_label_is_booted_out_without_restarting(tmp_path):
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, "print('done')")
        done = _wait(manager)
        assert done["state"] == "exited" and done["cleanup_verified"]
        helper = manager._jobs["native"].supervisor
    target, plist = helper.target, helper.directory.root / "job.plist"
    assert native_scope._launchctl("bootstrap", f"gui/{os.getuid()}", str(plist)).returncode == 0
    try:
        # RunAtLoad/KeepAlive are False; bootstrap only registers this owned
        # fixture's inactive job. No command or helper is started again.
        registered = native_scope._launchctl("print", target)
        (tmp_path / "inactive-launchd.txt").write_bytes(registered.stdout)
        assert registered.returncode == 0
        assert MacKernel().members(done["resource_id"]) == {}
        proof = verify_retired_job_scope(tmp_path / "private-scope", "native")
        assert proof["cleanup_verified"] and proof["launchd_retired"]
        assert native_scope._launchctl("print", target).returncode == 113
        _record(tmp_path, proof)
    finally:
        native_scope._launchctl("bootout", target)


def _private_write(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)


def _fake_retirement_scope(tmp_path, monkeypatch, *, root=None, job_id="retired"):
    from code_context import execution_process

    root = root or tmp_path / "private-scope"
    root.mkdir(mode=0o700, exist_ok=True)
    name = os.urandom(16).hex()
    directory = root / name
    directory.mkdir(mode=0o700)
    control = {
        "uid": os.getuid(),
        "label": "com.colink.execution." + name,
        "port": 43000,
        "token": os.urandom(32).hex(),
        "job_id": job_id,
        "kernel_boot": native_scope._kernel_boot_identity(),
    }
    control_raw = native_scope._scope_json(control)
    plist_raw = native_scope.plistlib.dumps(
        native_scope._supervisor_plist(directory, Path(execution_process.__file__).resolve())
    )
    identity = {
        "job_id": job_id,
        "kernel_boot": control["kernel_boot"],
        "control_sha256": hashlib.sha256(control_raw).hexdigest(),
        "plist_sha256": hashlib.sha256(plist_raw).hexdigest(),
        "identity": {"pid": 999999, "unique": 42, "version": 13},
        "resource_id": 99999,
    }
    _private_write(directory / "control.json", control_raw)
    _private_write(directory / "identity.json", native_scope._scope_json(identity))
    _private_write(directory / "job.plist", plist_raw)
    calls = []

    def launchctl(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=113, stdout=b"")

    monkeypatch.setattr(
        native_scope,
        "MacKernel",
        lambda: SimpleNamespace(identity=lambda pid: None, members=lambda resource: {}),
    )
    monkeypatch.setattr(native_scope, "_launchctl", launchctl)
    return root, directory, control, identity, calls


def test_saved_retirement_protocol_verifies_with_an_explicit_empty_kernel_observation(
    tmp_path, monkeypatch
):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    proof = verify_retired_job_scope(root, "retired")
    assert proof["cleanup_verified"] and proof["resource_id"] == 99999
    assert len(calls) == 3 and all(call[0] == "print" for call in calls)


@pytest.mark.parametrize(
    "change", ["job", "boot", "control_hash", "plist_hash", "resource_type", "identity_type"]
)
def test_retirement_identity_binding_refuses_invalid_saved_records(tmp_path, monkeypatch, change):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    if change == "job":
        identity["job_id"] = "foreign"
    elif change == "boot":
        control["kernel_boot"] = identity["kernel_boot"] = "1:0"
        raw = native_scope._scope_json(control)
        _private_write(directory / "control.json", raw)
        identity["control_sha256"] = hashlib.sha256(raw).hexdigest()
    elif change == "control_hash":
        identity["control_sha256"] = "0" * 64
    elif change == "plist_hash":
        identity["plist_sha256"] = "0" * 64
    elif change == "resource_type":
        identity["resource_id"] = True
    else:
        identity["identity"]["pid"] = True
    _private_write(directory / "identity.json", native_scope._scope_json(identity))
    with pytest.raises(ScopeError, match="RETIREMENT_UNVERIFIED"):
        verify_retired_job_scope(root, "retired")
    assert calls == []


@pytest.mark.parametrize(
    "unsafe", ["permissions", "symlink", "hardlink", "size", "legacy", "plist"]
)
def test_retirement_private_record_boundaries_fail_closed(tmp_path, monkeypatch, unsafe):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    path = directory / "control.json"
    if unsafe == "permissions":
        path.chmod(0o644)
    elif unsafe == "symlink":
        retained = directory / "retained-control.json"
        path.rename(retained)
        path.symlink_to(retained)
    elif unsafe == "hardlink":
        os.link(path, directory / "second-control-link")
    elif unsafe == "size":
        _private_write(path, b" " * 501)
    elif unsafe == "legacy":
        del control["job_id"]
        del control["kernel_boot"]
        _private_write(path, native_scope._scope_json(control))
    else:
        plist = native_scope.plistlib.loads((directory / "job.plist").read_bytes())
        plist["RunAtLoad"] = True
        _private_write(directory / "job.plist", native_scope.plistlib.dumps(plist))
    with pytest.raises(ScopeError):
        verify_retired_job_scope(root, "retired")
    assert calls == []


def test_retirement_missing_or_duplicate_job_and_changed_index_never_guess(tmp_path, monkeypatch):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    index = index_retired_job_scopes(root)
    with pytest.raises(ScopeError):
        verify_retired_job_scope(root, "missing", index=index)
    raw = (directory / "control.json").read_bytes()
    _private_write(directory / "control.json", raw)  # Same bytes still changes ctime/mtime.
    with pytest.raises(ScopeError, match="STATE_CHANGED"):
        verify_retired_job_scope(root, "retired", index=index)
    _fake_retirement_scope(tmp_path, monkeypatch, root=root)
    with pytest.raises(ScopeError):
        verify_retired_job_scope(root, "retired")
    assert calls == []


def test_retirement_root_must_exist_be_private_and_have_no_symlink(tmp_path, monkeypatch):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    missing = tmp_path / "missing"
    with pytest.raises(ScopeError):
        index_retired_job_scopes(missing)
    assert not missing.exists()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ScopeError):
        index_retired_job_scopes(alias)
    root.chmod(0o755)
    with pytest.raises(ScopeError):
        verify_retired_job_scope(root, "retired")
    assert calls == []


@pytest.mark.parametrize("loaded", ["foreign_path", "running", "unknown", "large"])
def test_retirement_only_exact_inactive_self_label_may_be_booted_out(tmp_path, monkeypatch, loaded):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    target = f"gui/{os.getuid()}/{control['label']}"
    path = directory / "job.plist"
    state, pid = "not running", ""
    if loaded == "foreign_path":
        path = tmp_path / "foreign.plist"
    elif loaded == "running":
        state, pid = "running", "\n\tpid = 123"
    elif loaded == "unknown":
        state = "waiting"
    output = (
        target + " = {\n\tpath = " + str(path) + "\n\tstate = " + state + pid + "\n}\n"
    ).encode()
    if loaded == "large":
        output = b"x" * 65537

    def launchctl(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(native_scope, "_launchctl", launchctl)
    with pytest.raises(ScopeError):
        verify_retired_job_scope(root, "retired")
    assert all(call[0] == "print" for call in calls)


def test_retirement_enumeration_failure_or_member_appearance_prevents_proof(tmp_path, monkeypatch):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    count = 0

    def members(resource):
        nonlocal count
        count += 1
        return {} if count == 1 else {123: object()}

    monkeypatch.setattr(
        native_scope,
        "MacKernel",
        lambda: SimpleNamespace(identity=lambda pid: None, members=members),
    )
    with pytest.raises(ScopeError):
        verify_retired_job_scope(root, "retired")

    def unreadable(resource):
        raise ScopeError("EXECUTION_SCOPE_ENUMERATION_FAILED")

    monkeypatch.setattr(
        native_scope,
        "MacKernel",
        lambda: SimpleNamespace(identity=lambda pid: None, members=unreadable),
    )
    with pytest.raises(ScopeError, match="ENUMERATION_FAILED"):
        verify_retired_job_scope(root, "retired")
    assert all(call[0] == "print" for call in calls)


def test_retirement_changed_identity_during_verification_invalidates_empty_scope(
    tmp_path, monkeypatch
):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    count = 0

    def members(resource):
        nonlocal count
        count += 1
        if count == 2:
            raw = (directory / "identity.json").read_bytes()
            _private_write(directory / "identity.json", raw)
        return {}

    monkeypatch.setattr(
        native_scope,
        "MacKernel",
        lambda: SimpleNamespace(identity=lambda pid: None, members=members),
    )
    with pytest.raises(ScopeError, match="STATE_CHANGED"):
        verify_retired_job_scope(root, "retired")


def test_retirement_index_has_strict_time_and_record_count_budgets(tmp_path, monkeypatch):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        ticks = iter([0, 1])
        patch.setattr(native_scope.time, "monotonic", lambda: next(ticks))
        with pytest.raises(ScopeError, match="TIME_BUDGET"):
            index_retired_job_scopes(root)
    with monkeypatch.context() as patch:
        patch.setattr(native_scope.os, "listdir", lambda fd: ["0" * 32] * 4097)
        with pytest.raises(ScopeError, match="METADATA_CAPACITY"):
            index_retired_job_scopes(root)
    assert calls == []


def test_job_id_over_record_budget_is_rejected_before_launchctl(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(native_scope, "_launchctl", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ScopeError, match="METADATA_CAPACITY"):
        LaunchdSupervisor.spawn(
            tmp_path / "private-scope", Path(__file__).resolve(), job_id="中" * 180
        )
    assert calls == []


def test_expiry_removes_only_three_verified_native_scope_files_and_preserves_report(tmp_path):
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, "print('done')")
        done = _wait(manager)
        assert done["state"] == "exited" and done["cleanup_verified"]
        directory = manager._jobs["native"].supervisor.directory.root
    _record(tmp_path, done)
    original_report = (tmp_path / "result.json").read_bytes()
    assert {path.name for path in directory.iterdir()} == {
        "control.json",
        "identity.json",
        "job.plist",
    }
    result = expire_verified_job_scope(tmp_path / "private-scope", "native")
    assert result["expired"] and result["cleanup_verified"]
    assert not directory.exists() and list((tmp_path / "private-scope").iterdir()) == []
    assert (tmp_path / "result.json").read_bytes() == original_report
    assert MacKernel().members(done["resource_id"]) == {}


def test_expiry_cannot_touch_an_active_native_scope(tmp_path):
    with _manager(tmp_path) as manager:
        _submit(manager, tmp_path, "import time;time.sleep(100)", service=True)
        _wait(manager, {"running"})
        helper = manager._jobs["native"].supervisor
        captured = {path.name: path.read_bytes() for path in helper.directory.root.iterdir()}
        with pytest.raises(ScopeError):
            expire_verified_job_scope(tmp_path / "private-scope", "native")
        assert {
            path.name: path.read_bytes() for path in helper.directory.root.iterdir()
        } == captured
        assert MacKernel().identity(helper.pid) == helper.scope.leader
        manager.cancel("native")
        assert _wait(manager)["cleanup_verified"]


@pytest.mark.parametrize("unknown", ["foreign.txt", ".state-retained", "control.sock"])
def test_expiry_preserves_unknown_objects_and_does_not_remove_any_scope_file(
    tmp_path, monkeypatch, unknown
):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    _private_write(directory / unknown, b"owned fixture canary; preserve")
    captured = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(ScopeError, match="EXPIRY_UNKNOWN_OBJECT"):
        expire_verified_job_scope(root, "retired")
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == captured
    assert list(root.iterdir()) == [directory]


@pytest.mark.parametrize("changed", ["identity", "unknown", "replacement"])
def test_expiry_native_namespace_cas_refuses_changed_or_replaced_data(
    tmp_path, monkeypatch, changed
):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    captured = {path.name: path.read_bytes() for path in directory.iterdir()}
    original = native_scope._scope_rename_excl
    retained = root / "retained-owned-scope"

    def rename(parent, old, new):
        original(parent, old, new)
        if new.startswith(".expired-"):
            moved = root / new
            if changed == "identity":
                _private_write(moved / "identity.json", b"{}")
            elif changed == "unknown":
                _private_write(moved / "foreign.txt", b"preserve")
            else:
                moved.rename(retained)
                moved.mkdir(mode=0o700)
                _private_write(moved / "foreign.txt", b"foreign directory; preserve")

    monkeypatch.setattr(native_scope, "_scope_rename_excl", rename)
    with pytest.raises(ScopeError):
        expire_verified_job_scope(root, "retired")
    assert directory.exists() and not any(
        path.name.startswith(".expired-") for path in root.iterdir()
    )
    if changed == "replacement":
        assert (directory / "foreign.txt").read_bytes() == b"foreign directory; preserve"
        assert {path.name: path.read_bytes() for path in retained.iterdir()} == captured
    elif changed == "unknown":
        assert (directory / "foreign.txt").read_bytes() == b"preserve"
        assert {name: (directory / name).read_bytes() for name in captured} == captured
    else:
        assert (directory / "identity.json").read_bytes() == b"{}"
        assert (directory / "control.json").read_bytes() == captured["control.json"]
        assert (directory / "job.plist").read_bytes() == captured["job.plist"]


def test_expiry_last_native_scan_rejects_a_new_member_and_restores_directory(tmp_path, monkeypatch):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    captured = {path.name: path.read_bytes() for path in directory.iterdir()}
    count = 0

    def members(resource):
        nonlocal count
        count += 1
        return {} if count <= 3 else {123: object()}

    monkeypatch.setattr(
        native_scope,
        "MacKernel",
        lambda: SimpleNamespace(identity=lambda pid: None, members=members),
    )
    with pytest.raises(ScopeError):
        expire_verified_job_scope(root, "retired")
    assert count == 4
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == captured
    assert list(root.iterdir()) == [directory]


@pytest.mark.parametrize("failure_after", [0, 1])
def test_expiry_filesystem_failure_retains_remainder_and_never_reports_success(
    tmp_path, monkeypatch, failure_after
):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    captured = {path.name: path.read_bytes() for path in directory.iterdir()}
    original = os.unlink
    removed = []

    def unlink(name, *, dir_fd=None):
        if len(removed) == failure_after:
            raise OSError(5, "synthetic fixture I/O failure")
        original(name, dir_fd=dir_fd)
        removed.append(name)

    monkeypatch.setattr(native_scope.os, "unlink", unlink)
    with pytest.raises(ScopeError, match="EXPIRY_UNVERIFIED"):
        expire_verified_job_scope(root, "retired")
    assert directory.exists() and list(root.iterdir()) == [directory]
    assert len(removed) == failure_after
    remaining = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert remaining == {name: raw for name, raw in captured.items() if name not in removed}


def test_expiry_does_not_touch_legacy_or_unbound_scope_metadata(tmp_path, monkeypatch):
    root, directory, control, identity, calls = _fake_retirement_scope(tmp_path, monkeypatch)
    del control["job_id"]
    del control["kernel_boot"]
    _private_write(directory / "control.json", native_scope._scope_json(control))
    captured = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(ScopeError):
        expire_verified_job_scope(root, "retired")
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == captured
    assert calls == []

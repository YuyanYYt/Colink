"""Lightweight CLI calls must not load services; service dispatch stays unchanged."""

import json
import os
import subprocess
import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from code_context import __version__


def probe_cli(arguments, stdin=""):
    # A fresh interpreter proves that imports from other tests cannot mask an
    # eager service import. Block the dependency at its real import boundary.
    script = """
import contextlib
import importlib.abc
import io
import json
import sys

class NoServices(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.')
               for name in ('uvicorn', 'mcp', 'code_context.server')):
            raise AssertionError('service imported by lightweight CLI: ' + fullname)

sys.meta_path.insert(0, NoServices())
from code_context.cli import main
output, errors = io.StringIO(), io.StringIO()
sys.stdin = io.StringIO(sys.argv[2])
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
    try:
        status = main(json.loads(sys.argv[1]))
    except SystemExit as exc:
        status = exc.code
print(json.dumps({'status': status, 'output': output.getvalue(),
                  'errors': errors.getvalue(), 'modules': sorted(sys.modules)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(arguments), stdin],
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--version"],
        ["desktop-status", "--help"],
        ["desktop-control", "--help"],
        ["local", "--help"],
        ["serve", "--help"],
    ],
)
def test_help_and_version_need_no_service_or_source_modules(arguments):
    result = probe_cli(arguments)
    assert result["status"] == 0
    assert result["output"] and not result["errors"]
    assert not any(
        name in result["modules"]
        for name in ("code_context.client", "code_context.scanner", "code_context.storage")
    )
    if arguments == ["--version"]:
        assert result["output"].strip() == __version__


@pytest.fixture
def configured_workspace(tmp_path):
    from code_context.tunnel import prepare_profile

    workspace = tmp_path.resolve() / "workspace"
    root = workspace / "source"
    root.mkdir(parents=True)
    prepare_profile(
        root,
        "sample",
        workspace / ".code-context/local-sample",
        "tunnel_" + "0" * 32,
        workspace / ".code-context/tunnel/profile.yaml",
    )
    return workspace, root


@pytest.mark.parametrize("mode", ["mirror", "live"])
def test_desktop_status_does_not_import_services_or_initialize_source(configured_workspace, mode):
    workspace, root = configured_workspace
    result = probe_cli(
        ["desktop-status", "--workspace", str(workspace), "--root", str(root), "--mode", mode]
    )
    assert result["status"] == 0 and not result["errors"]
    status = json.loads(result["output"])
    assert not status["running"]
    assert not (workspace / ".code-context/local-sample").exists()
    assert not (workspace / ".code-context/desktop/workspaces").exists()


@pytest.mark.parametrize("command", ["local-status", "status"])
def test_offline_status_needs_no_service_and_creates_no_state(tmp_path, command):
    data = tmp_path / "unused-data"
    arguments = [command, "--data-dir", str(data)]
    if command == "status":
        arguments += ["--root", str(tmp_path), "--project", "sample"]
    result = probe_cli(arguments)
    assert result["status"] == 0 and not result["errors"]
    assert not json.loads(result["output"])["initialized"]
    assert not data.exists()


def test_desktop_selection_does_not_import_services_or_start_a_connection(configured_workspace):
    workspace, root = configured_workspace
    result = probe_cli(
        [
            "desktop-select",
            "--workspace",
            str(workspace),
            "--root",
            str(root),
            "--mode",
            "live",
        ]
    )
    assert result["status"] == 0 and not result["errors"]
    assert not json.loads(result["output"])["connection_started"]
    assert not (workspace / ".code-context/desktop/workspaces").exists()


def test_invalid_desktop_control_keeps_its_safe_error_without_service_import(configured_workspace):
    workspace, root = configured_workspace
    result = probe_cli(
        [
            "desktop-control",
            "--workspace",
            str(workspace),
            "--root",
            str(root),
            "--mode",
            "live",
            "--action",
            "status",
        ],
        '["untrusted-synthetic-input"]',
    )
    assert result["status"] == 1 and not result["output"]
    assert result["errors"].strip() == "错误：local control requires an object"
    assert "untrusted-synthetic-input" not in result["errors"]


@pytest.mark.parametrize("command", ["workspace", "live", "local", "mcp"])
def test_stdio_service_commands_still_dispatch_and_close(tmp_path, monkeypatch, command):
    from code_context.cli import main

    mcp = SimpleNamespace(run=Mock())
    server = ModuleType("code_context.server")
    server.build_mcp = Mock(return_value=mcp)
    monkeypatch.setitem(sys.modules, "code_context.server", server)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "synthetic-control-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-model-key")
    arguments = [command, "--data-dir", str(tmp_path / "data")]
    if command != "mcp":
        arguments += ["--root", str(tmp_path), "--project", "sample"]
    backend = SimpleNamespace(mcp_status=Mock(), close=Mock())
    if command == "workspace":
        import code_context.workspace as workspace

        runtime = SimpleNamespace(backend=backend)
        monkeypatch.setattr(workspace, "WorkspaceRuntime", lambda *_: nullcontext(runtime))
    elif command == "live":
        import code_context.live as live

        monkeypatch.setattr(live, "LiveQueries", lambda *_, **options: backend)
    elif command == "local":
        import code_context.local as local

        mirror = SimpleNamespace(
            store=backend,
            root=tmp_path,
            start=Mock(),
            ensure_ready=Mock(),
            mcp_status=Mock(),
        )
        monkeypatch.setattr(local, "LocalMirror", lambda *_: nullcontext(mirror))
    else:
        import code_context.storage as storage

        monkeypatch.setattr(storage, "MirrorStore", lambda *_: backend)
    assert main(arguments) == 0
    assert server.build_mcp.call_args.args[0] is backend
    mcp.run.assert_called_once_with(transport="stdio")
    if command == "local":
        mirror.start.assert_called_once_with()
    if command == "live":
        backend.close.assert_called_once_with()
    if command != "mcp":
        assert "CONTROL_PLANE_API_KEY" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ


def test_serve_keeps_host_port_and_disabled_access_log(tmp_path, monkeypatch):
    import uvicorn

    import code_context.storage as storage
    from code_context.cli import main

    store, app = object(), object()
    server = ModuleType("code_context.server")
    server.create_app = Mock(return_value=app)
    monkeypatch.setitem(sys.modules, "code_context.server", server)
    monkeypatch.setattr(storage, "MirrorStore", lambda *_: store)
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setenv("CODE_CONTEXT_READ_TOKEN", "synthetic-read-token")
    monkeypatch.setenv("CODE_CONTEXT_SYNC_TOKEN", "synthetic-sync-token")
    assert (
        main(
            [
                "serve",
                "--data-dir",
                str(tmp_path),
                "--host",
                "127.0.0.1",
                "--port",
                "43219",
                "--public-url",
                "https://example.invalid",
            ]
        )
        == 0
    )
    server.create_app.assert_called_once_with(
        store, "synthetic-read-token", "synthetic-sync-token", "https://example.invalid"
    )
    run.assert_called_once_with(app, host="127.0.0.1", port=43219, access_log=False)


@pytest.mark.parametrize("kind", ["sync", "retryable", "scan", "unexpected", "interrupt"])
def test_lazy_command_imports_preserve_error_and_interrupt_boundary(
    tmp_path, monkeypatch, capsys, kind
):
    from code_context.cli import main
    from code_context.client import RetryableSyncError, SyncError
    from code_context.scanner import ScanError, Scanner

    error = {
        "sync": SyncError,
        "retryable": RetryableSyncError,
        "scan": ScanError,
        "unexpected": RuntimeError,
        "interrupt": KeyboardInterrupt,
    }[kind]

    def fail(_):
        raise error("safe synthetic diagnostic")

    monkeypatch.setattr(Scanner, "scan", fail)
    arguments = ["scan", "--root", str(tmp_path)]
    if kind == "unexpected":
        with pytest.raises(RuntimeError, match="safe synthetic diagnostic"):
            main(arguments)
        return
    assert main(arguments) == (130 if kind == "interrupt" else 1)
    output = capsys.readouterr()
    assert not output.out
    assert output.err.strip() == (
        "已停止；本地待同步队列已保留。"
        if kind == "interrupt"
        else "错误：safe synthetic diagnostic"
    )

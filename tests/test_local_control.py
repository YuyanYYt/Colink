import os
import socket
from pathlib import Path

import pytest

from code_context.local_control import (
    LocalControl,
    _receive,
    _send,
    control_request,
    private_directory,
    read_state,
    write_state,
)
from code_context.source_access import SourceError


def test_private_state_is_atomic_and_rejects_symlinks(tmp_path):
    directory = private_directory(tmp_path / "state")
    write_state(directory, "status.json", {"state": "closed"})
    assert read_state(directory, "status.json") == {"state": "closed"}
    assert os.stat(directory.root / "status.json").st_mode & 0o077 == 0
    (directory.root / "unsafe.json").symlink_to(directory.root / "status.json")
    with pytest.raises(SourceError, match="UNSAFE_CONTROL_STATE"):
        write_state(directory, "unsafe.json", {})
    with pytest.raises(SourceError):
        read_state(directory, "unsafe.json")


def test_unsafe_directory_and_long_socket_path_rejected(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(original)
    with pytest.raises(SourceError):
        private_directory(linked / "child")
    with pytest.raises(SourceError, match="CONTROL_PATH_TOO_LONG"):
        LocalControl(tmp_path / "state", tmp_path / ("x" * 100), lambda *_: {})


def test_actual_private_socket_and_token_restart(tmp_path):
    # Unix socket paths have a byte limit. Keep the socket root inside this
    # authorized worktree, independently of pytest's long fixture path.
    import tempfile

    socket_root = Path(tempfile.mkdtemp(prefix="ctl-", dir=Path.cwd() / ".artifacts"))
    state_dir = tmp_path / "state"
    calls = []

    def handler(action, parameters):
        calls.append((action, parameters))
        return {"state": "closed"}

    server = LocalControl(state_dir, socket_root, handler)
    server.start()
    saved = read_state(server.state, "control.json")
    try:
        assert control_request(state_dir, "status") == {"state": "closed"}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(server.path))
            _send(
                client,
                {"token": "not-the-private-token", "action": "enable_write", "parameters": {}},
            )
            assert _receive(client)["error"] == "CONTROL_NOT_AUTHORIZED"
        assert calls == [("status", {})]
    finally:
        server.close()
    assert not server.path.exists()
    with pytest.raises(SourceError, match="CONTROL_UNAVAILABLE"):
        control_request(state_dir, "status")
    replacement = LocalControl(state_dir, socket_root, handler)
    assert replacement.token != saved["token"]
    replacement.close()

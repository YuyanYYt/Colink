import errno
import os
import socket
import threading
from pathlib import Path

import pytest

import code_context.local_control as local_control
from code_context.local_control import (
    LocalControl,
    _accepts_connections,
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
    assert server.is_alive()
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
    assert not server.is_alive()
    with pytest.raises(SourceError, match="CONTROL_UNAVAILABLE"):
        control_request(state_dir, "status")
    replacement = LocalControl(state_dir, socket_root, handler)
    assert replacement.token != saved["token"]
    replacement.close()


@pytest.mark.parametrize(
    ("platform", "family", "error", "allowed"),
    [
        ("darwin", socket.AF_UNIX, errno.ENOPROTOOPT, True),
        ("darwin", socket.AF_INET, errno.ENOPROTOOPT, False),
        ("linux", socket.AF_UNIX, errno.ENOPROTOOPT, False),
        ("darwin", socket.AF_UNIX, errno.EPERM, False),
    ],
)
def test_unsupported_listener_option_fallback_is_narrow(
    monkeypatch, platform, family, error, allowed
):
    calls = []

    class SyntheticSocket:
        def getsockopt(self, level, option):
            calls.append(option)
            if option == socket.SO_ACCEPTCONN:
                raise OSError(error, "synthetic socket option failure")
            assert option == socket.SO_TYPE
            return socket.SOCK_STREAM

    connection = SyntheticSocket()
    connection.family = family
    monkeypatch.setattr(local_control.sys, "platform", platform)
    if allowed:
        assert _accepts_connections(connection)
        assert calls == [socket.SO_ACCEPTCONN, socket.SO_TYPE]
    else:
        with pytest.raises(OSError) as exc:
            _accepts_connections(connection)
        assert exc.value.errno == error
        assert calls == [socket.SO_ACCEPTCONN]


def test_close_rejects_connection_accepted_during_shutdown(tmp_path, monkeypatch):
    import tempfile

    native_socket = socket.socket
    accepted, release = threading.Event(), threading.Event()
    calls = []

    class DelayedAcceptSocket(native_socket):
        def accept(self):
            connection, address = super().accept()
            accepted.set()
            assert release.wait(2)
            return connection, address

    monkeypatch.setattr(socket, "socket", DelayedAcceptSocket)
    socket_root = Path(tempfile.mkdtemp(prefix="ctl-close-", dir=Path.cwd() / ".artifacts"))
    server = LocalControl(tmp_path / "state", socket_root, lambda *args: calls.append(args))
    server.start()
    closer = threading.Thread(target=server.close)
    try:
        with native_socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(server.path))
            client.sendall(b'{"token":')
            assert accepted.wait(2)
            closer.start()
            assert server.stop.wait(1)
            release.set()
            closer.join(2)
            assert not closer.is_alive() and not server.thread.is_alive()
            assert client.recv(1024) == b""
            assert calls == []
    finally:
        release.set()
        server.close()
        if closer.ident is not None:
            closer.join(2)

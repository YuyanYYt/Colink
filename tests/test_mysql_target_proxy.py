"""Real loopback TCP tests of MySQL account/catalog gates and packet framing."""

import queue
import socket
import threading
import time
from contextlib import closing

import pytest

from code_context import mysql_target_proxy as mysql
from code_context.postgres_target_proxy import _Rejected
from code_context.source_access import SourceError

FLAGS = mysql.PROTOCOL_41 | mysql.CONNECT_WITH_DB | mysql.SECURE_CONNECTION | mysql.PLUGIN_AUTH
OK = b"\x00\x00\x00\x02\x00\x00\x00"


def packet(payload, sequence=0):
    return len(payload).to_bytes(3, "little") + bytes([sequence]) + payload


def receive(connection, count):
    raw = bytearray()
    while len(raw) < count:
        part = connection.recv(count - len(raw))
        if not part:
            return bytes(raw)
        raw.extend(part)
    return bytes(raw)


def read_packet(connection):
    header = receive(connection, 4)
    if len(header) != 4:
        return header
    return header + receive(connection, int.from_bytes(header[:3], "little"))


def assert_closed(connection):
    try:
        assert connection.recv(1) == b""
    except ConnectionResetError:
        # Refusal with unread client bytes produces a TCP reset on macOS.
        pass


def handshake(*, user=b"project_role", database=b"project_db", flags=FLAGS, auth=b"opaque"):
    raw = flags.to_bytes(4, "little") + (16 * 1024 * 1024).to_bytes(4, "little")
    raw += b"\x2d" + b"\0" * 23 + user + b"\0"
    if flags & mysql.LENENC_AUTH or flags & mysql.SECURE_CONNECTION:
        raw += bytes([len(auth)]) + auth
    else:
        raw += auth + b"\0"
    if flags & mysql.CONNECT_WITH_DB:
        raw += database + b"\0"
    if flags & mysql.PLUGIN_AUTH:
        raw += b"caching_sha2_password\0"
    if flags & mysql.CONNECT_ATTRS:
        raw += b"\x06\x01k\x03val"
    return raw


class Backend:
    """Protocol fixture; SQL access grants are covered by real-server acceptance."""

    def __init__(self, *, auth=(), fragment=False, greeting=None):
        self.auth, self.fragment = auth, fragment
        self.handshakes, self.auth_replies, self.commands = (
            queue.Queue(),
            queue.Queue(),
            queue.Queue(),
        )
        self.closed = threading.Event()
        self.connections, self.workers = [], []
        self.lock = threading.Lock()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(16)
        self.listener.settimeout(0.1)
        caps = mysql.ALLOWED_FLAGS
        self.greeting = greeting or (
            b"\x0a8.4.0-test\0"
            + (123).to_bytes(4, "little")
            + b"abcdefgh\0"
            + (caps & 65535).to_bytes(2, "little")
            + b"\x2d\x02\x00"
            + (caps >> 16).to_bytes(2, "little")
            + b"\x15"
            + b"\0" * 10
            + b"ijklmnopqrst\0caching_sha2_password\0"
        )

    def __enter__(self):
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        return self

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self.lock:
                self.connections.append(connection)
                worker = threading.Thread(target=self._serve, args=(connection,), daemon=True)
                self.workers.append(worker)
            worker.start()

    def _send(self, connection, raw):
        if self.fragment:
            for byte in raw:
                connection.sendall(bytes([byte]))
        else:
            connection.sendall(raw)

    def _serve(self, connection):
        try:
            connection.settimeout(5)
            self._send(connection, packet(self.greeting))
            request = read_packet(connection)
            if len(request) < 4:
                return
            self.handshakes.put(request)
            sequence = 2
            for payload, reply in self.auth:
                self._send(connection, packet(payload, sequence))
                sequence = (sequence + 1) & 255
                if reply is not None:
                    response = read_packet(connection)
                    if len(response) < 4:
                        return
                    self.auth_replies.put(response)
                    sequence = (sequence + 1) & 255
            self._send(connection, packet(OK, sequence))
            while not self.closed.is_set():
                request = read_packet(connection)
                if len(request) < 4:
                    return
                self.commands.put(request)
                connection.sendall(request)
        except OSError:
            pass
        finally:
            connection.close()

    def __exit__(self, *_):
        self.closed.set()
        self.listener.close()
        with self.lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self.thread.join(1)
        for worker in self.workers:
            worker.join(1)


def proxy(backend, **options):
    return mysql.MySQLTargetProxy(
        "127.0.0.1", backend.port, "project_db", "project_role", **options
    )


def client(target):
    connection = socket.create_connection((target.host, target.port), timeout=3)
    connection.settimeout(3)
    return connection


def connected(frontend, backend, request=None):
    assert read_packet(frontend) == packet(backend.greeting)
    request = packet(handshake() if request is None else request, 1)
    frontend.sendall(request)
    assert read_packet(frontend) == packet(OK, 2)
    assert backend.handshakes.get(timeout=2) == request


@pytest.mark.parametrize("extra", [0, mysql.LENENC_AUTH, mysql.CONNECT_ATTRS, 1 << 28])
def test_exact_target_fragmented_greeting_handshake_and_query_relay(extra):
    with (
        Backend(fragment=True) as backend,
        proxy(backend) as target,
        closing(client(target)) as front,
    ):
        assert read_packet(front) == packet(backend.greeting)
        request = packet(handshake(flags=FLAGS | extra), 1)
        for offset in range(0, len(request), 3):
            front.sendall(request[offset : offset + 3])
        assert read_packet(front) == packet(OK, 2)
        assert backend.handshakes.get(timeout=2) == request
        command = packet(b"\x03select 'opaque SQL'")
        for byte in command:
            front.sendall(bytes([byte]))
        assert read_packet(front) == command
        assert backend.commands.get(timeout=2) == command


@pytest.mark.parametrize(
    "handshake_payload",
    [
        handshake(user=b"root"),
        handshake(user=b"PROJECT_ROLE"),
        handshake(database=b"other_db"),
        handshake(database=b""),
        handshake(flags=FLAGS & ~mysql.CONNECT_WITH_DB),
        handshake(flags=FLAGS & ~mysql.PROTOCOL_41),
        handshake() + b"trailing",
        handshake(auth=b"")[:-1],
        handshake()[:9] + b"x" + handshake()[10:],
        handshake()[:8] + b"\0" + handshake()[9:],
        handshake(flags=FLAGS | mysql.LENENC_AUTH).replace(b"\x06opaque", b"\xfbopaque"),
        handshake(flags=FLAGS | mysql.LENENC_AUTH).replace(b"\x06opaque", b"\xfc\x06\0opaque"),
        handshake(flags=FLAGS | mysql.CONNECT_ATTRS)[:-1],
        handshake(flags=FLAGS | mysql.CONNECT_ATTRS)[:-7] + b"\x03\x04key",
    ],
)
def test_wrong_account_catalog_and_malformed_fields_never_reach_backend_auth(handshake_payload):
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as front:
        assert read_packet(front) == packet(backend.greeting)
        front.sendall(packet(handshake_payload, 1))
        assert front.recv(1) == b""
        assert backend.handshakes.empty()


@pytest.mark.parametrize("bit", [5, 11, 26, 29, 30])
def test_tls_compression_and_unknown_flags_rejected(bit):
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as front:
        read_packet(front)
        front.sendall(packet(handshake(flags=FLAGS | (1 << bit)), 1))
        assert front.recv(1) == b""
        assert backend.handshakes.empty()


def test_native_client_local_files_advertisement_is_disabled_before_backend_auth():
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as front:
        read_packet(front)
        flags = FLAGS | mysql.LOCAL_FILES | mysql.LENENC_AUTH | mysql.CONNECT_ATTRS | (1 << 28)
        request = packet(handshake(flags=flags), 1)
        front.sendall(request)
        assert read_packet(front) == packet(OK, 2)
        forwarded = backend.handshakes.get(timeout=2)
        assert (
            forwarded
            == request[:4] + (flags & ~mysql.LOCAL_FILES).to_bytes(4, "little") + request[8:]
        )
        assert not int.from_bytes(forwarded[4:8], "little") & mysql.LOCAL_FILES
        command = packet(b"\x03select 1")
        front.sendall(command)
        assert read_packet(front) == command


@pytest.mark.parametrize("wire", [packet(handshake(), 0), b"\0\0\0\x01", b"\x01\x00\x01\x01"])
def test_bad_handshake_sequence_empty_and_oversized_packets_fail_closed(wire):
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as front:
        read_packet(front)
        front.sendall(wire)
        assert_closed(front)
        assert backend.handshakes.empty()


@pytest.mark.parametrize(
    "steps",
    [
        [(b"\x01\x03", None)],
        [(b"\xfemysql_native_password\0challenge\0", b"auth-token")],
        [(b"\x01\x04", b"\x02"), (b"\x01opaque-public-key\0", b"opaque-RSA-response")],
        [(b"\x02second_factor_plugin\0challenge", b"second-factor-response")],
        [(b"\xfemysql_native_password\0challenge\0", b"")],
    ],
)
def test_auth_switch_fast_auth_rsa_and_next_factor_roundtrips_remain_opaque(steps):
    with Backend(auth=steps) as backend, proxy(backend) as target, closing(client(target)) as front:
        read_packet(front)
        front.sendall(packet(handshake(flags=FLAGS | (1 << 28)), 1))
        backend.handshakes.get(timeout=2)
        sequence = 2
        for payload, reply in steps:
            assert read_packet(front) == packet(payload, sequence)
            sequence += 1
            if reply is not None:
                front.sendall(packet(reply, sequence))
                assert backend.auth_replies.get(timeout=2) == packet(reply, sequence)
                sequence += 1
        assert read_packet(front) == packet(OK, sequence)
        command = packet(b"\x0e")
        front.sendall(command)
        assert read_packet(front) == command


@pytest.mark.parametrize(
    "payload",
    [
        b"\x01",
        b"\x02project_db",
        b"\x03select 1",
        b"\x04table\0*",
        b"\x0e",
        b"\x16select ?",
        b"\x17" + b"\0" * 9,
        b"\x18" + b"\0" * 6 + b"opaque blob",
        b"\x19" + b"\0" * 4,
        b"\x1a" + b"\0" * 4,
        b"\x1b\0\0",
        b"\x1c" + b"\0" * 8,
        b"\x1f",
    ],
)
def test_query_and_prepared_command_allowlist_relays(payload):
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as front:
        connected(front, backend)
        command = packet(payload)
        front.sendall(command)
        assert read_packet(front) == command
        assert backend.commands.get(timeout=2) == command


@pytest.mark.parametrize(
    "command",
    [
        packet(b"\x02other_db"),
        packet(b"\x02PROJECT_DB"),
        packet(b"\x02project_db\0"),
        packet(b"\x11root\0opaque\0project_db\0"),
        packet(b"\x09"),
        packet(b"\x20opaque"),
        packet(b"\x0eextra"),
        packet(b"\x17short"),
        packet(b"\x03select 1", 1),
        packet(b""),
    ],
)
def test_catalog_switch_change_user_unknown_and_malformed_commands_not_forwarded(command):
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as front:
        connected(front, backend)
        # A wrong INIT_DB is held in full, even across several TCP reads.
        for offset in range(0, len(command), 3):
            try:
                front.sendall(command[offset : offset + 3])
            except OSError:
                break
        assert_closed(front)
        assert backend.commands.empty()


def test_prepared_long_data_large_continuation_is_data_even_when_first_byte_is_change_user():
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as front:
        front.settimeout(10)
        connected(front, backend)
        first = packet(b"\x18" + b"\0" * (mysql.MAX_PACKET - 1))
        continuation = packet(b"\x11root\0still-opaque-long-data", 1)
        front.sendall(first)
        assert read_packet(front) == first
        assert backend.commands.get(timeout=5) == first
        front.sendall(continuation)
        assert read_packet(front) == continuation
        assert backend.commands.get(timeout=5) == continuation
        front.sendall(packet(b"\x0e"))
        assert read_packet(front) == packet(b"\x0e")


def test_stream_parser_zero_terminator_sequence_and_logical_size_budget(monkeypatch):
    monkeypatch.setattr(mysql, "MAX_PACKET", 8)
    monkeypatch.setattr(mysql, "MAX_COMMAND", 16)
    gate = mysql._Commands(b"project_db")
    first = packet(b"\x18" + b"\0" * 7)
    raw = first + packet(b"", 1) + packet(b"\x0e")
    assert b"".join(gate.feed(raw[offset : offset + 1]) for offset in range(len(raw))) == raw
    for ending in (packet(b"x", 0), packet(b"\x11" * 8, 1) + packet(b"x", 2)):
        gate = mysql._Commands(b"project_db")
        assert gate.feed(first) == first
        with pytest.raises(_Rejected):
            gate.feed(ending)


def test_connection_budget_startup_timeout_and_close_reap_workers():
    with Backend() as backend:
        target = proxy(backend, max_connections=1, startup_timeout=0.25).start()
        with closing(client(target)) as front:
            read_packet(front)
            with closing(client(target)) as refused:
                assert refused.recv(1) == b""
            assert front.recv(1) == b""
            assert backend.handshakes.empty()
        deadline = time.monotonic() + 2
        while target.active_connections and time.monotonic() < deadline:
            time.sleep(0.01)
        assert target.active_connections == 0
        with closing(client(target)) as front:
            connected(front, backend)
            target.close()
            assert front.recv(1) == b""
            assert target.active_connections == 0
        target.close()
        with pytest.raises(SourceError, match="DATABASE_PROXY_CLOSED"):
            target.start()


def test_idle_connection_times_out():
    with (
        Backend() as backend,
        proxy(backend, idle_timeout=0.15) as target,
        closing(client(target)) as front,
    ):
        connected(front, backend)
        assert front.recv(1) == b""


@pytest.mark.parametrize(
    "options",
    [
        {"tls": True},
        {"tls": None},
        {"host": "10.0.0.1"},
        {"port": 443},
        {"database": ""},
        {"user": "root\0project_role"},
        {"user": "x" * 65},
        {"max_connections": 9},
    ],
)
def test_unsupported_tls_or_invalid_target_never_start(options):
    arguments = {
        "host": "127.0.0.1",
        "port": 3306,
        "database": "project_db",
        "user": "project_role",
    }
    arguments.update(options)
    with pytest.raises(SourceError, match="DATABASE_PROXY_"):
        mysql.MySQLTargetProxy(**arguments)

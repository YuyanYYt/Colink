"""Real TCP tests of startup routing, cancellation scope and proxy teardown."""

import queue
import socket
import struct
import threading
import time
from contextlib import closing

import pytest

from code_context.postgres_target_proxy import (
    CANCEL_REQUEST,
    GSS_REQUEST,
    SSL_REQUEST,
    PostgresTargetProxy,
)
from code_context.source_access import SourceError


def packet(code, body=b""):
    return struct.pack("!II", len(body) + 8, code) + body


def startup(parameters=None, protocol=196608):
    parameters = parameters or [(b"user", b"project_role"), (b"database", b"project_db")]
    return packet(
        protocol, b"".join(name + b"\0" + value + b"\0" for name, value in parameters) + b"\0"
    )


def message(tag, body):
    return tag + struct.pack("!I", len(body) + 4) + body


def receive(connection, count):
    result = bytearray()
    while len(result) < count:
        part = connection.recv(count - len(result))
        if not part:
            return bytes(result)
        result.extend(part)
    return bytes(result)


class Backend:
    def __init__(self, key=b"key1", *, fragment=False, finish_payload=None):
        self.key, self.fragment, self.finish_payload = key, fragment, finish_payload
        self.packets = queue.Queue()
        self.closed = threading.Event()
        self.connections, self.workers = [], []
        self.lock = threading.Lock()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(16)
        self.listener.settimeout(0.1)
        self.greeting = (
            message(b"R", struct.pack("!I", 0))
            + message(b"K", struct.pack("!I", 12345) + key)
            + message(b"Z", b"I")
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

    def _serve(self, connection):
        try:
            connection.settimeout(2)
            header = receive(connection, 4)
            if len(header) != 4:
                return
            size = struct.unpack("!I", header)[0]
            body = receive(connection, size - 4)
            self.packets.put(header + body)
            if struct.unpack("!I", body[:4])[0] == CANCEL_REQUEST:
                return
            if self.fragment:
                for byte in self.greeting:
                    connection.sendall(bytes([byte]))
            else:
                connection.sendall(self.greeting)
            while not self.closed.is_set():
                raw = connection.recv(65536)
                if not raw:
                    return
                if self.finish_payload is not None:
                    connection.sendall(self.finish_payload)
                    return
                connection.sendall(raw)
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
    return PostgresTargetProxy("127.0.0.1", backend.port, "project_db", "project_role", **options)


def client(target):
    connection = socket.create_connection((target.host, target.port), timeout=2)
    connection.settimeout(2)
    return connection


@pytest.mark.parametrize("protocol", [196608, 196610])
def test_exact_target_relays_fragmented_startup_auth_and_opaque_bytes(protocol):
    with (
        Backend(fragment=True) as backend,
        proxy(backend) as target,
        closing(client(target)) as frontend,
    ):
        request = startup(protocol=protocol)
        for offset in range(0, len(request), 3):
            frontend.sendall(request[offset : offset + 3])
        assert receive(frontend, len(backend.greeting)) == backend.greeting
        assert backend.packets.get(timeout=2) == request
        sql = b"Q\0\0\0\x17select 'opaque SQL';\0"
        frontend.sendall(sql)
        assert receive(frontend, len(sql)) == sql


@pytest.mark.parametrize(
    "parameters",
    [
        [(b"user", b"admin"), (b"database", b"project_db")],
        [(b"user", b"project_role"), (b"database", b"other_db")],
        [(b"user", b"project_role")],
        [(b"database", b"project_db")],
        [(b"user", b"project_role"), (b"user", b"admin"), (b"database", b"project_db")],
        [(b"user", b"project_role"), (b"database", b"other_db"), (b"database", b"project_db")],
        [(b"user", b"project_role"), (b"database", b"project_db"), (b"replication", b"false")],
        [(b"user", b"PROJECT_ROLE"), (b"database", b"project_db")],
    ],
)
def test_other_database_user_duplicate_and_replication_never_contact_backend(parameters):
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as frontend:
        frontend.sendall(startup(parameters))
        assert frontend.recv(1) == b""
        assert backend.packets.empty()


@pytest.mark.parametrize("code", [SSL_REQUEST, GSS_REQUEST])
def test_plaintext_loopback_negotiation_returns_n_then_requires_valid_target(code):
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as frontend:
        frontend.sendall(packet(code))
        assert frontend.recv(1) == b"N"
        frontend.sendall(startup())
        assert receive(frontend, len(backend.greeting)) == backend.greeting


def test_repeated_encryption_negotiation_is_rejected():
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as frontend:
        frontend.sendall(packet(SSL_REQUEST))
        assert frontend.recv(1) == b"N"
        frontend.sendall(packet(SSL_REQUEST))
        assert frontend.recv(1) == b""
        assert backend.packets.empty()


@pytest.mark.parametrize(
    "startup_packet",
    [
        startup(protocol=196609),
        packet(123),
        struct.pack("!I", 10001),
        struct.pack("!I", 7),
        packet(196608, b"user\0project_role\0database\0project_db\0"),
        packet(196608, b"user\0project_role\0database\0project_db\0\0extra"),
    ],
)
def test_unknown_protocol_oversized_and_malformed_startup_rejected(startup_packet):
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as frontend:
        frontend.sendall(startup_packet)
        assert frontend.recv(1) == b""
        assert backend.packets.empty()


@pytest.mark.parametrize("key", [b"key1", b"k" * 32, b"k" * 256])
def test_cancel_request_only_for_active_backend_key_and_key_expires(key):
    with Backend(key) as backend, proxy(backend) as target:
        with closing(client(target)) as frontend:
            frontend.sendall(startup())
            assert receive(frontend, len(backend.greeting)) == backend.greeting
            backend.packets.get(timeout=2)
            with closing(client(target)) as cancel:
                request = packet(CANCEL_REQUEST, struct.pack("!I", 12345) + key)
                cancel.sendall(request)
                assert cancel.recv(1) == b""
                assert backend.packets.get(timeout=2) == request
            with closing(client(target)) as forged:
                forged.sendall(packet(CANCEL_REQUEST, struct.pack("!I", 12345) + b"wrong"))
                assert forged.recv(1) == b""
                assert backend.packets.empty()
        deadline = time.monotonic() + 2
        while target.active_connections and time.monotonic() < deadline:
            time.sleep(0.01)
        assert target.active_connections == 0
        with closing(client(target)) as expired:
            expired.sendall(packet(CANCEL_REQUEST, struct.pack("!I", 12345) + key))
            assert expired.recv(1) == b""
            assert backend.packets.empty()


def test_cancel_without_any_authorized_session_never_contacts_backend():
    with Backend() as backend, proxy(backend) as target, closing(client(target)) as frontend:
        frontend.sendall(packet(CANCEL_REQUEST, struct.pack("!I", 12345) + b"key1"))
        assert frontend.recv(1) == b""
        assert backend.packets.empty()


def test_maximum_connections_is_bounded_and_close_reaps_listener_and_workers():
    with Backend() as backend:
        target = proxy(backend, max_connections=2, startup_timeout=2).start()
        clients = [client(target), client(target)]
        try:
            deadline = time.monotonic() + 2
            while target.active_connections != 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert target.active_connections == 2
            with closing(client(target)) as refused:
                assert refused.recv(1) == b""
            target.close()
            assert target.closed
            assert target.active_connections == 0
            assert all(connection.recv(1) == b"" for connection in clients)
            assert backend.packets.empty()
            target.close()
            with pytest.raises(SourceError, match="DATABASE_PROXY_CLOSED"):
                target.start()
        finally:
            target.close()
            for connection in clients:
                connection.close()


def test_incomplete_startup_timeout_never_connects_backend():
    with (
        Backend() as backend,
        proxy(backend, startup_timeout=0.1) as target,
        closing(client(target)) as frontend,
    ):
        frontend.sendall(b"\0\0")
        assert frontend.recv(1) == b""
        assert backend.packets.empty()


def test_idle_authenticated_connection_times_out_and_expires_cancel_key():
    with (
        Backend() as backend,
        proxy(backend, idle_timeout=0.1) as target,
        closing(client(target)) as frontend,
    ):
        frontend.sendall(startup())
        assert receive(frontend, len(backend.greeting)) == backend.greeting
        assert frontend.recv(1) == b""


def test_backend_eof_flushes_all_buffered_response_bytes():
    payload = b"result without parsing SQL\0" * 2000
    with (
        Backend(finish_payload=payload) as backend,
        proxy(backend) as target,
        closing(client(target)) as frontend,
    ):
        frontend.sendall(startup())
        assert receive(frontend, len(backend.greeting)) == backend.greeting
        frontend.sendall(b"query")
        assert receive(frontend, len(payload)) == payload
        assert frontend.recv(1) == b""


def test_tls_service_fails_closed_without_opening_proxy():
    with pytest.raises(SourceError, match="DATABASE_PROXY_TLS_UNSUPPORTED"):
        PostgresTargetProxy("127.0.0.1", 5432, "project_db", "project_role", tls=True)


@pytest.mark.parametrize("host", ["example.com", "192.168.1.1", "0.0.0.0", "/tmp"])
def test_backend_address_cannot_escape_loopback(host):
    with pytest.raises(SourceError, match="DATABASE_PROXY_TARGET_INVALID"):
        PostgresTargetProxy(host, 5432, "project_db", "project_role")

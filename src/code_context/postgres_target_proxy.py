"""Loopback PostgreSQL startup gate for one authorized project target.

Credentials and SQL remain opaque byte streams. The gate checks the database
and user before contacting PostgreSQL, then relays the connection. This is not
a replacement for server-side roles, grants or extension checks.
"""

import errno
import selectors
import socket
import struct
import threading
import time

from code_context.source_access import SourceError

PROTOCOLS = {196608, 196610}  # PostgreSQL 3.0 and 3.2.
SSL_REQUEST = 80877103
GSS_REQUEST = 80877104
CANCEL_REQUEST = 80877102
MAX_STARTUP = 10_000
MAX_BUFFER = 256 * 1024
MAX_AUTH_MESSAGE = 1024 * 1024


class _Rejected(Exception):
    pass


class _BackendStartup:
    """Read only startup framing and cancellation keys, never SQL messages."""

    def __init__(self, record_key):
        self.record_key = record_key
        self.buffer = bytearray()
        self.ready = False
        self.has_key = False

    def feed(self, raw):
        if self.ready:
            return
        self.buffer.extend(raw)
        while len(self.buffer) >= 5:
            tag = self.buffer[0]
            size = struct.unpack("!I", self.buffer[1:5])[0]
            if size < 4 or size > MAX_AUTH_MESSAGE:
                raise _Rejected
            if len(self.buffer) < size + 1:
                return
            payload = bytes(self.buffer[5 : size + 1])
            del self.buffer[: size + 1]
            if tag == ord("K"):
                if self.has_key or not 8 <= len(payload) <= 260:
                    raise _Rejected
                self.record_key(struct.unpack("!I", payload[:4])[0], payload[4:])
                self.has_key = True
            elif tag == ord("Z"):
                if payload not in {b"I", b"T", b"E"}:
                    raise _Rejected
                self.ready = True
                self.buffer.clear()
                return
            elif tag not in b"RSNEv":
                raise _Rejected


class PostgresTargetProxy:
    def __init__(
        self,
        host,
        port,
        database,
        user,
        *,
        tls=False,
        max_connections=8,
        startup_timeout=5,
        idle_timeout=300,
        connect_timeout=5,
    ):
        if tls is True:
            raise SourceError("DATABASE_PROXY_TLS_UNSUPPORTED: keep the configured TLS policy")
        if (
            tls is not False
            or host not in {"localhost", "127.0.0.1", "::1"}
            or type(port) is not int
            or not 1024 <= port <= 65535
            or any(
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or len(value.encode("utf-8")) > 63
                for value in (database, user)
            )
            or type(max_connections) is not int
            or not 1 <= max_connections <= 8
            or any(
                type(value) not in {int, float} or not 0 < value <= 3600
                for value in (startup_timeout, idle_timeout, connect_timeout)
            )
        ):
            raise SourceError("DATABASE_PROXY_TARGET_INVALID")
        self.host = "127.0.0.1"
        self.port = None
        self._backend = ("127.0.0.1" if host == "localhost" else host, port)
        self._database, self._user = database.encode("utf-8"), user.encode("utf-8")
        self._startup_timeout, self._idle_timeout = startup_timeout, idle_timeout
        self._connect_timeout = connect_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._max_connections = max_connections
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._listener = self._accept_thread = None
        self._sockets, self._workers, self._keys = set(), set(), {}

    @property
    def active_connections(self):
        with self._lock:
            return len(self._workers)

    @property
    def closed(self):
        return self._stop.is_set()

    def start(self):
        with self._lock:
            if self.closed:
                raise SourceError("DATABASE_PROXY_CLOSED")
            if self._listener is not None:
                return self
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind((self.host, 0))
                listener.listen(self._max_connections)
                listener.settimeout(0.2)
                self.port = listener.getsockname()[1]
                self._listener = listener
                self._accept_thread = threading.Thread(target=self._accept, daemon=True)
                self._accept_thread.start()
            except OSError:
                listener.close()
                raise SourceError("DATABASE_PROXY_START_FAILED") from None
        return self

    def _accept(self):
        while not self.closed:
            try:
                frontend, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                frontend.close()
                continue
            worker = threading.Thread(target=self._serve, args=(frontend,), daemon=True)
            with self._lock:
                if self.closed:
                    self._slots.release()
                    frontend.close()
                    break
                self._sockets.add(frontend)
                self._workers.add(worker)
                worker.start()

    def _receive(self, connection, count, deadline):
        raw = bytearray()
        while len(raw) < count and not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Rejected
            connection.settimeout(min(remaining, 0.2))
            try:
                part = connection.recv(count - len(raw))
            except TimeoutError:
                continue
            if not part:
                raise _Rejected
            raw.extend(part)
        if len(raw) != count:
            raise _Rejected
        return bytes(raw)

    def _startup(self, frontend):
        deadline, negotiated = time.monotonic() + self._startup_timeout, set()
        while True:
            header = self._receive(frontend, 4, deadline)
            size = struct.unpack("!I", header)[0]
            if not 8 <= size <= MAX_STARTUP:
                raise _Rejected
            body = self._receive(frontend, size - 4, deadline)
            code = struct.unpack("!I", body[:4])[0]
            if code in {SSL_REQUEST, GSS_REQUEST}:
                if size != 8 or code in negotiated:
                    raise _Rejected
                negotiated.add(code)
                frontend.sendall(b"N")
                continue
            if code == CANCEL_REQUEST:
                if not 16 <= size <= 268:
                    raise _Rejected
                return header + body, True
            if code not in PROTOCOLS or not body[4:].endswith(b"\x00\x00"):
                raise _Rejected
            parts = body[4:].split(b"\x00")[:-2]
            if not parts or len(parts) % 2:
                raise _Rejected
            parameters = {}
            for name, value in zip(parts[::2], parts[1::2], strict=True):
                if not name or name in parameters:
                    raise _Rejected
                parameters[name] = value
            if (
                parameters.get(b"database") != self._database
                or parameters.get(b"user") != self._user
                or b"replication" in parameters
            ):
                raise _Rejected
            return header + body, False

    def _backend_connection(self):
        family = socket.AF_INET6 if self._backend[0] == "::1" else socket.AF_INET
        connection = socket.socket(family, socket.SOCK_STREAM)
        with self._lock:
            if self.closed:
                connection.close()
                raise _Rejected
            self._sockets.add(connection)
        try:
            connection.setblocking(False)
            result = connection.connect_ex(self._backend)
            if result not in {0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY}:
                raise _Rejected
            deadline = time.monotonic() + self._connect_timeout
            with selectors.DefaultSelector() as selector:
                selector.register(connection, selectors.EVENT_WRITE)
                while not self.closed and time.monotonic() < deadline:
                    if selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                        if connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                            raise _Rejected
                        connection.settimeout(min(self._connect_timeout, 1))
                        return connection
            raise _Rejected
        except BaseException:
            with self._lock:
                self._sockets.discard(connection)
            connection.close()
            raise

    def _record_key(self, owner, pid, key):
        with self._lock:
            previous = self._keys.get((pid, key))
            if previous is not None and previous is not owner:
                raise _Rejected
            self._keys[(pid, key)] = owner

    def _relay(self, frontend, backend, owner):
        startup = _BackendStartup(lambda pid, key: self._record_key(owner, pid, key))
        pending = {frontend: bytearray(), backend: bytearray()}
        peer = {frontend: backend, backend: frontend}
        registered, eof = set(), set()
        activity = time.monotonic()
        deadline = activity + self._startup_timeout
        frontend.setblocking(False)
        backend.setblocking(False)
        with selectors.DefaultSelector() as selector:
            while not self.closed:
                now = time.monotonic()
                if now - activity >= self._idle_timeout or (not startup.ready and now >= deadline):
                    raise _Rejected
                if any(not pending[peer[connection]] for connection in eof):
                    return
                for connection in peer:
                    events = 0
                    if (
                        connection not in eof
                        and peer[connection] not in eof
                        and len(pending[peer[connection]]) < MAX_BUFFER
                    ):
                        events |= selectors.EVENT_READ
                    if pending[connection]:
                        events |= selectors.EVENT_WRITE
                    if events and connection in registered:
                        selector.modify(connection, events)
                    elif events:
                        selector.register(connection, events)
                        registered.add(connection)
                    elif connection in registered:
                        selector.unregister(connection)
                        registered.remove(connection)
                for key, events in selector.select(0.2):
                    connection = key.fileobj
                    if events & selectors.EVENT_WRITE:
                        try:
                            sent = connection.send(pending[connection])
                        except BlockingIOError:
                            sent = 0
                        if sent:
                            del pending[connection][:sent]
                            activity = time.monotonic()
                    if events & selectors.EVENT_READ:
                        try:
                            raw = connection.recv(65536)
                        except BlockingIOError:
                            continue
                        if not raw:
                            eof.add(connection)
                            continue
                        if connection is backend:
                            startup.feed(raw)
                        pending[peer[connection]].extend(raw)
                        activity = time.monotonic()

    def _serve(self, frontend):
        backend, owner = None, object()
        try:
            packet, cancel = self._startup(frontend)
            if cancel:
                pid = struct.unpack("!I", packet[8:12])[0]
                with self._lock:
                    if (pid, packet[12:]) not in self._keys:
                        raise _Rejected
            backend = self._backend_connection()
            backend.sendall(packet)
            if not cancel:
                self._relay(frontend, backend, owner)
        except (_Rejected, OSError, ValueError):
            # Connection refusal contains no target, credential or SQL body.
            pass
        finally:
            with self._lock:
                self._keys = {key: value for key, value in self._keys.items() if value is not owner}
                for connection in (frontend, backend):
                    if connection is not None:
                        self._sockets.discard(connection)
                        connection.close()
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def close(self):
        self._stop.set()
        with self._lock:
            if self._listener is not None:
                self._listener.close()
            connections, workers = list(self._sockets), list(self._workers)
            self._keys.clear()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        deadline = time.monotonic() + 2
        for worker in [self._accept_thread, *workers]:
            if worker is not None and worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

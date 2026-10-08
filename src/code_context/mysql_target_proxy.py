"""One-account, one-database gate for plaintext MySQL Protocol 4.1.

Authentication contents and SQL remain opaque. Only the initial account/catalog
and catalog-changing protocol commands are checked. The limited MySQL role
must separately prevent SQL access to other databases. TLS and compression
are rejected. LOCAL INFILE is disabled in the backend capability negotiation;
modern native clients advertise it even with --local-infile=0.
"""

import selectors
import threading
import time

from code_context.postgres_target_proxy import MAX_BUFFER, PostgresTargetProxy, _Rejected
from code_context.source_access import SourceError

PROTOCOL_41 = 1 << 9
CONNECT_WITH_DB = 1 << 3
LOCAL_FILES = 1 << 7
SECURE_CONNECTION = 1 << 15
PLUGIN_AUTH = 1 << 19
CONNECT_ATTRS = 1 << 20
LENENC_AUTH = 1 << 21
ALLOWED_FLAGS = sum(
    1 << bit
    for bit in (
        0,
        1,
        2,
        3,
        4,
        6,
        7,
        8,
        9,
        10,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        27,
        28,
        31,
    )
)
MAX_AUTH = 65536
MAX_PACKET = 0xFFFFFF
MAX_COMMAND = 32 * 1024 * 1024
# QUIT, INIT_DB, QUERY, FIELD_LIST, PING, PREPARE, EXECUTE, LONG_DATA,
# CLOSE, RESET, SET_OPTION, FETCH and RESET_CONNECTION.
COMMANDS = {1, 2, 3, 4, 14, 22, 23, 24, 25, 26, 27, 28, 31}


def _cstring(raw, offset):
    end = raw.find(b"\0", offset)
    if end < 0:
        raise _Rejected
    return raw[offset:end], end + 1


def _lenenc(raw, offset):
    if offset >= len(raw):
        raise _Rejected
    marker, offset = raw[offset], offset + 1
    if marker < 251:
        return marker, offset
    lengths = {252: 2, 253: 3, 254: 8}
    count = lengths.get(marker)
    if count is None or offset + count > len(raw):
        raise _Rejected
    value = int.from_bytes(raw[offset : offset + count], "little")
    if value < {252: 251, 253: 65536, 254: 16777216}[marker]:
        raise _Rejected
    return value, offset + count


def _handshake(raw, database, user):
    if len(raw) < 34:
        raise _Rejected
    flags = int.from_bytes(raw[:4], "little")
    if (
        flags & ~ALLOWED_FLAGS
        or flags & (PROTOCOL_41 | CONNECT_WITH_DB) != PROTOCOL_41 | CONNECT_WITH_DB
        or raw[9:32] != b"\0" * 23
        or raw[8] == 0
        or flags & LENENC_AUTH
        and not flags & PLUGIN_AUTH
    ):
        raise _Rejected
    account, offset = _cstring(raw, 32)
    if account != user:
        raise _Rejected
    if flags & LENENC_AUTH:
        size, offset = _lenenc(raw, offset)
        offset += size
    elif flags & SECURE_CONNECTION:
        if offset >= len(raw):
            raise _Rejected
        offset += 1 + raw[offset]
    else:
        _, offset = _cstring(raw, offset)
    if offset > len(raw):
        raise _Rejected
    catalog, offset = _cstring(raw, offset)
    if catalog != database:
        raise _Rejected
    if flags & PLUGIN_AUTH:
        _, offset = _cstring(raw, offset)
    if flags & CONNECT_ATTRS:
        size, offset = _lenenc(raw, offset)
        end, attributes = offset + size, 0
        if end > len(raw):
            raise _Rejected
        while offset < end:
            for _ in range(2):
                size, offset = _lenenc(raw, offset)
                offset += size
                if offset > end:
                    raise _Rejected
            attributes += 1
            if attributes > 128:
                raise _Rejected
    if offset != len(raw):
        raise _Rejected
    return flags


class _Commands:
    """Streaming packet framing; continuation bytes are never new commands."""

    def __init__(self, database):
        self.database = database
        self.header = bytearray()
        self.remaining = None
        self.size = self.sequence = self.total = 0
        self.continuation = False
        self.command = None
        self.catalog = bytearray()

    def feed(self, raw):
        offset, output = 0, bytearray()
        while offset < len(raw):
            if self.remaining is None:
                count = min(4 - len(self.header), len(raw) - offset)
                self.header.extend(raw[offset : offset + count])
                offset += count
                if len(self.header) < 4:
                    break
                size, sequence = int.from_bytes(self.header[:3], "little"), self.header[3]
                expected = (self.sequence + 1) & 255 if self.continuation else 0
                if sequence != expected or not self.continuation and size == 0:
                    raise _Rejected
                self.size, self.sequence, self.remaining = size, sequence, size
                self.total = self.total + size if self.continuation else size
                if self.total > MAX_COMMAND:
                    raise _Rejected
                if self.continuation:
                    output.extend(self.header)
                    self.header.clear()
                else:
                    self.command = None
                if self.remaining == 0:
                    self._end()
                    continue
            if self.command is None:
                if offset == len(raw):
                    break
                self.command = raw[offset]
                if self.command not in COMMANDS:
                    raise _Rejected
                exact = {1: 1, 2: len(self.database) + 1, 14: 1, 25: 5, 26: 5, 27: 3, 28: 9, 31: 1}
                minimum = {4: 2, 22: 2, 23: 10, 24: 7}
                if self.command in exact and self.size != exact[self.command]:
                    raise _Rejected
                if self.size < minimum.get(self.command, 1):
                    raise _Rejected
                if self.command != 2:
                    output.extend(self.header)
                    self.header.clear()
            count = min(self.remaining, len(raw) - offset)
            chunk = raw[offset : offset + count]
            if self.command == 2:
                self.catalog.extend(chunk)
            else:
                output.extend(chunk)
            offset += count
            self.remaining -= count
            if self.remaining == 0:
                if self.command == 2:
                    if self.catalog != b"\x02" + self.database:
                        raise _Rejected
                    output.extend(self.header)
                    output.extend(self.catalog)
                    self.header.clear()
                    self.catalog.clear()
                self._end()
        return bytes(output)

    def _end(self):
        self.continuation = self.size == MAX_PACKET
        self.remaining = None
        if not self.continuation:
            self.command = None
            self.total = 0


class MySQLTargetProxy(PostgresTargetProxy):
    """Reuse the bounded loopback lifecycle; implement MySQL framing separately."""

    def __init__(self, host, port, database, user, **options):
        if any(
            not isinstance(value, str) or not value or "\0" in value or len(value.encode()) > 64
            for value in (database, user)
        ):
            raise SourceError("DATABASE_PROXY_TARGET_INVALID")
        super().__init__(host, port, "mysql_target", "mysql_account", **options)
        self._database, self._user = database.encode(), user.encode()

    def _packet(self, connection, deadline, expected, *, empty=False):
        header = self._receive(connection, 4, deadline)
        size = int.from_bytes(header[:3], "little")
        if not int(not empty) <= size <= MAX_AUTH or header[3] != expected:
            raise _Rejected
        return header + self._receive(connection, size, deadline)

    def _authenticate(self, frontend, backend):
        deadline = time.monotonic() + self._startup_timeout
        greeting = self._packet(backend, deadline, 0)
        raw = greeting[4:]
        if raw[0] != 10:
            raise _Rejected
        _, offset = _cstring(raw, 1)
        if len(raw) < offset + 18:
            raise _Rejected
        capabilities = int.from_bytes(raw[offset + 13 : offset + 15], "little")
        if not capabilities & PROTOCOL_41:
            raise _Rejected
        frontend.sendall(greeting)
        handshake = self._packet(frontend, deadline, 1)
        flags = _handshake(handshake[4:], self._database, self._user)
        if flags & LOCAL_FILES:
            # MySQL 8+ advertises this bit even when the client disables uploads.
            # Tell the server this proxy does not support the file-upload phase.
            handshake = handshake[:4] + (flags & ~LOCAL_FILES).to_bytes(4, "little") + handshake[8:]
        backend.sendall(handshake)
        sequence = 2
        for _ in range(32):
            response = self._packet(backend, deadline, sequence)
            sequence = (sequence + 1) & 255
            frontend.sendall(response)
            raw = response[4:]
            if raw[0] == 0:
                if len(raw) < 7:
                    raise _Rejected
                return
            if raw[0] == 255:
                raise _Rejected
            if raw[0] == 1 and raw == b"\x01\x03":
                # caching_sha2_password fast auth is followed directly by OK.
                continue
            if raw[0] not in {1, 2, 254} or len(raw) < 2:
                raise _Rejected
            reply = self._packet(frontend, deadline, sequence, empty=True)
            sequence = (sequence + 1) & 255
            backend.sendall(reply)
        raise _Rejected

    def _relay(self, frontend, backend, owner):
        gate = _Commands(self._database)
        pending = {frontend: bytearray(), backend: bytearray()}
        peer = {frontend: backend, backend: frontend}
        registered, eof = set(), set()
        activity = time.monotonic()
        frontend.setblocking(False)
        backend.setblocking(False)
        with selectors.DefaultSelector() as selector:
            while not self.closed:
                if time.monotonic() - activity >= self._idle_timeout:
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
                            count = connection.send(pending[connection])
                        except BlockingIOError:
                            count = 0
                        if count:
                            del pending[connection][:count]
                            activity = time.monotonic()
                    if events & selectors.EVENT_READ:
                        try:
                            raw = connection.recv(65536)
                        except BlockingIOError:
                            continue
                        if not raw:
                            eof.add(connection)
                            continue
                        pending[peer[connection]].extend(
                            gate.feed(raw) if connection is frontend else raw
                        )
                        activity = time.monotonic()

    def _serve(self, frontend):
        backend = None
        try:
            backend = self._backend_connection()
            self._authenticate(frontend, backend)
            self._relay(frontend, backend, None)
        except (_Rejected, OSError, ValueError):
            pass
        finally:
            with self._lock:
                for connection in (frontend, backend):
                    if connection is not None:
                        self._sockets.discard(connection)
                        connection.close()
                self._workers.discard(threading.current_thread())
            self._slots.release()

"""Exact loopback listeners relaying only to a job's registered Unix endpoints.

Project code receives no TCP-bind permission. The coordinator binds numeric
127.0.0.1 itself. Connections and forwarding buffers are bounded; closing a job
closes listeners and active streams before detaching its workspace.
"""

import ctypes
import os
import select
import socket
import stat
import threading
from pathlib import Path

from code_context.execution_scope import MacKernel, ScopeError
from code_context.local_control import private_directory
from code_context.source_access import SourceError


class _AuditToken(ctypes.Structure):
    _fields_ = [("opaque", ctypes.c_uint32 * 8)]


class _PeerIdentity:
    """Use native BSM accessors for the kernel's immutable Unix peer token."""

    def __init__(self):
        try:
            self.kernel = MacKernel()
            self.bsm = ctypes.CDLL("/usr/lib/libbsm.dylib", use_errno=True)
            for name in ("euid", "pid", "pidversion"):
                accessor = getattr(self.bsm, "audit_token_to_" + name)
                accessor.argtypes = [_AuditToken]
                accessor.restype = ctypes.c_uint32 if name == "euid" else ctypes.c_int
        except (OSError, AttributeError, ScopeError):
            raise SourceError("SERVICE_PEER_VALIDATION_UNAVAILABLE") from None

    def matches(self, stream, resource_id):
        if type(resource_id) is not int or resource_id <= 1:
            return False
        try:
            # SDK sys/un.h: SOL_LOCAL=0, LOCAL_PEERTOKEN=0x006. Interpret
            # audit_token_t through libbsm, never fixed field offsets.
            raw = stream.getsockopt(0, 0x006, ctypes.sizeof(_AuditToken))
            if len(raw) != ctypes.sizeof(_AuditToken):
                return False
            token = _AuditToken.from_buffer_copy(raw)
            if self.bsm.audit_token_to_euid(token) != os.geteuid():
                return False
            pid = self.bsm.audit_token_to_pid(token)
            version = self.bsm.audit_token_to_pidversion(token)
            before = self.kernel.identity(pid)
            return (
                before is not None
                and before.version == version
                and self.kernel.resource_id(pid) == resource_id
                and self.kernel.identity(pid) == before
            )
        except (OSError, ValueError, ScopeError):
            return False


class LocalRelay:
    def __init__(self, ports, mount, *, scope_for=None):
        self.peer_identity = _PeerIdentity()
        self.scope_for = scope_for or (lambda: None)
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.active = set()
        self.connections = 0
        self.listeners = []
        self.endpoints = {}
        directory = Path(mount) / "endpoints"
        directory.mkdir(mode=0o700)
        self.directory = private_directory(directory)
        try:
            for port in ports:
                endpoint = str(directory / f"p-{port}.sock")
                if len(os.fsencode(endpoint)) > 90:
                    raise SourceError("SERVICE_ENDPOINT_PATH_TOO_LONG")
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.listeners.append(listener)
                listener.bind(("127.0.0.1", port))
                listener.listen(16)
                listener.settimeout(0.5)
                self.endpoints[str(port)] = endpoint
                threading.Thread(
                    target=self._accept, args=(listener, endpoint), daemon=True
                ).start()
        except OSError:
            self.close()
            raise SourceError("PORT_IN_USE: choose another locally registered port") from None
        except BaseException:
            self.close()
            raise

    def ready(self, port):
        endpoint = self.endpoints.get(str(port))
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(0.2)
                self._connect(stream, endpoint)
            return True
        except (OSError, TypeError, SourceError):
            return False

    def _connect(self, stream, endpoint):
        if self.closed.is_set():
            raise SourceError("SERVICE_RELAY_CLOSED")
        with self.directory.root_fd() as parent:
            before = os.stat(Path(endpoint).name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.geteuid():
                raise SourceError("SERVICE_ENDPOINT_CHANGED")
            stream.connect(endpoint)
            after = os.stat(Path(endpoint).name, dir_fd=parent, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise SourceError("SERVICE_ENDPOINT_CHANGED")
            if not self.peer_identity.matches(stream, self.scope_for()):
                raise SourceError("SERVICE_ENDPOINT_WRONG_JOB")

    def _accept(self, listener, endpoint):
        while not self.closed.is_set():
            try:
                stream, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self.lock:
                if self.connections >= 32 or self.closed.is_set():
                    stream.close()
                    continue
                self.active.add(stream)
                self.connections += 1
            threading.Thread(target=self._forward, args=(stream, endpoint), daemon=True).start()

    def _forward(self, client, endpoint):
        backend = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with self.lock:
            self.active.add(backend)
        try:
            backend.settimeout(1)
            self._connect(backend, endpoint)
            peers = {client: backend, backend: client}
            for stream in peers:
                stream.setblocking(False)
            buffers = {stream: bytearray() for stream in peers}
            readable = set(peers)
            while not self.closed.is_set() and (readable or any(buffers.values())):
                inputs = [stream for stream in readable if len(buffers[peers[stream]]) < 65536]
                outputs = [stream for stream, data in buffers.items() if data]
                read, write, _ = select.select(inputs, outputs, [], 0.5)
                for stream in read:
                    try:
                        data = stream.recv(min(16384, 65536 - len(buffers[peers[stream]])))
                    except BlockingIOError:
                        continue
                    if data:
                        buffers[peers[stream]].extend(data)
                    else:
                        readable.discard(stream)
                for stream in write:
                    try:
                        sent = stream.send(buffers[stream])
                        del buffers[stream][:sent]
                    except BlockingIOError:
                        continue
                for source, target in peers.items():
                    if source not in readable and not buffers[target]:
                        try:
                            target.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
        except (OSError, ValueError, SourceError):
            pass
        finally:
            with self.lock:
                self.active.discard(client)
                self.active.discard(backend)
                self.connections -= 1
            client.close()
            backend.close()

    def close(self):
        self.closed.set()
        for listener in self.listeners:
            listener.close()
        with self.lock:
            for stream in tuple(self.active):
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()

"""Private local UI control plane, deliberately not registered as MCP tools."""

import errno
import json
import os
import secrets
import socket
import stat
import sys
import threading
import time
from pathlib import Path

from code_context.scanner import Scanner, _identity, _version
from code_context.source_access import SourceAccess, SourceError

MAX_CONTROL_BYTES = 65536


def private_directory(path: Path) -> SourceAccess:
    """Create only real directories, pin the final private state directory."""
    path = Path(os.path.abspath(path.expanduser()))
    descriptors, links = [], []
    try:
        fd = os.open(path.anchor, Scanner._directory_flags())
        descriptors.append(fd)
        for name in path.parts[1:]:
            try:
                child = os.open(name, Scanner._directory_flags(), dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(name, 0o700, dir_fd=fd)
                child = os.open(name, Scanner._directory_flags(), dir_fd=fd)
            descriptors.append(child)
            links.append((fd, name, _identity(os.fstat(child))))
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SourceError("UNSAFE_CONTROL_STATE: use a private owned directory")
        for parent, name, expected in links:
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
                raise SourceError("CONTROL_DIRECTORY_CHANGED: restart local control")
        return SourceAccess(path)
    except SourceError:
        raise
    except OSError:
        raise SourceError("UNSAFE_CONTROL_STATE: real private directories are required") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def write_state(directory: SourceAccess, name: str, payload: dict):
    if "/" in name or not name or name in {".", ".."}:
        raise SourceError("INVALID_CONTROL_STATE: invalid state name")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_CONTROL_BYTES:
        raise SourceError("CONTROL_METADATA_LIMIT: state exceeds its budget")
    temporary = ".state-" + secrets.token_hex(12)
    with directory.root_fd() as parent:
        try:
            previous = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous and (
            not stat.S_ISREG(previous.st_mode)
            or previous.st_uid != os.getuid()
            or previous.st_mode & 0o077
            or previous.st_nlink != 1
        ):
            raise SourceError("UNSAFE_CONTROL_STATE: existing state cannot be replaced")
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        except OSError:
            raise SourceError("CONTROL_STATE_FAILED: private state was not committed") from None


def _read_state(directory: SourceAccess, name: str):
    if "/" in name or not name or name in {".", ".."}:
        raise SourceError("INVALID_CONTROL_STATE: invalid state name")
    try:
        with directory.root_fd() as parent:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_nlink != 1
                    or info.st_size > MAX_CONTROL_BYTES
                ):
                    raise SourceError("UNSAFE_CONTROL_STATE: state must be private and regular")
                raw = stream.read(MAX_CONTROL_BYTES + 1)
                if len(raw) > MAX_CONTROL_BYTES:
                    raise ValueError
                after = os.fstat(stream.fileno())
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _version(info) != _version(after) or _version(info) != _version(named):
                    raise SourceError("CONTROL_STATE_CHANGED: restart local control")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result, _version(info)
    except SourceError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise SourceError("CONTROL_UNAVAILABLE: local control is not ready") from None


def read_state(directory: SourceAccess, name: str):
    return _read_state(directory, name)[0]


def _receive(connection, *, stop=None, timeout=5):
    raw = bytearray()
    deadline = time.monotonic() + timeout if stop is not None else None
    while len(raw) <= MAX_CONTROL_BYTES:
        if stop is not None and (stop.is_set() or time.monotonic() >= deadline):
            raise ValueError("local receiver stopped or timed out")
        try:
            chunk = connection.recv(min(4096, MAX_CONTROL_BYTES + 1 - len(raw)))
        except TimeoutError:
            if stop is None:
                raise
            continue
        if not chunk:
            break
        raw.extend(chunk)
        if b"\n" in chunk:
            break
    if len(raw) > MAX_CONTROL_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid bounded message")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid message type")
    return value


def _send(connection, value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_CONTROL_BYTES:
        raw = b'{"ok":false,"error":"CONTROL_RESPONSE_LIMIT"}\n'
    connection.sendall(raw)


def _accepts_connections(connection):
    try:
        return bool(connection.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
    except OSError as exc:
        # Darwin exposes SO_ACCEPTCONN but its AF_UNIX sockets reject it with
        # ENOPROTOOPT. listen() has already succeeded; the pinned FD/path and
        # accepting thread are checked separately. Do not mask other failures.
        if (
            sys.platform != "darwin"
            or connection.family != socket.AF_UNIX
            or exc.errno != errno.ENOPROTOOPT
        ):
            raise
        return connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM


class LocalControl:
    def __init__(self, state_dir: Path, socket_dir: Path, handler, *, on_disconnect=None):
        self.state = private_directory(state_dir)
        self.socket_directory = private_directory(socket_dir)
        self.path = self.socket_directory.root / ("c-" + secrets.token_hex(8) + ".sock")
        if len(os.fsencode(self.path)) > 103:
            raise SourceError("CONTROL_PATH_TOO_LONG: use a shorter local control directory")
        self.token = secrets.token_urlsafe(32)
        self.handler = handler
        self.stop = threading.Event()
        self.socket = None
        self.thread = None
        self.socket_identity = None
        self.fd_identity = None
        self.state_version = None
        self._on_disconnect = on_disconnect or (lambda: None)
        self._loss_lock = threading.Lock()
        self._loss_notified = False
        self._started = False
        self._ready = threading.Event()
        self._connection_lock = threading.Lock()
        self._connection = None

    def _lost(self):
        # This may run inside Source/coordinator validation. Notify outside the
        # tiny bookkeeping lock; callbacks must only latch revocation, not wait
        # for coordinator locks or call this server over its own socket.
        self.stop.set()
        with self._loss_lock:
            notify = not self._loss_notified
            self._loss_notified = True
        if notify:
            try:
                self._on_disconnect()
            except Exception:
                pass  # The stop latch still rejects all future control requests.

    def is_alive(self):
        """Pinned local endpoint only, not evidence of remote tunnel health.

        A started endpoint's loss is sticky. A new instance/token and explicit
        local enable are required; restoring a path or saved token cannot revive
        its old grants. No self-RPC or coordinator lock is acquired here.
        """
        if not self._started:
            return False
        if not self._ready.is_set() and not self.stop.is_set():
            return False
        try:
            if (
                self.stop.is_set()
                or not self.token
                or self.socket is None
                or self.thread is None
                or not self.thread.is_alive()
            ):
                raise ValueError
            if _identity(
                os.fstat(self.socket.fileno())
            ) != self.fd_identity or not _accepts_connections(self.socket):
                raise ValueError
            with self.socket_directory.root_fd() as parent:
                info = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
                if (
                    _identity(info) != self.socket_identity
                    or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise ValueError
                saved, version = _read_state(self.state, "control.json")
                if (
                    version != self.state_version
                    or set(saved) != {"socket", "token", "pid"}
                    or saved["socket"] != str(self.path)
                    or type(saved["pid"]) is not int
                    or saved["pid"] != os.getpid()
                    or not isinstance(saved["token"], str)
                    or not secrets.compare_digest(saved["token"], self.token)
                ):
                    raise ValueError
                final = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
                if _identity(final) != self.socket_identity:
                    raise ValueError
            if (
                self.stop.is_set()
                or not self.thread.is_alive()
                or _identity(os.fstat(self.socket.fileno())) != self.fd_identity
            ):
                raise ValueError
            return True
        except (SourceError, OSError, ValueError, TypeError, KeyError, AttributeError):
            self._lost()
            return False

    def start(self):
        if self.stop.is_set():
            raise SourceError("CONTROL_STOPPED: create a new local control connection")
        if self.socket is not None:
            return
        with self.socket_directory.root_fd():
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.bind(str(self.path))
                os.chmod(self.path, 0o600, follow_symlinks=False)
                self.socket_identity = _identity(os.lstat(self.path))
                connection.listen(4)
                connection.settimeout(0.25)
                self.socket = connection
                self.fd_identity = _identity(os.fstat(connection.fileno()))
                write_state(
                    self.state,
                    "control.json",
                    {"socket": str(self.path), "token": self.token, "pid": os.getpid()},
                )
                _, self.state_version = _read_state(self.state, "control.json")
            except (OSError, SourceError):
                self._lost()
                connection.close()
                raise SourceError("CONTROL_START_FAILED: local control could not start") from None
        self.thread = threading.Thread(target=self._run, name="colink-local-control", daemon=True)
        self._started = True
        self.thread.start()
        if not self._ready.wait(1) or not self.is_alive():
            self._lost()
            raise SourceError("CONTROL_START_FAILED: local control did not become ready")

    def _run(self):
        self._ready.set()
        try:
            while self.is_alive():
                try:
                    connection, _ = self.socket.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with self._connection_lock:
                    # accept() can finish concurrently with close(). Never
                    # adopt a new receiver after close has snapshotted the old
                    # one; otherwise a partial message can outlive shutdown.
                    if self.stop.is_set():
                        connection.close()
                        break
                    self._connection = connection
                with connection:
                    try:
                        # Some native select()/close races do not wake a
                        # timeout-mode recv promptly. Poll the stop latch with
                        # a bounded interval and an absolute request deadline.
                        connection.settimeout(0.25)
                        request = _receive(connection, stop=self.stop)
                        if not self.is_alive():
                            raise SourceError("CONTROL_UNAVAILABLE: local control has stopped")
                        token = request.get("token")
                        if not isinstance(token, str) or not secrets.compare_digest(
                            token, self.token
                        ):
                            self._reply(
                                connection, {"ok": False, "error": "CONTROL_NOT_AUTHORIZED"}
                            )
                            continue
                        if (
                            set(request) != {"token", "action", "parameters"}
                            or not isinstance(request["action"], str)
                            or not isinstance(request["parameters"], dict)
                        ):
                            raise ValueError
                        result = self.handler(request["action"], request["parameters"])
                        self._reply(connection, {"ok": True, "result": result})
                    except SourceError as exc:
                        self._reply(connection, {"ok": False, "error": str(exc)})
                    except (OSError, ValueError, TypeError, RecursionError):
                        self._reply(connection, {"ok": False, "error": "INVALID_CONTROL_REQUEST"})
                    finally:
                        with self._connection_lock:
                            if self._connection is connection:
                                self._connection = None
        except Exception:
            pass  # Unexpected handler/listener failure is sticky, without input traceback.
        finally:
            self._lost()

    @staticmethod
    def _reply(connection, value):
        try:
            _send(connection, value)
        except OSError:
            pass

    def close(self):
        self._lost()
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self.socket is not None:
            self.socket.close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(1)
        # Remove only this endpoint, pinned by identity. Never delete an unrelated
        # path or the recovery/state directory. Stale state has an expired token.
        with self.socket_directory.root_fd() as parent:
            try:
                current = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
                if _identity(current) == self.socket_identity and stat.S_ISSOCK(current.st_mode):
                    os.unlink(self.path.name, dir_fd=parent)
            except FileNotFoundError:
                pass
        self.token = ""


def control_request(state_dir: Path, action: str, parameters=None):
    directory = SourceAccess(state_dir)
    state = read_state(directory, "control.json")
    try:
        path, token = state["socket"], state["token"]
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(8)
            connection.connect(path)
            _send(connection, {"token": token, "action": action, "parameters": parameters or {}})
            response = _receive(connection)
        if response.get("ok") is not True:
            raise SourceError(response.get("error", "CONTROL_FAILED"))
        return response["result"]
    except SourceError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise SourceError("CONTROL_UNAVAILABLE: local runtime is not running") from None

"""Private local UI control plane, deliberately not registered as MCP tools."""

import json
import os
import secrets
import socket
import stat
import threading
from pathlib import Path

from code_context.scanner import Scanner, _identity
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


def read_state(directory: SourceAccess, name: str):
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
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result
    except SourceError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise SourceError("CONTROL_UNAVAILABLE: local control is not ready") from None


def _receive(connection):
    raw = bytearray()
    while len(raw) <= MAX_CONTROL_BYTES:
        chunk = connection.recv(min(4096, MAX_CONTROL_BYTES + 1 - len(raw)))
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


class LocalControl:
    def __init__(self, state_dir: Path, socket_dir: Path, handler):
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

    def start(self):
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
                write_state(
                    self.state,
                    "control.json",
                    {"socket": str(self.path), "token": self.token, "pid": os.getpid()},
                )
            except (OSError, SourceError):
                connection.close()
                raise SourceError("CONTROL_START_FAILED: local control could not start") from None
        self.thread = threading.Thread(target=self._run, name="colink-local-control", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(5)
                try:
                    request = _receive(connection)
                    token = request.get("token")
                    if not isinstance(token, str) or not secrets.compare_digest(token, self.token):
                        _send(connection, {"ok": False, "error": "CONTROL_NOT_AUTHORIZED"})
                        continue
                    if (
                        set(request) != {"token", "action", "parameters"}
                        or not isinstance(request["action"], str)
                        or not isinstance(request["parameters"], dict)
                    ):
                        raise ValueError
                    result = self.handler(request["action"], request["parameters"])
                    _send(connection, {"ok": True, "result": result})
                except SourceError as exc:
                    _send(connection, {"ok": False, "error": str(exc)})
                except (OSError, ValueError, TypeError, RecursionError):
                    try:
                        _send(connection, {"ok": False, "error": "INVALID_CONTROL_REQUEST"})
                    except OSError:
                        pass

    def close(self):
        self.stop.set()
        if self.socket is not None:
            self.socket.close()
        if self.thread is not None:
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

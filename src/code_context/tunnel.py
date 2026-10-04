"""A narrow launcher for the official outbound-only tunnel client.

Profiles contain no credentials. No shell evaluates the .env file; only the
OPENAI_API_KEY assignment is read and it is passed to the child's environment.
This module never creates a Platform tunnel or changes a ChatGPT connection.
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import httpx

from code_context.client import SyncError
from code_context.local import read_local_mirror_status
from code_context.models import validate_project
from code_context.scanner import Scanner

_TUNNEL_ID = re.compile(r"tunnel_[A-Za-z0-9_-]{16,128}")
_KEY = re.compile(r"sk-[A-Za-z0-9_-]{20,4096}")
_SAFE_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
}


def _read_private(path: Path) -> str:
    """Reject symlinks, shared permissions, hardlinks, oversized or non-text files."""
    try:
        fd = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > 65536
            ):
                raise SyncError("configuration must be a private, owned regular file (mode 600)")
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise SyncError("configuration exceeds the size limit")
            return raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise SyncError(
            "cannot safely read private configuration; check its path and permissions"
        ) from exc


def read_runtime_key(env_file: Path) -> str:
    values = []
    for line in _read_private(env_file).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if separator and name.strip() == "OPENAI_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
                value = value[1:-1]
            values.append(value)
    if len(values) != 1 or _KEY.fullmatch(values[0]) is None:
        raise SyncError("set exactly one valid OPENAI_API_KEY assignment in the private env file")
    return values[0]


def _command(root: Path, project: str, data: Path) -> str:
    return shlex.join(
        [
            str(Path(sys.executable).absolute()),
            "-m",
            "code_context",
            "local",
            "--root",
            str(root),
            "--project",
            project,
            "--data-dir",
            str(data),
        ]
    )


def _profile(root: Path, project: str, data: Path, tunnel_id: str, directory: Path) -> dict:
    return {
        "config_version": 1,
        "control_plane": {
            "base_url": "https://api.openai.com",
            "tunnel_id": tunnel_id,
            "api_key": "env:CONTROL_PLANE_API_KEY",
        },
        "health": {"listen_addr": "127.0.0.1:0", "url_file": str(directory / "health.url")},
        "admin_ui": {"open_browser": False},
        "log": {"level": "warn", "format": "json"},
        "mcp": {"commands": [{"channel": "main", "command": _command(root, project, data)}]},
    }


def prepare_profile(root: Path, project: str, data_dir: Path, tunnel_id: str, output: Path) -> dict:
    """Create a local JSON/YAML-compatible official profile; no network or key read."""
    if output.suffix != ".yaml":
        raise SyncError("official tunnel-client profiles must use the .yaml extension")
    project = validate_project(project)
    if _TUNNEL_ID.fullmatch(tunnel_id) is None:
        raise SyncError("provide the tunnel_id obtained from Platform tunnel settings")
    data = data_dir.expanduser().resolve()
    scanner = Scanner(root.expanduser(), excluded_roots=(data,))
    existing = read_local_mirror_status(data)
    if existing["initialized"] and (
        existing.get("root") != str(scanner.root) or existing.get("project_id") != project
    ):
        raise SyncError("data directory is already bound to a different source or project")
    output = output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = output.parent.resolve() / output.name
    profile = _profile(scanner.root, project, data, tunnel_id, output.parent)
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SyncError(
            "profile already exists; choose a new output path instead of overwriting it"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(profile, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {
        "profile": str(output),
        "project_id": project,
        "root": str(scanner.root),
        "tunnel_id": tunnel_id,
        "remote_connection_started": False,
    }


def load_profile(path: Path, *, verify_source: bool = True) -> tuple[dict, Path, str, Path]:
    """Accept only our exact one-project, official-host, stdio profile shape."""
    if path.suffix != ".yaml":
        raise SyncError("official tunnel-client profiles must use the .yaml extension")
    try:
        profile = json.loads(_read_private(path))
        command = profile["mcp"]["commands"][0]["command"]
        args = shlex.split(command)
        if (
            len(args) != 10
            or args[:4] != [str(Path(sys.executable).absolute()), "-m", "code_context", "local"]
            or args[4] != "--root"
            or args[6] != "--project"
            or args[8] != "--data-dir"
        ):
            raise ValueError
        root, project, data = Path(args[5]), validate_project(args[7]), Path(args[9])
        tunnel_id = profile["control_plane"]["tunnel_id"]
        if (
            not root.is_absolute()
            or not data.is_absolute()
            or _TUNNEL_ID.fullmatch(tunnel_id) is None
        ):
            raise ValueError
        expected = _profile(
            root, project, data, tunnel_id, path.expanduser().absolute().parent.resolve()
        )
        if profile != expected:
            raise ValueError
        if ".." in root.parts or data.resolve() != data:
            raise ValueError
        if verify_source and Scanner(root, excluded_roots=(data,)).root != root:
            raise ValueError
        existing = read_local_mirror_status(data)
        if existing["initialized"] and (
            existing.get("root") != str(root) or existing.get("project_id") != project
        ):
            raise ValueError
        return profile, root, project, data
    except (KeyError, IndexError, TypeError, ValueError, RecursionError) as exc:
        raise SyncError(
            "invalid or expanded-scope tunnel profile; recreate it with tunnel-init"
        ) from exc


def _client_path(client: str) -> str:
    candidate = shutil.which(client)
    if candidate is None:
        raise SyncError("official tunnel-client was not found; pass its executable with --client")
    return str(Path(candidate).resolve())


def launch_tunnel(action: str, profile_file: Path, env_file: Path, client: str) -> int:
    if action not in {"doctor", "run"}:
        raise SyncError("unsupported tunnel action")
    load_profile(profile_file)
    executable = _client_path(client)
    key = read_runtime_key(env_file)
    env = {name: value for name, value in os.environ.items() if name in _SAFE_ENV}
    env["CONTROL_PLANE_API_KEY"] = key
    # Signed, self-contained applications must not grow __pycache__ inside the
    # bundle when the tunnel starts its stdio Python child.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    args = [
        executable,
        action,
        "--profile-file",
        str(profile_file.expanduser().absolute()),
        "--control-plane.base-url",
        "https://api.openai.com",
        "--control-plane.api-key",
        "env:CONTROL_PLANE_API_KEY",
        "--allow-remote-ui=false",
        "--health.listen-addr",
        "127.0.0.1:0",
        "--log.http-raw-unsafe=false",
        "--harpoon.capture-payloads=false",
        "--mcp.stdio-send-initialized-notification=true",
    ]
    if action == "doctor":
        args.append("--explain")
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncError(
                "tunnel doctor could not complete; check the client and network"
            ) from exc
        print((result.stdout + result.stderr).replace(key, "[redacted]"), end="", flush=True)
        return result.returncode
    # Child flags and diagnostics never contain the actual key. Raw HTTP/payload
    # logging is disabled; also redact the key defensively before displaying output.
    try:
        process = subprocess.Popen(
            args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
    except OSError as exc:
        raise SyncError("could not start official tunnel-client") from exc
    try:
        for line in process.stdout:
            print(line.replace(key, "[redacted]"), end="", flush=True)
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()


def tunnel_status(profile_file: Path) -> dict:
    # Offline inspection must remain possible after the source was moved/deleted.
    # Starting a connection still uses the full, nofollow source verification.
    profile, _, project, data = load_profile(profile_file, verify_source=False)
    result = {
        "project_id": project,
        "tunnel_id": profile["control_plane"]["tunnel_id"],
        "local_mirror": read_local_mirror_status(data),
        "health_reachable": False,
        "healthy": False,
        "ready": False,
        "chatgpt_web_verified": False,
    }
    url_file = Path(profile["health"]["url_file"])
    if not url_file.exists():
        return result
    try:
        url = url_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", url) is None:
            raise SyncError("health URL is not a loopback endpoint")
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as http:
            health = http.get(url + "/healthz")
            ready = http.get(url + "/readyz")
        result.update(
            health_reachable=True,
            healthy=health.status_code == 200,
            ready=ready.status_code == 200,
            admin_ui_url=url + "/ui",
        )
    except (OSError, UnicodeError, httpx.HTTPError, ValueError):
        pass
    return result

"""Bounded native tool identities and exact Mach-O dependency read paths.

The caller supplies the application's already trusted installed-tool inventory.
Only fixed version arguments and /usr/bin/otool are executed, with no user
configuration or inherited environment. This inspects static Mach-O links; it
does not claim to enumerate arbitrary plugins loaded later by project code.
No installation directory is added by this module to a sandbox read policy.
"""

import hashlib
import json
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from code_context.execution_environment import _clean_environment, _roots, _trusted
from code_context.source_access import SourceError

MAX_PROBE_BYTES = 64 * 1024
PROBE_SECONDS = 3
TOTAL_SECONDS = 15
MAX_TOOLS = 16
MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_DEPTH = 16
MAX_CONTEXTS = 512
MAX_FINGERPRINT_BYTES = 64 * 1024
MACH_MAGIC = frozenset(
    {
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)
VERSION_ARGUMENTS = {
    "node": ("--version",),
    "npm": ("--version",),
    "python3": ("-I", "-S", "--version"),
    "java": ("-version",),
    "javac": ("-version",),
    "mvn": ("--version", "--settings", "/dev/null", "--global-settings", "/dev/null"),
    "mysql": ("--no-defaults", "--no-login-paths", "--version"),
    "mysqld": ("--no-defaults", "--version"),
    "psql": ("--no-psqlrc", "--version"),
    "postgres": ("--version",),
    "pg_config": ("--version",),
    "redis-cli": ("--version",),
    "redis-server": ("--version",),
    "sqlite3": ("--version",),
}
_LINK_LINE = re.compile(r"^\s+(.+) \(compatibility version [0-9.]+, current version [0-9.]+\)$")
_VERSION = re.compile(r"\b(?:v)?\d+(?:\.\d+){1,3}(?:[-+._a-zA-Z0-9]*)?\b")


class ToolchainError(SourceError):
    """A tool inventory failure never discloses probe output or arguments."""


def _identity(info):
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _file(path):
    """Resolve then open the actual file without adopting a replacement."""
    try:
        requested = Path(path)
        if not requested.is_absolute() or "\x00" in str(requested):
            raise ValueError
        resolved = requested.resolve(strict=True)
        if not any(resolved.is_relative_to(root.resolve()) for root in _roots()):
            raise ValueError
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid not in {0, os.geteuid()}
                or before.st_mode & 0o022
                or not before.st_size
            ):
                raise ValueError
            magic = os.read(fd, 4)
            after = os.fstat(fd)
            if _identity(before) != _identity(after) or _identity(after) != _identity(
                resolved.stat()
            ):
                raise ValueError
        finally:
            os.close(fd)
        return str(resolved), _identity(before), magic in MACH_MAGIC
    except (OSError, ValueError, TypeError):
        raise ToolchainError("TOOLCHAIN_FILE_UNTRUSTED_OR_CHANGED") from None


def _run(argv, env):
    """Bound combined output and wall time, including a producer's open pipes."""
    process = None
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd="/",
            close_fds=True,
            start_new_session=True,
        )
        captured = bytearray()
        deadline = time.monotonic() + PROBE_SECONDS
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolchainError("TOOLCHAIN_PROBE_TIMEOUT")
                for key, _ in selector.select(min(remaining, 0.05)):
                    part = os.read(key.fileobj.fileno(), MAX_PROBE_BYTES + 1)
                    if not part:
                        selector.unregister(key.fileobj)
                        continue
                    if len(captured) + len(part) > MAX_PROBE_BYTES:
                        raise ToolchainError("TOOLCHAIN_PROBE_OUTPUT_LIMIT")
                    captured.extend(part)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.wait(timeout=remaining) != 0:
                raise ToolchainError("TOOLCHAIN_PROBE_FAILED")
        return captured.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ToolchainError("TOOLCHAIN_PROBE_FAILED") from None
    finally:
        if process is not None:
            # These are fixed version/otool commands from trusted installations,
            # isolated from user startup files; never project command execution.
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()


def _system(path):
    return path.startswith(("/usr/lib/", "/System/Library/"))


def _links(text):
    result = []
    for line in text.splitlines():
        if not line or line.endswith(":"):
            continue
        match = _LINK_LINE.fullmatch(line)
        if match is None or len(match[1]) > 4096 or any(ord(c) < 32 for c in match[1]):
            raise ToolchainError("TOOLCHAIN_LINK_FORMAT_UNSUPPORTED")
        result.append(match[1])
    return tuple(result)


def _rpaths(text):
    result = []
    lines = iter(text.splitlines())
    for line in lines:
        if line.strip() != "cmd LC_RPATH":
            continue
        found = None
        for following in lines:
            if following.strip().startswith("path "):
                found = following.strip()[5:].split(" (offset ", 1)[0]
                break
            if following.startswith("Load command") or following.strip().startswith("cmd "):
                break
        if not found or len(found) > 4096 or any(ord(c) < 32 for c in found):
            raise ToolchainError("TOOLCHAIN_RPATH_FORMAT_UNSUPPORTED")
        result.append(found)
    return tuple(result)


def _expand(path, loader, executable):
    if path.startswith("@loader_path/"):
        return str(Path(loader).parent / path[len("@loader_path/") :])
    if path.startswith("@executable_path/"):
        return str(Path(executable).parent / path[len("@executable_path/") :])
    if path.startswith("/"):
        return path
    raise ToolchainError("TOOLCHAIN_LIBRARY_UNRESOLVED")


def _resolve(name, loader, executable, run_paths):
    if name.startswith("@rpath/"):
        suffix = name[len("@rpath/") :]
        if not suffix or Path(suffix).is_absolute():
            raise ToolchainError("TOOLCHAIN_LIBRARY_UNRESOLVED")
        for root in run_paths:
            candidate = str(Path(root) / suffix)
            # dyld consults candidates in order. Do not skip an existing unsafe
            # candidate and silently select a different trusted installation.
            if _system(os.path.normpath(candidate)) or Path(candidate).exists():
                return candidate
        raise ToolchainError("TOOLCHAIN_LIBRARY_UNRESOLVED")
    return _expand(name, loader, executable)


def _symlinks(paths):
    """Pin link inodes encountered while expanding an actual install name.

    A directory symlink can contain a second, file symlink after its parent is
    resolved. Enumerating only the lexical parents misses that physical inode.
    Only metadata for these exact links is needed; their target directories do
    not become read roots.
    """
    result = {}
    for path in paths:
        pending = list(Path(path).parts[1:])
        current = Path("/")
        steps = links = 0
        while pending:
            steps += 1
            if steps > 256:
                raise ToolchainError("TOOLCHAIN_CLOSURE_LIMIT")
            part = pending.pop(0)
            if part == ".":
                continue
            if part == "..":
                current = current.parent
                continue
            candidate = current / part
            info = candidate.lstat()
            if not stat.S_ISLNK(info.st_mode):
                current = candidate
                continue
            links += 1
            if (
                links > 64
                or len(result) >= MAX_FILES
                or info.st_uid not in {0, os.geteuid()}
                or info.st_nlink != 1
            ):
                raise ToolchainError("TOOLCHAIN_CLOSURE_LIMIT")
            target = os.readlink(candidate)
            if len(target.encode()) > 4096 or "\x00" in target:
                raise ToolchainError("TOOLCHAIN_INSTALL_NAME_CHANGED")
            item = {"path": str(candidate), "target": target, "identity": _identity(info)}
            if _identity(candidate.lstat()) != item["identity"]:
                raise ToolchainError("TOOLCHAIN_INSTALL_NAME_CHANGED")
            previous = result.setdefault(str(candidate), item)
            if previous != item:
                raise ToolchainError("TOOLCHAIN_INSTALL_NAME_CHANGED")
            target_path = Path(target)
            current = Path("/") if target_path.is_absolute() else candidate.parent
            pending = (
                list(target_path.parts[1:] if target_path.is_absolute() else target_path.parts)
                + pending
            )
    return [item for _, item in sorted(result.items())]


def _inspect(paths, versions):
    if (
        sys.platform != "darwin"
        or not isinstance(paths, Mapping)
        or not 1 <= len(paths) <= MAX_TOOLS
    ):
        raise ToolchainError("TOOLCHAIN_PLATFORM_OR_INVENTORY_UNSUPPORTED")
    if any(name not in VERSION_ARGUMENTS for name in paths):
        raise ToolchainError("TOOLCHAIN_TOOL_UNSUPPORTED")
    architecture = platform.machine()
    if architecture not in {"arm64", "x86_64"}:
        raise ToolchainError("TOOLCHAIN_ARCHITECTURE_UNSUPPORTED")
    started = time.monotonic()
    tools, files, aliases, contexts, system_links = {}, {}, {}, set(), set()
    parsed = {}
    queue = []
    total_bytes = 0

    def budget():
        if time.monotonic() - started > TOTAL_SECONDS:
            raise ToolchainError("TOOLCHAIN_TOTAL_TIMEOUT")

    def add(path):
        nonlocal total_bytes
        canonical, identity, native = _file(path)
        if canonical in files:
            if files[canonical]["identity"] != identity:
                raise ToolchainError("TOOLCHAIN_FILE_CHANGED")
        else:
            total_bytes += identity["size"]
            if len(files) >= MAX_FILES or total_bytes > MAX_FILE_BYTES:
                raise ToolchainError("TOOLCHAIN_CLOSURE_LIMIT")
            files[canonical] = {"identity": identity, "native": native}
        alias = os.path.normpath(str(path))
        aliases[alias] = canonical
        return canonical

    for name, path in sorted(paths.items()):
        trusted, _ = _trusted(path, name)
        if not trusted:
            raise ToolchainError("TOOLCHAIN_EXECUTABLE_UNTRUSTED")
        canonical = add(trusted)
        # Preserve the caller's actual install name as an exact read path too.
        alias = os.path.normpath(str(Path(path).absolute()))
        aliases[alias] = canonical
        tools[name] = {
            "path": canonical,
            "requested_path": alias,
            "identity": files[canonical]["identity"],
        }
        if files[canonical]["native"]:
            queue.append((canonical, canonical, (), 0))
    env = _clean_environment({name: item["path"] for name, item in tools.items()})
    while queue:
        budget()
        loader, executable, inherited, depth = queue.pop(0)
        context = (loader, executable, inherited)
        if context in contexts:
            continue
        if len(contexts) >= MAX_CONTEXTS or depth > MAX_DEPTH:
            raise ToolchainError("TOOLCHAIN_CLOSURE_LIMIT")
        contexts.add(context)
        if loader not in parsed:
            prefix = ["/usr/bin/otool", "-arch", architecture]
            parsed[loader] = (
                _links(_run([*prefix, "-L", loader], env)),
                _rpaths(_run([*prefix, "-l", loader], env)),
            )
            if _file(loader)[1] != files[loader]["identity"]:
                raise ToolchainError("TOOLCHAIN_FILE_CHANGED")
        links, loader_paths = parsed[loader]
        run_paths = tuple(
            dict.fromkeys([*(_expand(p, loader, executable) for p in loader_paths), *inherited])
        )
        for name in links:
            install_name = os.path.normpath(_resolve(name, loader, executable, run_paths))
            if _system(install_name):
                system_links.add(install_name)
                continue
            canonical = add(install_name)
            if not files[canonical]["native"]:
                raise ToolchainError("TOOLCHAIN_LIBRARY_FORMAT_UNSUPPORTED")
            # A dylib's first -L entry can be its own install ID.
            if canonical != loader:
                queue.append((canonical, executable, run_paths, depth + 1))
    if versions:
        for name, tool in tools.items():
            budget()
            argv = [tool["path"], *VERSION_ARGUMENTS[name]]
            if name == "npm" and Path(tool["path"]).suffix == ".js":
                if "node" not in tools:
                    raise ToolchainError("TOOLCHAIN_NPM_RUNTIME_UNAVAILABLE")
                argv = [tools["node"]["path"], *argv]
            text = _run(argv, env).strip()
            if not _VERSION.search(text):
                raise ToolchainError("TOOLCHAIN_VERSION_UNAVAILABLE")
            tool["version"] = text
    budget()
    for path, item in files.items():
        if _file(path)[1] != item["identity"]:
            raise ToolchainError("TOOLCHAIN_FILE_CHANGED")
    symlinks = _symlinks(aliases)
    if len(files) + len(symlinks) > MAX_FILES:
        raise ToolchainError("TOOLCHAIN_CLOSURE_LIMIT")
    for alias, canonical in aliases.items():
        if str(Path(alias).resolve(strict=True)) != canonical:
            raise ToolchainError("TOOLCHAIN_INSTALL_NAME_CHANGED")
    libraries = [
        {
            "path": path,
            "identity": item["identity"],
            "install_names": sorted(alias for alias, target in aliases.items() if target == path),
        }
        for path, item in sorted(files.items())
        if path not in {tool["path"] for tool in tools.values()}
    ]
    result = {
        "tools": tools,
        "libraries": libraries,
        "system_links": sorted(system_links),
        "read_paths": sorted(set(aliases) | set(files)),
        "read_aliases": sorted(alias for alias, canonical in aliases.items() if alias != canonical),
        "read_metadata_paths": [item["path"] for item in symlinks],
        "symlinks": symlinks,
        "platform": {"system": "Darwin", "release": os.uname().release, "machine": architecture},
        "static_links_only": True,
    }
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(raw) > MAX_FINGERPRINT_BYTES:
        raise ToolchainError("TOOLCHAIN_FINGERPRINT_LIMIT")
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def effective_toolchain_fingerprint(paths):
    """Return actual fixed versions, pinned stat identities and dependency files.

    `paths` is a mapping such as {"node": trusted_node, "psql": trusted_psql}.
    The returned `read_paths` contains exact files, including install-name
    symlinks and their targets. Refresh before launch; this API does not cache.
    """
    try:
        return _inspect(paths, versions=True)
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, ToolchainError):
            raise
        raise ToolchainError("TOOLCHAIN_INSPECTION_FAILED") from None


def library_read_paths(paths):
    """Resolve the same bounded exact file closure without version probes."""
    try:
        return _inspect(paths, versions=False)["read_paths"]
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, ToolchainError):
            raise
        raise ToolchainError("TOOLCHAIN_INSPECTION_FAILED") from None


def library_read_policy(paths):
    """Exact library files plus raw install aliases/link metadata, no versions.

    Add `read_paths` as exact file grants. The sandbox's path canonicalization
    must not drop `read_metadata_paths`: append raw literal file-read-metadata
    grants for these link inodes, before controller protection deny rules.
    Never grant a directory subpath from an alias or metadata entry.
    """
    try:
        result = _inspect(paths, versions=False)
        return {key: result[key] for key in ("read_paths", "read_aliases", "read_metadata_paths")}
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, ToolchainError):
            raise
        raise ToolchainError("TOOLCHAIN_INSPECTION_FAILED") from None


def toolchain_unchanged(fingerprint, paths):
    """Fast stat/install-name validation of a previously created fingerprint.

    No process, version argument or otool is executed. Caller path-map changes,
    platform changes, malformed metadata and any identity/alias change return
    False. Refresh the complete fingerprint only when preparing a new plan.
    """
    try:
        if (
            sys.platform != "darwin"
            or not isinstance(fingerprint, dict)
            or not isinstance(paths, Mapping)
            or not 1 <= len(paths) <= MAX_TOOLS
            or fingerprint.get("static_links_only") is not True
        ):
            return False
        body = {key: value for key, value in fingerprint.items() if key != "sha256"}
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if len(raw) > MAX_FINGERPRINT_BYTES or hashlib.sha256(raw).hexdigest() != fingerprint.get(
            "sha256"
        ):
            return False
        if fingerprint.get("platform") != {
            "system": "Darwin",
            "release": os.uname().release,
            "machine": platform.machine(),
        }:
            return False
        tools = fingerprint["tools"]
        if not isinstance(tools, dict) or set(tools) != set(paths):
            return False
        records = {}
        aliases = {}
        for name, path in paths.items():
            if name not in VERSION_ARGUMENTS:
                return False
            trusted, _ = _trusted(path, name)
            item = tools[name]
            requested = os.path.normpath(str(Path(path).absolute()))
            if trusted != item["path"] or requested != item["requested_path"]:
                return False
            records[item["path"]] = item["identity"]
            aliases[requested] = item["path"]
        libraries = fingerprint["libraries"]
        if not isinstance(libraries, list) or len(libraries) + len(records) > MAX_FILES:
            return False
        for item in libraries:
            if item["path"] in records:
                return False
            records[item["path"]] = item["identity"]
            for alias in item["install_names"]:
                if alias in aliases and aliases[alias] != item["path"]:
                    return False
                aliases[alias] = item["path"]
        if sorted(set(aliases) | set(records)) != fingerprint["read_paths"]:
            return False
        if (
            sorted(alias for alias, canonical in aliases.items() if alias != canonical)
            != fingerprint["read_aliases"]
        ):
            return False
        for path, identity in records.items():
            if _file(path)[1] != identity:
                return False
        for alias, canonical in aliases.items():
            if str(Path(alias).resolve(strict=True)) != canonical:
                return False
        if _symlinks(aliases) != fingerprint["symlinks"] or fingerprint["read_metadata_paths"] != [
            item["path"] for item in fingerprint["symlinks"]
        ]:
            return False
        return True
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False

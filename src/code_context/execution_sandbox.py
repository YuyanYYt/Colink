"""Fail-closed macOS task disks, validated input export and SRT job adapter.

Only application-owned images are attached. File bodies are temporary task input,
never a historical mirror. Native disk size / APFS quota enforce storage limits;
CPU, RSS and process monitoring are separately labelled soft limits.
"""

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from code_context.local_control import private_directory, read_state, write_state
from code_context.policy import SECRET_PATTERNS, validate_path
from code_context.scanner import _version
from code_context.source_access import SourceError

GIB = 1024**3
HELPER = Path(__file__).parent / "resources/sandbox/helper.mjs"
SRT_VERSION = "0.0.78"
MAX_HELPER_FILES = 256
MAX_HELPER_BYTES = 8 * 1024**2
PROXY_PARENT = Path("/private/tmp")
SYSTEM_READS = (
    "/bin",
    "/usr/bin",
    "/usr/lib",
    "/System/Library",
    "/usr/share/locale",
    "/private/etc/ssl",
    "/private/etc/hosts",
    "/private/etc/services",
    "/private/var/select/sh",
    "/dev/null",
    "/dev/random",
    "/dev/urandom",
    "/dev/zero",
)


def _disk_identity(info):
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "birth_ns": int(info.st_birthtime * 10**9) if hasattr(info, "st_birthtime") else None,
        "uid": info.st_uid,
    }


def _run(argv, *, timeout=30):
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"},
        close_fds=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise SourceError("NATIVE_DISK_FAILED: private bounded APFS disk unavailable")
    return result.stdout


class BoundedDisk:
    """A fixed-size image with a private mount, verified before every handoff."""

    def __init__(self, root, name, *, size=2 * GIB, project_id=None, source_id=None):
        if platform.system() != "Darwin" or re.fullmatch(r"[a-z0-9_-]{1,80}", name) is None:
            raise SourceError("NATIVE_SANDBOX_UNAVAILABLE")
        self.state = private_directory(root)
        self.image = self.state.root / (name + ".sparsebundle")
        self.mount = self.state.root / (name + "-mount")
        self.device = None
        self.mount_underlying = None
        self.record = None
        if re.fullmatch(r"job-[a-f0-9]{32}", name) and project_id and source_id:
            with self.state.root_fd() as parent:
                self.record = {
                    "version": 1,
                    "job_id": name,
                    "project_id": project_id,
                    "source_id": source_id,
                    "image_basename": self.image.name,
                    "root_identity": _disk_identity(os.fstat(parent)),
                    "image_identity": None,
                    "mount": None,
                    "mount_underlying": None,
                    "device": None,
                    "container": None,
                    "volume": None,
                    "container_uuid": None,
                    "volume_uuid": None,
                    "mounted_identity": None,
                    "size": size,
                    "cleanup_verified": True,
                    "execution_started": False,
                }
            self._persist("creating")
        with self.state.root_fd():
            if self.image.exists() or self.mount.exists():
                raise SourceError("DISK_ALREADY_EXISTS: use a fresh job identity")
            # macOS 27's hdiutil compatibility path can produce an unmountable
            # APFS volume. Use the native replacement; no host-directory fallback.
            _run(
                [
                    "/usr/sbin/diskutil",
                    "image",
                    "create",
                    "blank",
                    "--size",
                    str(size),
                    "--volumeName",
                    "CoLink-" + name,
                    "--format",
                    "UDSB",
                    "--fs",
                    "APFS",
                    str(self.image),
                ]
            )
            os.chmod(self.image, 0o700)
            self.image_identity = (self.image.stat().st_dev, self.image.stat().st_ino)
            if self.record:
                self.record["image_identity"] = _disk_identity(self.image.lstat())
                self._persist("creating")
            if name != "cache":
                self.mount = Path(tempfile.mkdtemp(prefix="cl-w-", dir="/private/tmp"))
                under = self.mount.stat()
                self.mount_underlying = (under.st_dev, under.st_ino)
                if self.record:
                    self.record.update(
                        mount=str(self.mount), mount_underlying=_disk_identity(under)
                    )
                    self._persist("creating")
            info = plistlib.loads(
                _run(
                    [
                        "/usr/sbin/diskutil",
                        "image",
                        "attach",
                        "--nobrowse",
                        "--mountOptions",
                        "noowners",
                        "--mountPoint",
                        str(self.mount),
                        "--plist",
                        str(self.image),
                    ]
                )
            )
        entities = info.get("system-entities", [])
        mounted = [e for e in entities if e.get("mount-point") == str(self.mount)]
        disks = [
            e["dev-entry"] for e in entities if e.get("content-hint") == "GUID_partition_scheme"
        ]
        containers = [
            e["dev-entry"] for e in entities if e.get("content-hint") == "Apple_APFS_Container"
        ]
        if len(mounted) != 1 or len(disks) != 1 or len(containers) != 1:
            raise SourceError("UNVERIFIED_TASK_DISK")
        self.device, self.container = disks[0], containers[0]
        self.volume = mounted[0]["dev-entry"]
        self.size = size
        os.chmod(self.mount, 0o700)
        self.access = private_directory(self.mount)
        self.verify()
        if self.record:
            volume_info = plistlib.loads(
                _run(["/usr/sbin/diskutil", "info", "-plist", self.volume])
            )
            container_info = plistlib.loads(
                _run(["/usr/sbin/diskutil", "apfs", "list", "-plist", self.container])
            )["Containers"]
            if len(container_info) != 1 or not volume_info.get("VolumeUUID"):
                raise SourceError("UNVERIFIED_TASK_DISK")
            self.record.update(
                device=self.device,
                container=self.container,
                volume=self.volume,
                container_uuid=container_info[0]["APFSContainerUUID"],
                volume_uuid=volume_info["VolumeUUID"],
                mounted_identity=_disk_identity(self.mount.stat()),
            )
            self._persist("attached")

    def _persist(self, state):
        if getattr(self, "record", None):
            self.record.update(state=state, updated_at=time.time())
            write_state(self.state, self.record["job_id"] + ".disk.json", self.record)

    def mark_started(self):
        if self.record:
            self.record["cleanup_verified"] = False
            self.record["execution_started"] = True
            self._persist("attached")

    def mark_cleanup(self, verified):
        if self.record:
            self.record["cleanup_verified"] = verified is True
            self._persist("attached" if self.device else "detached")

    def verify(self):
        with self.state.root_fd(), self.access.root_fd():
            info = plistlib.loads(_run(["/usr/sbin/diskutil", "info", "-plist", self.volume]))
            if (
                info.get("MountPoint") != str(self.mount)
                or info.get("APFSContainerSize", 0) > self.size
                or info.get("FilesystemType") != "apfs"
                or not info.get("WritableVolume")
            ):
                raise SourceError("TASK_DISK_CHANGED")
        return self.mount

    def close(self):
        if self.device:
            self.verify()
            _run(["/usr/sbin/diskutil", "eject", self.device])
            self.device = None
            self._persist("detached")

    def retire(self):
        """Reclaim only this verified, detached, application-owned task image."""
        if self.device or self.image.name == "cache.sparsebundle":
            raise SourceError("TASK_DISK_STILL_REFERENCED")
        if self.record and not self.record["cleanup_verified"]:
            raise SourceError("TASK_DISK_CLEANUP_UNVERIFIED")
        self._persist("retiring")
        with self.state.root_fd():
            info = self.image.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != self.image_identity
            ):
                raise SourceError("TASK_DISK_RETIREMENT_IDENTITY_CHANGED")
            if not shutil.rmtree.avoids_symlink_attacks:
                raise SourceError("TASK_DISK_RETIREMENT_UNSUPPORTED")
            shutil.rmtree(self.image)
        if self.mount_underlying:
            info = self.mount.lstat()
            if (
                stat.S_ISDIR(info.st_mode)
                and (info.st_dev, info.st_ino) == self.mount_underlying
                and not any(self.mount.iterdir())
            ):
                self.mount.rmdir()
        self._persist("retired")


def export_input(
    source,
    destination: Path | None,
    *,
    relative="",
    max_bytes=256 * 1024**2,
    protected_paths=(),
    max_entries=20000,
    max_seconds=15,
):
    """Pin every directory/file, reject links, secrets and racing file identities."""
    if relative:
        validate_path(relative)
    if destination is not None:
        destination.mkdir(mode=0o700)
    manifest, directories, total, count = {}, {}, 0, 0
    started = time.monotonic()
    protected = tuple(Path(os.path.abspath(path)) for path in protected_paths)

    def check_budget():
        if count > max_entries or time.monotonic() - started > max_seconds:
            raise SourceError("EXECUTION_INPUT_LIMIT")

    def copy(directory, prefix, out, spec):
        nonlocal total, count
        if len(Path(prefix).parts) > 64:
            raise SourceError("EXECUTION_INPUT_LIMIT")
        # Iterate with a cap before sorting; huge directories and empty trees
        # cannot consume unbounded memory during a web request.
        names = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                count += 1
                check_budget()
                names.append(entry.name)
        names.sort()
        for name in names:
            check_budget()
            path = f"{prefix}/{name}" if prefix else name
            absolute = source.root / path
            if any(absolute == private or private in absolute.parents for private in protected):
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            is_dir = stat.S_ISDIR(info.st_mode)
            if source.scanner._path_problem(path, spec, is_dir):
                continue
            if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise SourceError("UNSAFE_EXECUTION_INPUT: symbolic and hard links are excluded")
            target = out / name if out is not None else None
            if is_dir:
                directories[path] = {"mode": stat.S_IMODE(info.st_mode)}
                if target is not None:
                    target.mkdir(mode=0o700)
                with source.scanner._directory(directory, name, path, info) as child:
                    copy(child, path, target, spec)
                if _version(info) != _version(
                    os.stat(name, dir_fd=directory, follow_symlinks=False)
                ):
                    raise SourceError("SOURCE_CHANGED: input directory changed")
            elif stat.S_ISREG(info.st_mode):
                if (
                    info.st_size > 32 * 1024**2
                    or len(manifest) >= 10000
                    or total + info.st_size > max_bytes
                ):
                    raise SourceError("EXECUTION_INPUT_LIMIT")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    actual = os.fstat(stream.fileno())
                    raw = stream.read(32 * 1024**2 + 1)
                    after = os.fstat(stream.fileno())
                if (
                    _version(actual) != _version(info)
                    or _version(after) != _version(info)
                    or _version(os.stat(name, dir_fd=directory, follow_symlinks=False))
                    != _version(info)
                ):
                    raise SourceError("SOURCE_CHANGED: input file changed")
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    text = None
                if text is not None and any(p.search(text) for p in SECRET_PATTERNS):
                    raise SourceError("EXECUTION_INPUT_SECRET: remove embedded credentials locally")
                if target is not None:
                    target.write_bytes(raw)
                    target.chmod(0o700 if info.st_mode & 0o111 else 0o600)
                    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
                manifest[path] = {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                    "mode": stat.S_IMODE(info.st_mode),
                    "type": "file",
                }
                total += len(raw)
            else:
                raise SourceError("UNSAFE_EXECUTION_INPUT: regular files and directories required")

    with source.root_fd() as root:
        spec = source._ignore(root)
        if relative:
            with source.parent_fd(relative, directory=True) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                directories[relative] = {"mode": stat.S_IMODE(info.st_mode)}
                with source.scanner._directory(parent, name, relative, info) as child:
                    copy(child, relative, destination, spec)
        else:
            copy(root, "", destination, spec)
        source.ensure_available()
    digest = hashlib.sha256(
        json.dumps(
            {"files": manifest, "directories": directories},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "files": manifest,
        "directories": directories,
        "sha256": digest,
        "bytes": total,
        "relative": relative,
    }


def toolchains():
    """Resolve preinstalled tools, not ambient shell commands or model credentials."""
    from code_context.execution_environment import _fallback, _trusted

    found = {}
    for name in ("node", "npm", "python3", "mvn", "java", "mysql", "psql"):
        value = shutil.which(name)
        trusted, _ = _trusted(value, name) if value else (None, None)
        trusted = trusted or _fallback(name)
        if trusted:
            found[name] = trusted
    # Maven's JAVA_HOME may be a locally installed JDK outside PATH.
    home = Path(os.environ.get("JAVA_HOME", ""))
    if home.is_absolute() and (home / "bin/java").is_file():
        found["java"] = str((home / "bin/java").resolve())
    return found


class NativeSandbox:
    def __init__(self, root, protected_paths):
        self.state = private_directory(root)
        self.protected_paths = [str(Path(p).absolute()) for p in protected_paths]
        self.tools = toolchains()
        self.payloads = {}
        self.payload_lock = threading.Lock()
        self.preparation_lock = threading.RLock()

    def _check_metadata_budget(self):
        total, count = 0, 0
        with self.state.root_fd() as parent:
            with os.scandir(parent) as entries:
                for entry in entries:
                    count += 1
                    info = entry.stat(follow_symlinks=False)
                    if (
                        count + 4 > MAX_HELPER_FILES
                        or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_mode & 0o077
                        or info.st_nlink != 1
                    ):
                        raise SourceError("EXECUTION_HELPER_METADATA_CAPACITY")
                    total += info.st_size
                    # Reserve a complete config, optional proof and atomic replacements.
                    if total + 4 * 65536 > MAX_HELPER_BYTES:
                        raise SourceError("EXECUTION_HELPER_METADATA_CAPACITY")

    def cleanup_job(self, job_id, *, verified):
        with self.preparation_lock:
            return self._cleanup_job(job_id, verified=verified)

    def _cleanup_job(self, job_id, *, verified):
        """Retire only private helper input after the native scope was confirmed stopped."""
        if verified is not True or re.fullmatch(r"job-[a-f0-9]{32}", job_id) is None:
            raise SourceError("EXECUTION_HELPER_CLEANUP_UNVERIFIED")
        self.discard_payload(job_id)
        name = job_id + ".json"
        with self.state.root_fd() as parent:
            try:
                original = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            config = read_state(self.state, name)
            proxy = Path(config.get("proxyTmp", ""))
            expected = config.get("proxyIdentity")
            if (
                proxy.parent != PROXY_PARENT
                or not proxy.name.startswith("colink-srt-")
                or not isinstance(expected, dict)
                or set(expected) != {"dev", "ino", "uid"}
                or any(type(value) is not int or value < 0 for value in expected.values())
            ):
                raise SourceError("EXECUTION_HELPER_CLEANUP_UNVERIFIED")
            try:
                info = proxy.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                actual = {"dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid}
                if actual != expected or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                    raise SourceError("EXECUTION_HELPER_PROXY_CHANGED")
                access = private_directory(proxy)
                with access.root_fd() as temporary:
                    children = os.listdir(temporary)
                    if len(children) > 64:
                        raise SourceError("EXECUTION_HELPER_PROXY_CHANGED")
                    pinned = []
                    for child in children:
                        item = os.stat(child, dir_fd=temporary, follow_symlinks=False)
                        if (
                            not (stat.S_ISREG(item.st_mode) or stat.S_ISSOCK(item.st_mode))
                            or item.st_uid != os.getuid()
                            or item.st_nlink != 1
                            or item.st_size > 65536
                        ):
                            raise SourceError("EXECUTION_HELPER_PROXY_CHANGED")
                        pinned.append((child, _version(item)))
                    for child, identity in pinned:
                        if (
                            _version(os.stat(child, dir_fd=temporary, follow_symlinks=False))
                            != identity
                        ):
                            raise SourceError("EXECUTION_HELPER_PROXY_CHANGED")
                        os.unlink(child, dir_fd=temporary)
                directory = os.open(proxy.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    named = os.stat(proxy.name, dir_fd=directory, follow_symlinks=False)
                    if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                        raise SourceError("EXECUTION_HELPER_PROXY_CHANGED")
                    os.rmdir(proxy.name, dir_fd=directory)
                finally:
                    os.close(directory)
            # Proof bodies have already been validated and copied to the project profile.
            proof = name + ".database-proof"
            try:
                proof_info = os.stat(proof, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                proof_info = None
            if proof_info is not None:
                read_state(self.state, proof)
                if _version(os.stat(proof, dir_fd=parent, follow_symlinks=False)) != _version(
                    proof_info
                ):
                    raise SourceError("EXECUTION_HELPER_METADATA_CHANGED")
                os.unlink(proof, dir_fd=parent)
            if _version(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _version(original):
                raise SourceError("EXECUTION_HELPER_METADATA_CHANGED")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)

    def available(self):
        from code_context.execution_scope import TESTED_DARWIN_MAJORS

        package = HELPER.parent / "node_modules/@anthropic-ai/sandbox-runtime/package.json"
        try:
            return (
                platform.system() == "Darwin"
                and int(os.uname().release.split(".")[0]) in TESTED_DARWIN_MAJORS
                and Path("/usr/bin/sandbox-exec").is_file()
                and "node" in self.tools
                and json.loads(package.read_text())["version"] == SRT_VERSION
            )
        except (OSError, ValueError, KeyError):
            return False

    def fingerprint(self):
        from code_context.execution_toolchain import effective_toolchain_fingerprint

        return effective_toolchain_fingerprint(self.tools)

    def toolchain_matches(self, fingerprint):
        from code_context.execution_toolchain import toolchain_unchanged

        return toolchain_unchanged(fingerprint, self.tools)

    def prepare(self, *args, **kwargs):
        with self.preparation_lock:
            return self._prepare(*args, **kwargs)

    def _prepare(
        self,
        job_id,
        disk,
        workspace,
        argv,
        *,
        domains=(),
        bind_ports=(),
        connect_ports=(),
        cache_paths=(),
        database=None,
        socket_endpoints=None,
    ):
        if not self.available():
            raise SourceError("NATIVE_SANDBOX_UNAVAILABLE: terminal remains disabled")
        self._check_metadata_budget()
        from code_context.execution_toolchain import library_read_policy

        payload = None
        if database:
            payload = json.dumps(
                {"env": database["env"], "redactions": database["redactions"]}
            ).encode()
            if len(payload) > 65536:
                raise SourceError("DATABASE_CONFIGURATION_LIMIT")
        disk.verify()
        library_policy = library_read_policy(self.tools)
        home, tmp = disk.mount / "home", disk.mount / "tmp"
        home.mkdir(mode=0o700, exist_ok=True)
        tmp.mkdir(mode=0o700, exist_ok=True)
        roots = []
        for name, path in self.tools.items():
            if name in {"node", "java", "python3"}:
                # Tool installation roots are exact canonical directories.
                parent = Path(path).parent.parent
                if name == "python3" and "Frameworks" in Path(path).parts:
                    parent = Path(path).parents[1]
                roots.append(str(parent))
            elif name in {"npm", "mvn", "mysql", "psql"}:
                roots.append(str(Path(path).parent.parent))
        jar = (
            HELPER.parent
            / "node_modules/@anthropic-ai/sandbox-runtime"
            / "vendor/java-proxy-agent/srt-proxy-agent.jar"
        )
        roots += [
            *library_policy["read_paths"],
            str(jar),
            *SYSTEM_READS,
            str(disk.mount),
            *map(str, cache_paths),
        ]
        node_adapter = HELPER.parent / "node-service.cjs"
        if socket_endpoints:
            roots.append(str(node_adapter))
        protected = [*self.protected_paths, str(self.state.root), str(disk.image)]
        # SRT uses a Unix socket. A long app-data or Unicode workspace path can
        # exceed sockaddr_un's limit. Only the trusted proxy uses this short,
        # private directory; the untrusted command's temp remains on its disk.
        proxy_tmp = Path(tempfile.mkdtemp(prefix="colink-srt-", dir=str(PROXY_PARENT)))
        private_directory(proxy_tmp)
        proxy_info = proxy_tmp.lstat()
        name = job_id + ".json"
        proxy_identity = {
            "dev": proxy_info.st_dev,
            "ino": proxy_info.st_ino,
            "uid": proxy_info.st_uid,
        }
        # Commit ownership before the larger command config; failed preparation
        # can then retire its exact temporary directory without guessing paths.
        write_state(self.state, name, {"proxyTmp": str(proxy_tmp), "proxyIdentity": proxy_identity})
        protected.append(str(proxy_tmp))
        config = {
            "argv": list(argv),
            "cwd": str(workspace),
            "domains": list(domains),
            "bindPorts": list(bind_ports),
            "connectPorts": list(connect_ports),
            "readRoots": roots,
            "readMetadataPaths": library_policy["read_metadata_paths"],
            "writeRoots": [str(disk.mount), *map(str, cache_paths)],
            "protectedPaths": protected,
            "childTmp": str(tmp),
            "proxyTmp": str(proxy_tmp),
            "proxyIdentity": proxy_identity,
            "databasePayload": bool(database),
            "socketEndpoints": socket_endpoints or {},
        }
        if database:
            config["connectPorts"].append(database["port"])
            kind = database["env"]["COLINK_DATABASE_KIND"]
            config["databaseClient"] = self.tools.get("mysql" if kind == "mysql" else "psql")
            config["databaseUser"] = database.get("user")
            config["databaseTLS"] = database.get("tls", False)
            config["databaseApprovedAccount"] = database.get("approved_account", False)
            config["databaseTargetEnforced"] = database.get("target_enforced", False)
            config["databaseAction"] = database.get("database_action")
            config["databaseInstanceIdentity"] = database.get("instance_identity")
            with self.payload_lock:
                self.payloads[job_id] = payload
        write_state(self.state, name, config)
        env = {
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    [str(Path(p).parent) for p in self.tools.values()] + ["/usr/bin", "/bin"]
                )
            ),
            "HOME": str(home),
            "TMPDIR": str(proxy_tmp),
            "TMP": str(proxy_tmp),
            "TEMP": str(proxy_tmp),
            "LANG": "en_US.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": "/dev/null",
            "NPM_CONFIG_USERCONFIG": str(home / "npmrc"),
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "CI": "true",
        }
        if socket_endpoints:
            env["COLINK_SOCKET_ENDPOINTS"] = json.dumps(socket_endpoints, separators=(",", ":"))
            env["COLINK_SERVICE_SOCKET"] = next(iter(socket_endpoints.values()))
            env["COLINK_SERVICE_PORT"] = next(iter(socket_endpoints))
            env["NODE_OPTIONS"] = "--require=" + json.dumps(str(node_adapter), ensure_ascii=False)
        if "java" in self.tools:
            env["JAVA_HOME"] = str(Path(self.tools["java"]).parent.parent)
        if cache_paths:
            cache = Path(cache_paths[0])
            env.update(
                NPM_CONFIG_CACHE=str(cache / "npm"),
                PIP_CACHE_DIR=str(cache / "pip"),
                MAVEN_OPTS="-XX:+PerfDisableSharedMem",
            )
            if config["argv"][0] == self.tools.get("mvn"):
                config["argv"].insert(1, "-Dmaven.repo.local=" + str(cache / "maven"))
            environment = cache / "venv"
            env["COLINK_ENV_DIR"] = str(environment)
            env["COLINK_CACHE_DIR"] = str(cache)
            config["argv"] = [
                argument.replace("{environment}", str(environment)) for argument in config["argv"]
            ]
            if (environment / "bin/python").is_file():
                env.update(
                    VIRTUAL_ENV=str(environment),
                    PATH=str(environment / "bin") + os.pathsep + env["PATH"],
                )
                if config["argv"][0] == self.tools.get("python3"):
                    config["argv"][0] = str(environment / "bin/python")
            write_state(self.state, name, config)
        return [self.tools["node"], str(HELPER), str(self.state.root / name)], env

    def take_payload(self, job_id):
        with self.payload_lock:
            return self.payloads.pop(job_id, None)

    def discard_payload(self, job_id):
        self.take_payload(job_id)

    def database_proof(self, job_id):
        try:
            return read_state(self.state, job_id + ".json.database-proof")
        except SourceError:
            return None

    def check(self, argv, cwd, env):
        """ProcessManager's mandatory outer boundary: only our fixed helper."""
        return (
            self.available()
            and len(argv) == 3
            and list(argv[:2]) == [self.tools["node"], str(HELPER)]
            and Path(argv[2]).parent == self.state.root
            and env.get("CI") == "true"
        )

"""Read-only installed tool inventory; no user startup/configuration or DB access.

Only fixed version arguments run, from trusted installation locations, with a
clean environment, bounded output and a three-second deadline. Python package
metadata is inspected without importing project/third-party modules. Creating a
venv is a separate execution plan in the application's bounded task/cache disk.
"""

import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from code_context.policy import SECRET_PATTERNS, validate_path
from code_context.source_access import SourceError

PROBE_SECONDS = 3
PROBE_BYTES = 8 * 1024
TOOLS = (
    "python3",
    "node",
    "npm",
    "java",
    "javac",
    "mvn",
    "mysql",
    "mysqld",
    "psql",
    "postgres",
    "pg_config",
    "redis-cli",
    "redis-server",
    "sqlite3",
)
_ARGUMENTS = {
    "node": ["--version"],
    "npm": ["--version"],
    "java": ["-version"],
    "javac": ["-version"],
    "mvn": ["--version", "--settings", "/dev/null", "--global-settings", "/dev/null"],
    "mysql": ["--no-defaults", "--version"],
    "mysqld": ["--no-defaults", "--version"],
    "psql": ["--version"],
    "postgres": ["--version"],
    "pg_config": ["--version"],
    "redis-cli": ["--version"],
    "redis-server": ["--version"],
    "sqlite3": ["--version"],
}
_VERSION = re.compile(r"\b(?:v)?\d+(?:\.\d+){1,3}(?:[-+._a-zA-Z0-9]*)?\b")
_PACKAGES = (
    "pip",
    "setuptools",
    "wheel",
    "pytest",
    "psycopg",
    "psycopg2",
    "psycopg2-binary",
    "pymysql",
    "mysql-connector-python",
    "redis",
    "pgvector",
    "numpy",
    "chromadb",
    "qdrant-client",
)
_PYTHON_PROBE = (
    "import sys,sysconfig,json,importlib.util;"
    "print(json.dumps({'version':list(sys.version_info[:3]),'executable':sys.executable,"
    "'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
    "'purelib':sysconfig.get_path('purelib'),'platlib':sysconfig.get_path('platlib'),"
    "'venv_supported':importlib.util.find_spec('venv') is not None,"
    "'ensurepip_supported':importlib.util.find_spec('ensurepip') is not None}))"
)


def _roots():
    home = Path.home()
    return (
        Path("/usr/bin"),
        Path("/usr/local"),
        Path("/opt/homebrew"),
        Path("/Library/Frameworks/Python.framework"),
        Path("/Library/Java/JavaVirtualMachines"),
        Path("/Library/Developer/CommandLineTools"),
        Path("/Applications"),
        home / ".nvm",
        home / ".pyenv",
        home / ".local",
        home / ".codex",
        Path(sys.base_prefix).resolve(),
    )


def _trusted(path, name):
    """Do not run a version-shaped project script or an Apple install launcher."""
    try:
        requested = Path(path)
        if not requested.is_absolute():
            return None, "invalid_path"
        if str(requested) in {"/usr/bin/java", "/usr/bin/javac", "/usr/bin/python3"}:
            return None, "developer_launcher_skipped"
        resolved = requested.resolve(strict=True)
        info = resolved.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o022
            or not any(resolved.is_relative_to(root) for root in _roots())
        ):
            return None, "untrusted_installation"
        if name != "npm" and not os.access(resolved, os.X_OK):
            return None, "not_executable"
        if name == "npm" and resolved.suffix != ".js" and not os.access(resolved, os.X_OK):
            return None, "not_executable"
        # Native Python/Node/Java are not shell wrappers. Module probes must not
        # execute a script bearing a convenient runtime name in a trusted folder.
        if name in {"python3", "node", "java", "javac"}:
            with resolved.open("rb") as stream:
                magic = stream.read(4)
            if magic not in {
                b"\x7fELF",
                b"\xcf\xfa\xed\xfe",
                b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xfe\xed\xfa\xce",
                b"\xca\xfe\xba\xbe",
                b"\xbe\xba\xfe\xca",
                b"\xca\xfe\xba\xbf",
                b"\xbf\xba\xfe\xca",
            }:
                return None, "runtime_wrapper_skipped"
        return str(resolved), None
    except (OSError, ValueError, TypeError):
        return None, "not_found"


def _fallback(name):
    # Application launches often have a shorter PATH than the user's shell.
    # Inspect bounded known installation locations; never source a profile.
    candidates = [
        str(Path.home() / ".local/bin" / name),
        "/opt/homebrew/bin/" + name,
        "/usr/local/bin/" + name,
        "/usr/bin/" + name,
    ]
    if name == "python3":
        candidates.extend([sys.executable, "/Library/Developer/CommandLineTools/usr/bin/python3"])
    elif name == "java":
        candidates.extend(
            [
                "/opt/homebrew/opt/openjdk/bin/java",
                "/usr/local/opt/openjdk/bin/java",
            ]
        )
        java_root = Path("/Library/Java/JavaVirtualMachines")
        if java_root.is_dir():
            with os.scandir(java_root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 32:
                        break
                    candidates.append(str(Path(entry.path) / "Contents/Home/bin/java"))
    elif name == "javac":
        java = _fallback("java")
        if java:
            candidates.append(str(Path(java).parent / "javac"))
    elif name in {"psql", "postgres", "pg_config"}:
        for prefix in ("/opt/homebrew/opt", "/usr/local/opt"):
            location = Path(prefix)
            if not location.is_dir():
                continue
            with os.scandir(location) as entries:
                for index, entry in enumerate(entries):
                    if index >= 512:
                        break
                    if re.fullmatch(r"postgresql(?:@\d+)?|libpq", entry.name):
                        candidates.append(str(location / entry.name / "bin" / name))
    for candidate in candidates:
        path, _reason = _trusted(candidate, name)
        if path:
            return path
    return None


def _clean_environment(paths):
    binaries = [str(Path(path).parent) for path in paths.values() if path]
    environment = {
        "PATH": os.pathsep.join(dict.fromkeys([*binaries, "/usr/bin", "/bin"])),
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": "/var/empty",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NPM_CONFIG_USERCONFIG": "/dev/null",
        # npm rejects loading the very same path for user + global config. This
        # root-owned empty home supplies a distinct absent global-config path.
        "NPM_CONFIG_GLOBALCONFIG": "/var/empty/.colink-inventory-global-npmrc",
        "NPM_CONFIG_CACHE": "/dev/null",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "MAVEN_SKIP_RC": "1",
        "MAVEN_USER_HOME": "/var/empty",
    }
    if paths.get("java"):
        environment["JAVA_HOME"] = str(Path(paths["java"]).parent.parent)
    return environment


def _probe(argv, environment):
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        env=environment,
        shell=False,
        close_fds=True,
        start_new_session=True,
    )
    captured = bytearray()
    state, deadline = "ok", time.monotonic() + PROBE_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    state = "timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 32 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(captured) < PROBE_BYTES:
                        captured.extend(chunk[: PROBE_BYTES - len(captured)])
            if state == "ok":
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    state = "timeout"
    finally:
        if process.poll() is None or state == "timeout":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=1)
        process.stdout.close()
        process.stderr.close()
    output = captured.decode("utf-8", errors="replace")
    if any(pattern.search(output) for pattern in SECRET_PATTERNS):
        return {"state": "unsafe_output", "exit_code": process.returncode, "output": ""}
    return {"state": state, "exit_code": process.returncode, "output": output}


def _package_metadata(runtime):
    result = {
        name: {"installed_metadata": False, "version": None, "import_verified": False}
        for name in _PACKAGES
    }
    for directory in dict.fromkeys([runtime.get("purelib"), runtime.get("platlib")]):
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            continue
        location = Path(directory)
        if not any(location.is_relative_to(root) for root in _roots()) or not location.is_dir():
            continue
        with os.scandir(location) as entries:
            for index, entry in enumerate(entries):
                if index >= 4096:
                    break
                if not entry.name.endswith(".dist-info") or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    parent = os.open(
                        location / entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
                    )
                    try:
                        fd = os.open(
                            "METADATA", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                        )
                        try:
                            info = os.fstat(fd)
                            if (
                                not stat.S_ISREG(info.st_mode)
                                or info.st_size > 1024 * 1024
                                or info.st_uid not in {0, os.geteuid()}
                                or info.st_nlink != 1
                            ):
                                continue
                            raw = os.read(fd, PROBE_BYTES).decode("utf-8", errors="replace")
                        finally:
                            os.close(fd)
                    finally:
                        os.close(parent)
                except OSError:
                    continue
                if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
                    continue
                name = re.search(r"^Name: ([A-Za-z0-9_.-]{1,80})$", raw, re.MULTILINE)
                version = re.search(r"^Version: ([A-Za-z0-9+_.-]{1,80})$", raw, re.MULTILINE)
                normalized = name.group(1).lower().replace("_", "-") if name else None
                if normalized in result and version:
                    result[normalized] = {
                        "installed_metadata": True,
                        "version": version.group(1),
                        "import_verified": False,
                    }
    return result


def inventory(tool_paths: dict | None = None):
    """Return installed versions and evidence gaps, without starting services."""
    if tool_paths is not None and (
        not isinstance(tool_paths, dict)
        or any(
            name not in TOOLS
            or not isinstance(path, str)
            or not path
            or len(path) > 4096
            or not Path(path).is_absolute()
            for name, path in tool_paths.items()
        )
    ):
        raise SourceError("INVALID_TOOLCHAIN_CONFIGURATION: use explicit installed tool paths")
    paths, tools = {}, {}
    for name in TOOLS:
        configured = tool_paths.get(name) if tool_paths is not None else None
        if configured is None and name == "javac" and paths.get("java"):
            candidate = str(Path(paths["java"]).parent / "javac")
        else:
            candidate = configured or shutil.which(name)
        path, reason = _trusted(candidate, name) if candidate else (None, "not_found")
        if not path and configured is None:
            path = _fallback(name)
            if path:
                reason = None
        paths[name] = path
        tools[name] = {
            "path": path,
            "present": candidate is not None or path is not None,
            "available": False,
            "version": None,
            "probe_state": reason or "pending",
            "runner_bound": name in tool_paths if tool_paths is not None else None,
        }
    environment = _clean_environment(paths)

    def inspect(name):
        path = paths[name]
        if not path:
            return name, None
        if name == "python3":
            argv = [path, "-I", "-B", "-S", "-c", _PYTHON_PROBE]
        elif name == "npm" and Path(path).suffix == ".js":
            if not paths["node"]:
                return name, {"state": "node_required", "exit_code": None, "output": ""}
            argv = [paths["node"], path, "--version"]
        else:
            argv = [path, *_ARGUMENTS[name]]
        try:
            return name, _probe(argv, environment)
        except (OSError, subprocess.SubprocessError):
            return name, {"state": "probe_failed", "exit_code": None, "output": ""}

    runtime = None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name, probe in pool.map(inspect, TOOLS):
            if probe is None:
                continue
            item = tools[name]
            item["probe_state"] = probe["state"]
            if probe["state"] != "ok" or probe["exit_code"] != 0:
                if probe["state"] == "ok":
                    item["probe_state"] = "probe_failed"
                continue
            if name == "python3":
                try:
                    parsed = json.loads(probe["output"])
                    if (
                        not isinstance(parsed, dict)
                        or not isinstance(parsed["version"], list)
                        or len(parsed["version"]) != 3
                        or any(type(value) is not int or value < 0 for value in parsed["version"])
                        or type(parsed["venv_supported"]) is not bool
                        or type(parsed["ensurepip_supported"]) is not bool
                        or str(Path(parsed["executable"]).resolve()) != paths[name]
                    ):
                        raise ValueError
                    runtime = parsed
                    item["version"] = ".".join(map(str, parsed["version"]))
                except (ValueError, KeyError, TypeError):
                    item["probe_state"] = "invalid_runtime_probe"
                    continue
            else:
                match = _VERSION.search(probe["output"])
                if not match:
                    item["probe_state"] = "version_unrecognized"
                    continue
                item["version"] = match.group(0).removeprefix("v")
                if name == "mvn":
                    java = re.search(
                        r"Java version:\s*(\d+(?:\.\d+){1,3}(?:[-+._a-zA-Z0-9]*)?)",
                        probe["output"],
                    )
                    item["java_runtime_version"] = java.group(1) if java else None
            item["available"] = True
    mismatches = []
    if runtime and Path(runtime["executable"]).resolve() != Path(sys.executable).resolve():
        mismatches.append(
            {
                "code": "PYTHON_DIFFERS_FROM_COLINK",
                "message": "项目 Python 与 CoLink 自身不同；安装和测试应绑定项目运行时。",
            }
        )
    if tool_paths is not None:
        for name, item in tools.items():
            if item["available"] and not item["runner_bound"]:
                mismatches.append(
                    {
                        "code": "TOOL_NOT_BOUND_TO_RUNNER",
                        "tool": name,
                        "message": "本机已安装，受控执行器尚未绑定此工具。",
                    }
                )
    if tools["npm"]["present"] and not tools["node"]["available"]:
        mismatches.append(
            {
                "code": "NPM_NODE_UNAVAILABLE",
                "message": "npm 需要可验证的 Node；先绑定实际 Node 运行时。",
            }
        )
    maven_java = tools["mvn"].get("java_runtime_version")
    if maven_java and tools["java"]["available"] and tools["java"]["version"] != maven_java:
        mismatches.append(
            {
                "code": "MAVEN_JDK_DIFFERS_FROM_JAVA",
                "tool": "mvn",
                "message": "Maven 报告的 JDK 与已选 Java 不同；先核对项目运行时绑定。",
            }
        )
    return {
        "tools": tools,
        "python": {
            "selected_runtime": runtime["executable"] if runtime else None,
            "venv_supported": runtime["venv_supported"] if runtime else False,
            "ensurepip_supported": runtime["ensurepip_supported"] if runtime else False,
            "modules": _package_metadata(runtime) if runtime else {},
            "module_evidence": "package_metadata_only; no project or third-party imports",
        },
        "runtime_mismatches": mismatches,
        "jdk": {
            "runtime_available": tools["java"]["available"],
            "compiler_available": tools["javac"]["available"],
            "compiler_path": tools["javac"]["path"],
            "compile_verified": False,
        },
        "databases": {
            "mysql_client_available": tools["mysql"]["available"],
            "mysql_server_binary_available": tools["mysqld"]["available"],
            "postgresql_client_available": tools["psql"]["available"],
            "postgresql_server_binary_available": tools["postgres"]["available"],
            "redis_client_available": tools["redis-cli"]["available"],
            "redis_server_binary_available": tools["redis-server"]["available"],
            "connection_verified": False,
            "migration_verified": False,
            "crud_verified": False,
            "instance_policy": "reuse_existing; never_create_or_start_implicitly",
        },
        "vector": {
            "recommendation": "PostgreSQL + pgvector",
            "extension_verified": False,
            "query_verified": False,
            "embedding_verified": False,
        },
        "probe_limits": {
            "seconds_per_command": PROBE_SECONDS,
            "output_bytes_per_command": PROBE_BYTES,
            "shell": False,
            "user_configuration": False,
        },
        "guidance": [
            "每次开发先检查本机实际工具，再按项目需要选择 Python/npm/Maven；缺失项不会自动安装。",
            "Python 虚拟环境建在本任务或受控项目缓存盘中，使用同一个环境完成安装、测试与服务运行。",
            "已有数据库优先复用；先绑定项目角色与连接范围，再运行 migration，最后验证真实 CRUD。",
            "向量推荐 PostgreSQL + pgvector；Python 包、客户端或开放端口不能证明扩展和检索可用。",
            "预演会执行真实隔离命令；服务停止不会撤销数据库写入。演示数据和真实业务验收分别记录。",
        ],
        "guide_path": "docs/DEVELOPMENT_ENVIRONMENT_GUIDE.md",
    }


def venv_plan(environment, path="{environment}"):
    """Build a fixed argv for the controlled executor; never create anything here."""
    try:
        if path != "{environment}":
            validate_path(path)
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", path):
                raise ValueError
        python = environment["python"]
        available = environment["tools"]["python3"]["available"] and python["venv_supported"]
    except (ValueError, KeyError, TypeError):
        raise SourceError("INVALID_VENV_PLAN: use one relative environment directory") from None
    if not available:
        raise SourceError("PYTHON_VENV_UNAVAILABLE: choose a verified installed Python")
    command = ["python3", "-I", "-B", "-S", "-m", "venv"]
    if not python["ensurepip_supported"]:
        command.append("--without-pip")
    command.append(path)
    return {
        "command": command,
        "operation": "generate",
        "network": "none",
        "path": path,
        "python_path": environment["tools"]["python3"]["path"],
        "pip_bootstrap_available": python["ensurepip_supported"],
        "requires_execution_plan": True,
        "requires_rehearsal": True,
        "storage_scope": "bounded_task_or_project_cache_only",
        "environment_variable": "COLINK_ENV_DIR",
        "message": "先计划并预演；虚拟环境仅写入任务/项目缓存盘，保留源码和全局 Python。",
    }

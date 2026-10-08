"""Static, bounded database configuration discovery inside authorized sources.

Sensitive configuration is read only by this component; it is never added to the
source-query index. No shell, imports, YAML constructors or project scripts run.
"""

import hashlib
import json
import os
import re
import stat
import time
from urllib.parse import parse_qs, unquote, urlsplit

from code_context.scanner import _version
from code_context.source_access import SourceError

CONFIG_NAMES = {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. -]{0,63}\Z")
USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}\Z")
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-]?([^}]*))?\}")


def valid_target(profile):
    return (
        profile.get("kind") in {"postgresql", "pgvector", "mysql", "qdrant"}
        and profile.get("host") in {"localhost", "127.0.0.1", "::1"}
        and type(profile.get("port")) is int
        and 1024 <= profile["port"] <= 65535
        and bool(NAME.fullmatch(profile.get("database", "")))
        and bool(USER.fullmatch(profile.get("user", "")))
        and type(profile.get("tls")) is bool
    )


def target_identity(profile):
    return {
        key: ("127.0.0.1" if key == "host" and profile[key] == "localhost" else profile[key])
        for key in ("kind", "host", "port", "database", "user", "tls")
    }


def target_id(profile):
    encoded = json.dumps(target_identity(profile), sort_keys=True, separators=(",", ":"))
    return "db-" + hashlib.sha256(encoded.encode()).hexdigest()[:32]


def _is_config(name):
    return (
        name in CONFIG_NAMES
        or name == ".env"
        or name.startswith(".env.")
        and not name.endswith((".example", ".sample"))
        or re.fullmatch(r"application(?:-[A-Za-z0-9_-]+)?\.(?:yml|yaml|properties)", name)
        or name == "schema.prisma"
    )


def configuration_files(source):
    """Read <=128 known config files with pinned nofollow FDs and identity checks."""
    source.ensure_available()
    result = {}
    total, directories = 0, 0
    deadline = time.monotonic() + 2
    with source.root_fd() as root:
        spec = source._ignore(root)

        def walk(fd, prefix, depth):
            nonlocal total, directories
            directories += 1
            if directories > 512 or depth > 8 or time.monotonic() > deadline:
                raise SourceError("DATABASE_DISCOVERY_BUDGET: select a smaller project")
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > 4096:
                        raise SourceError("DATABASE_DISCOVERY_BUDGET")
            for name in sorted(names):
                path = f"{prefix}/{name}" if prefix else name
                info = source.scanner._stat(fd, name, path)
                if info is None:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if source.scanner._path_problem(path, spec, True):
                        continue
                    with source.scanner._directory(fd, name, path, info) as child:
                        walk(child, path, depth + 1)
                elif stat.S_ISREG(info.st_mode) and _is_config(name):
                    # Only the explicit config allowlist may bypass source secret exclusion.
                    if any(path == p or path.startswith(p + "/") for p in source.scanner._excluded):
                        continue
                    if info.st_size > 65536 or len(result) >= 128 or total + info.st_size > 1048576:
                        raise SourceError("DATABASE_DISCOVERY_BUDGET")
                    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        before = os.fstat(opened)
                        if _version(info) != _version(before) or before.st_nlink != 1:
                            raise SourceError("DATABASE_CONFIG_UNSAFE")
                        raw = os.read(opened, 65537)
                        after = source.scanner._stat(fd, name, path)
                        if (
                            len(raw) > 65536
                            or after is None
                            or _version(before) != _version(os.fstat(opened))
                            or _version(before) != _version(after)
                        ):
                            raise SourceError("DATABASE_CONFIG_CHANGED: retry")
                        result[path] = raw.decode("utf-8")
                        total += len(raw)
                    except (OSError, UnicodeError):
                        raise SourceError("DATABASE_CONFIG_UNREADABLE") from None
                    finally:
                        os.close(opened)

        walk(root, "", 0)
    return result


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value.split(" #", 1)[0].strip()


def _properties(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*[=:]\s*(.*)", line)
        if match:
            result[match[1]] = _scalar(match[2])
    return result


def _yaml(text):
    """Conservative scalar mapping reader; anchors, tags and multiline values are unresolved."""
    stack, result = [], {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.strip() == "---":
            stack = []
            continue
        match = re.match(r"^( *)([A-Za-z_][A-Za-z0-9_.-]*):(?:\s+(.*))?$", line)
        if not match:
            continue
        indent, key, value = len(match[1]), match[2], match[3]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        full = ".".join([x[1] for x in stack] + [key])
        if value is None or not value.strip():
            stack.append((indent, key))
        elif value.strip().startswith(("!", "&", "*", "|", ">", "{", "[")):
            result[full] = None
        elif full in result:
            result[full] = None
        else:
            result[full] = _scalar(value)
    return result


def _expand(value, env):
    if not isinstance(value, str):
        return None
    for _ in range(4):
        missing = False

        def replace(match):
            nonlocal missing
            key, default = match.groups()
            actual = env.get(key, default)
            if actual is None:
                missing = True
                return ""
            return actual

        value = VARIABLE.sub(replace, value)
        if missing:
            return None
        if not VARIABLE.search(value):
            return value
    return None


def _url(value, env, user=None, password=None):
    value = _expand(value, env)
    if value is None:
        return None
    if value.startswith("jdbc:"):
        value = value[5:]
    try:
        parsed = urlsplit(value)
        kind = {"postgres": "postgresql", "postgresql": "postgresql", "mysql": "mysql"}.get(
            parsed.scheme
        )
        if not kind or not parsed.hostname or not parsed.path.startswith("/"):
            return None
        query = parse_qs(parsed.query)
        ssl = query.get("sslmode", [""])[0]
        if ssl not in {"", "disable", "verify-full"}:
            return None
        profile = {
            "kind": kind,
            "host": parsed.hostname,
            "port": parsed.port or (5432 if kind == "postgresql" else 3306),
            "database": unquote(parsed.path[1:]),
            "user": _expand(user, env) if user is not None else unquote(parsed.username or ""),
            "password": _expand(password, env)
            if password is not None
            else unquote(parsed.password or ""),
            "tls": ssl == "verify-full" or query.get("useSSL", ["false"])[0].lower() == "true",
        }
        return profile if valid_target(profile) and profile["password"] is not None else None
    except (ValueError, TypeError):
        return None


def discover(source, *, connection_environment=None):
    files = configuration_files(source)
    envs = {
        path: _properties(text)
        for path, text in files.items()
        if path.rsplit("/", 1)[-1].startswith(".env")
    }
    candidates, unresolved = [], []
    config_digest = hashlib.sha256()
    for path, text in sorted(files.items()):
        config_digest.update(path.encode() + b"\x00" + text.encode() + b"\x00")
        directory = path.rpartition("/")[0]
        env = {
            **(connection_environment or {}),
            **envs.get(".env", {}),
            **envs.get(f"{directory}/.env" if directory else ".env", {}),
        }
        found = []
        name = path.rsplit("/", 1)[-1]
        if name.startswith(".env"):
            values = {**env, **envs[path]}
            for key in ("DATABASE_URL", "SQLALCHEMY_DATABASE_URL", "SPRING_DATASOURCE_URL"):
                if key in values:
                    found.append(
                        _url(
                            values[key],
                            values,
                            values.get("SPRING_DATASOURCE_USERNAME"),
                            values.get("SPRING_DATASOURCE_PASSWORD"),
                        )
                    )
            if "DB_NAME" in values or "DB_DATABASE" in values:
                kind = values.get(
                    "DB_CONNECTION", values.get("DB_TYPE", values.get("DB_ENGINE", ""))
                )
                if not kind:
                    kind = {"5432": "postgresql", "3306": "mysql"}.get(values.get("DB_PORT"), "")
                kind = "postgresql" if kind in {"postgres", "pgsql"} else kind
                try:
                    p = {
                        "kind": kind,
                        "host": values.get("DB_HOST", "localhost"),
                        "port": int(
                            values.get("DB_PORT", "5432" if kind == "postgresql" else "3306")
                        ),
                        "database": _expand(
                            values.get("DB_NAME", values.get("DB_DATABASE", "")), values
                        )
                        or "",
                        "user": _expand(
                            values.get("DB_USER", values.get("DB_USERNAME", "")), values
                        )
                        or "",
                        "password": _expand(values.get("DB_PASSWORD", ""), values),
                        "tls": False,
                    }
                    found.append(p if valid_target(p) and p["password"] is not None else None)
                except ValueError:
                    found.append(None)
        elif name.startswith("application"):
            documents = [text] if name.endswith(".properties") else re.split(r"(?m)^---\s*$", text)
            for document in documents:
                values = _properties(document) if name.endswith(".properties") else _yaml(document)
                if any(
                    value is None
                    for key, value in values.items()
                    if key.startswith("spring.datasource.")
                ):
                    found.append(None)
                elif "spring.datasource.url" in values:
                    found.append(
                        _url(
                            values["spring.datasource.url"],
                            env,
                            values.get("spring.datasource.username"),
                            values.get("spring.datasource.password"),
                        )
                    )
                elif "datasource" in document:
                    found.append(None)
        elif name in CONFIG_NAMES:
            values = _yaml(text)
            services = {
                key.split(".")[1]
                for key in values
                if key.startswith("services.") and key.endswith(".image")
            }
            for service in sorted(services):
                prefix = "services." + service
                image = values[prefix + ".image"]
                if not isinstance(image, str):
                    found.append(None)
                    continue
                kind = (
                    "postgresql"
                    if image.startswith(("postgres:", "pgvector/"))
                    else "mysql"
                    if image.startswith(("mysql:", "mariadb:"))
                    else None
                )
                if not kind:
                    continue
                e = {
                    key.removeprefix(prefix + ".environment."): _expand(value, env)
                    for key, value in values.items()
                    if key.startswith(prefix + ".environment.")
                }
                # Compose supports both a scalar mapping and KEY=value list environment.
                block = re.search(
                    r"(?ms)^  " + re.escape(service) + r":\s*\n(.*?)(?=^  [A-Za-z_]|\Z)", text
                )
                service_text = block[1] if block else ""
                for match in re.finditer(r"(?m)^\s+-\s+([A-Z][A-Z0-9_]*)=(.*)$", service_text):
                    e[match[1]] = _expand(_scalar(match[2]), env)
                ports = re.findall(
                    r"(?m)^\s+-\s+[\"']?((?:127\.0\.0\.1:)?[0-9]+:[0-9]+)[\"']?\s*$", service_text
                )
                expected = 5432 if kind == "postgresql" else 3306
                port = next(
                    (int(p.split(":")[-2]) for p in ports if int(p.split(":")[-1]) == expected),
                    None,
                )
                db = e.get("POSTGRES_DB" if kind == "postgresql" else "MYSQL_DATABASE")
                user = (
                    e.get("POSTGRES_USER", "postgres")
                    if kind == "postgresql"
                    else e.get("MYSQL_USER", "root")
                )
                password = (
                    e.get("POSTGRES_PASSWORD")
                    if kind == "postgresql"
                    else e.get("MYSQL_PASSWORD", e.get("MYSQL_ROOT_PASSWORD"))
                )
                p = {
                    "kind": kind,
                    "host": "127.0.0.1",
                    "port": port,
                    "database": db or "",
                    "user": user or "",
                    "password": password,
                    "tls": False,
                }
                found.append(p if valid_target(p) and password is not None else None)
        elif name == "schema.prisma":
            for match in re.finditer(r"url\s*=\s*env\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"\s*\)", text):
                key = match[1]
                prisma_env = {
                    **env,
                    **envs.get(
                        (path.rpartition("/")[0].rpartition("/")[0] + "/.env").lstrip("/"), {}
                    ),
                }
                value = prisma_env.get(key)
                found.append(_url(value, prisma_env) if value else None)
        for candidate in found:
            if candidate is None:
                unresolved.append(path)
            else:
                candidate["origin"] = path
                candidate["database_target_id"] = target_id(candidate)
                candidates.append(candidate)
    # Include environment-resolved values in the binding; never persist or expose this data.
    config_digest.update(json.dumps(candidates, sort_keys=True).encode())
    return candidates, sorted(set(unresolved)), config_digest.hexdigest()

"""Bounded local Redis configuration discovery and native connection checks.

Only AUTH, SELECT and PING are sent. This module grants no data-command access.
"""

import hashlib
import json
import socket
import ssl
import time
from urllib.parse import unquote, urlsplit

from code_context.database_discovery import (
    USER,
    _expand,
    _properties,
    _yaml,
    configuration_files,
)
from code_context.source_access import SourceError


def identity(value):
    return {key: value[key] for key in ("kind", "host", "port", "user", "tls", "database")}


def valid(value):
    return (
        value.get("kind") == "redis"
        and value.get("host") in {"127.0.0.1", "localhost", "::1"}
        and type(value.get("port")) is int
        and 1024 <= value["port"] <= 65535
        and type(value.get("database")) is int
        and 0 <= value["database"] <= 65535
        and isinstance(value.get("user"), str)
        and bool(USER.fullmatch(value["user"]))
        and type(value.get("tls")) is bool
    )


def public(value):
    return {
        **identity(value),
        "config_id": value["config_id"],
        "origin": value["origin"],
        "credential_conflict": value.get("credential_conflict", False),
    }


def discover(source):
    files = configuration_files(source)
    envs = {
        path: _properties(text)
        for path, text in files.items()
        if path.rsplit("/", 1)[-1].startswith(".env")
    }
    result, unresolved = {}, []
    for path, text in sorted(files.items()):
        name = path.rsplit("/", 1)[-1]
        directory = path.rpartition("/")[0]
        env = {**envs.get(".env", {}), **envs.get(f"{directory}/.env" if directory else ".env", {})}
        values = _yaml(text) if name.endswith((".yml", ".yaml")) else _properties(text)
        if name.startswith(".env"):
            env.update(values)
            prefix = next(
                (
                    p
                    for p in ("REDIS_", "SPRING_DATA_REDIS_", "SPRING_REDIS_")
                    if any(k.startswith(p) for k in values)
                ),
                "REDIS_",
            )
            field = lambda key, values=values, prefix=prefix: values.get(prefix + key.upper())  # noqa: E731
        elif name.startswith("application"):
            prefix = next(
                (
                    p
                    for p in ("spring.data.redis.", "spring.redis.")
                    if any(k.startswith(p) for k in values)
                ),
                None,
            )
            if prefix is None:
                continue
            field = lambda key, values=values, prefix=prefix: values.get(prefix + key.lower())  # noqa: E731
        else:
            continue
        if any(value is None for key, value in values.items() if key.startswith(prefix)):
            unresolved.append({"origin": path, "reason": "redis_config_unresolved"})
            continue
        if not any(
            field(key) is not None for key in ("url", "host", "port", "password", "database", "db")
        ):
            continue
        try:
            raw_url = field("url")
            if raw_url is not None:
                url = _expand(raw_url, env)
                if url is None:
                    raise ValueError
                parsed = urlsplit(url)
                if parsed.scheme not in {"redis", "rediss"} or parsed.query or parsed.fragment:
                    raise ValueError
                value = {
                    "kind": "redis",
                    "host": parsed.hostname,
                    "port": parsed.port or 6379,
                    "user": unquote(parsed.username or "default"),
                    "password": unquote(parsed.password or ""),
                    "database": int(parsed.path.lstrip("/") or "0"),
                    "tls": parsed.scheme == "rediss",
                }
            else:
                tls = _expand(field("ssl.enabled") or field("ssl") or "false", env)
                if tls is None or tls.lower() not in {"true", "false"}:
                    raise ValueError
                value = {
                    "kind": "redis",
                    "host": _expand(field("host") or "127.0.0.1", env),
                    "port": int(_expand(field("port") or "6379", env)),
                    "database": int(_expand(field("database") or field("db") or "0", env)),
                    "user": _expand(field("username") or field("user") or "default", env),
                    "password": _expand(field("password") or "", env),
                    "tls": tls.lower() == "true",
                }
            if not valid(value) or not isinstance(value["password"], str):
                raise ValueError
            if value["host"] == "localhost":
                value["host"] = "127.0.0.1"
            identifier = (
                "redis-"
                + hashlib.sha256(json.dumps(identity(value), sort_keys=True).encode()).hexdigest()[
                    :32
                ]
            )
            if identifier in result and result[identifier]["password"] != value["password"]:
                result[identifier]["credential_conflict"] = True
            elif identifier not in result:
                result[identifier] = {**value, "config_id": identifier, "origin": path}
        except (ValueError, TypeError):
            unresolved.append({"origin": path, "reason": "redis_config_unresolved"})
    return list(result.values()), unresolved


def probe(value, password):
    if (
        not valid(value)
        or not isinstance(password, str)
        or len(password) > 4096
        or "\x00" in password
    ):
        raise SourceError("INVALID_REDIS_CONNECTION")
    commands = []
    if password or value["user"] != "default":
        commands.append(
            (
                ["AUTH", password]
                if value["user"] == "default"
                else ["AUTH", value["user"], password],
                b"+OK\r\n",
            )
        )
    if value["database"]:
        commands.append((["SELECT", str(value["database"])], b"+OK\r\n"))
    commands.append((["PING"], b"+PONG\r\n"))
    deadline = time.monotonic() + 6
    try:
        with socket.create_connection((value["host"], value["port"]), timeout=2) as raw:
            connection = (
                ssl.create_default_context().wrap_socket(raw, server_hostname=value["host"])
                if value["tls"]
                else raw
            )
            try:
                for command, expected in commands:
                    chunks = [f"*{len(command)}\r\n".encode()]
                    for part in command:
                        encoded = part.encode("utf-8")
                        chunks.extend([f"${len(encoded)}\r\n".encode(), encoded, b"\r\n"])
                    connection.sendall(b"".join(chunks))
                    reply = bytearray()
                    while not reply.endswith(b"\r\n") and len(reply) <= 4096:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        connection.settimeout(min(2, remaining))
                        part = connection.recv(1)
                        if not part:
                            break
                        reply.extend(part)
                    if reply.startswith((b"-NOAUTH", b"-WRONGPASS")):
                        raise SourceError("DATABASE_AUTHENTICATION_FAILED")
                    if reply.startswith(b"-NOPERM"):
                        raise SourceError("DATABASE_PERMISSION_DENIED")
                    if reply != expected:
                        raise SourceError("REDIS_CONNECTION_CHECK_FAILED")
            finally:
                if connection is not raw:
                    connection.close()
    except ssl.SSLError:
        raise SourceError("DATABASE_TLS_FAILED") from None
    except TimeoutError:
        raise SourceError("DATABASE_QUERY_TIMEOUT") from None
    except OSError:
        raise SourceError("DATABASE_SERVICE_UNAVAILABLE") from None

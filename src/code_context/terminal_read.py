"""Fixed read-only database commands, without a connection UI or saved DB grants.

No caller SQL, shell, scripts, arbitrary Redis commands or client flags are accepted.
Connection secrets are resolved locally from project configuration on every request.
"""

import json
import os
import re
import selectors
import sqlite3
import subprocess
import time
from pathlib import Path

from code_context.database_discovery import discover, target_identity
from code_context.redis_connection import discover as discover_redis
from code_context.redis_connection import public as redis_public
from code_context.source_access import SourceError


class TerminalReader:
    def __init__(self, source_for):
        self.source_for = source_for

    def _targets(self, project_id):
        source = self.source_for(project_id)
        source.ensure_available()
        sql, unresolved, fingerprint = discover(source, connection_environment=os.environ)
        grouped = {}
        for value in sql:
            key = value["database_target_id"]
            if key in grouped and grouped[key]["password"] != value["password"]:
                grouped[key]["credential_conflict"] = True
            else:
                grouped.setdefault(key, value)
        redis, redis_unresolved = discover_redis(source)
        return (
            source,
            grouped,
            {v["config_id"]: v for v in redis},
            unresolved,
            redis_unresolved,
            fingerprint,
        )

    def targets(self, project_id):
        _, sql, redis, unresolved, redis_unresolved, _ = self._targets(project_id)
        return {
            "targets": [
                {
                    **target_identity(v),
                    "target": k,
                    "origin": v["origin"],
                    "credential_conflict": v.get("credential_conflict", False),
                }
                for k, v in sql.items()
            ]
            + [{**redis_public(v), "target": k} for k, v in redis.items()],
            "unresolved": unresolved + redis_unresolved,
            "read_only": True,
            "commands": {
                "mysql/psql": ["list_databases", "list_tables", "describe", "preview"],
                "redis-cli": ["scan", "get", "type", "ttl", "hgetall", "lrange", "scard", "zrange"],
                "sqlite3": ["list_tables", "describe", "preview"],
            },
            "sqlite": "For sqlite3, target is a project-relative existing database file",
        }

    def read(
        self, project_id, client, action, target, *, table="", schema="public", key="", limit=100
    ):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise SourceError("INVALID_DATABASE_READ_LIMIT")
        if client == "sqlite3":
            return self._sqlite(project_id, target, action, table, limit)
        _, sql, redis, _, _, fingerprint = self._targets(project_id)
        if client == "redis-cli":
            profile = redis.get(target)
            if not profile or profile.get("credential_conflict"):
                raise SourceError("DATABASE_CONFIG_UNAVAILABLE")
            result = self._redis(profile, action, key, limit)
        elif client in {"mysql", "psql"}:
            profile = sql.get(target)
            if (
                not profile
                or profile.get("credential_conflict")
                or (client == "mysql") != (profile["kind"] == "mysql")
            ):
                raise SourceError("DATABASE_CONFIG_UNAVAILABLE")
            result = self._sql(profile, action, table, schema, limit)
        else:
            raise SourceError("READ_ONLY_COMMAND_UNSUPPORTED: use listed read commands")
        _, current_sql, current_redis, _, _, current_fingerprint = self._targets(project_id)
        if fingerprint != current_fingerprint or current_redis != redis or current_sql != sql:
            raise SourceError("DATABASE_CONFIG_CHANGED: discard result and refresh")
        return {"read_only": True, "client": client, "action": action, "data": result}

    @staticmethod
    def _validate_table(action, table, schema):
        if (
            action not in {"list_databases", "list_tables", "describe", "preview"}
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,63}", schema)
            or (
                action in {"describe", "preview"}
                and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,63}", table)
            )
        ):
            raise SourceError("READ_ONLY_COMMAND_UNSUPPORTED")

    def _sql(self, profile, action, table, schema, limit):
        self._validate_table(action, table, schema)
        configuration = {
            "env": {
                "COLINK_DATABASE_KIND": profile["kind"],
                "COLINK_DATABASE_NAME": profile["database"],
            },
            "port": profile["port"],
            "user": profile["user"],
            "tls": profile["tls"],
        }
        if profile["kind"] in {"postgresql", "pgvector"}:
            configuration["env"].update(
                PGHOST=profile["host"],
                PGPORT=str(profile["port"]),
                PGUSER=profile["user"],
                PGPASSWORD=profile["password"],
                PGDATABASE=profile["database"],
                PGSSLMODE="verify-full" if profile["tls"] else "disable",
            )
        else:
            configuration["env"].update(MYSQL_HOST=profile["host"], MYSQL_PWD=profile["password"])
        if profile["kind"] in {"postgresql", "pgvector"}:
            if action == "list_databases":
                query = (
                    "SELECT datname FROM pg_catalog.pg_database "
                    "WHERE datallowconn ORDER BY datname LIMIT 100"
                )
            elif action == "list_tables":
                query = (
                    "SELECT table_schema,table_name FROM information_schema.tables "
                    "WHERE table_type='BASE TABLE' AND table_schema "
                    "NOT IN ('pg_catalog','information_schema') ORDER BY 1,2 LIMIT 100"
                )
            elif action == "describe":
                query = (
                    "SELECT column_name,data_type,is_nullable,column_default "
                    "FROM information_schema.columns "
                    f"WHERE table_schema='{schema}' AND table_name='{table}' "
                    "ORDER BY ordinal_position LIMIT 100"
                )
            else:
                # Preflight forbids views, whose function bodies can perform external work.
                allowed = (
                    "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                    f"ON c.relnamespace=n.oid WHERE n.nspname='{schema}' AND c.relname='{table}' "
                    "AND c.relkind IN ('r','p')"
                )
                if self._query(configuration, allowed).strip() != "1":
                    raise SourceError("DATABASE_BASE_TABLE_REQUIRED")
                query = f'SELECT * FROM "{schema}"."{table}" LIMIT {limit}'
            sql = (
                "BEGIN READ ONLY; SET LOCAL statement_timeout=5000; "
                "SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json)::text FROM ("
                + query
                + ") t; ROLLBACK;"
            )
            # BEGIN/SET/ROLLBACK command tags are omitted with --quiet.
            sql = "SET client_min_messages TO ERROR; " + sql
            text = self._query(configuration, sql)
            lines = [line for line in text.splitlines() if line.startswith("[")]
            if len(lines) != 1:
                raise SourceError("DATABASE_RESPONSE_INVALID")
            try:
                data = json.loads(lines[0])
            except ValueError:
                raise SourceError("DATABASE_RESPONSE_INVALID") from None
        elif profile["kind"] == "mysql":
            if action == "list_databases":
                query = (
                    "SELECT schema_name FROM information_schema.schemata "
                    "ORDER BY schema_name LIMIT 100"
                )
            elif action == "list_tables":
                query = (
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() "
                    "AND table_type='BASE TABLE' ORDER BY table_name LIMIT 100"
                )
            elif action == "describe":
                query = (
                    "SELECT column_name,data_type,is_nullable,column_default "
                    "FROM information_schema.columns "
                    f"WHERE table_schema=DATABASE() AND table_name='{table}' "
                    "ORDER BY ordinal_position LIMIT 100"
                )
            else:
                allowed = (
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
                    f"AND table_name='{table}' AND table_type='BASE TABLE'"
                )
                if self._query(configuration, allowed).strip() != "1":
                    raise SourceError("DATABASE_BASE_TABLE_REQUIRED")
                query = f"SELECT * FROM `{table}` LIMIT {limit}"
            text = self._query(
                configuration, "START TRANSACTION READ ONLY; " + query + "; ROLLBACK;"
            )
            # Keep the bounded native TSV response, which handles existing MySQL clients.
            data = {"format": "tsv", "text": text}
        else:
            raise SourceError("DATABASE_READ_UNSUPPORTED")
        return data

    @staticmethod
    def _bounded_client(argv, env, *, timeout=10, max_bytes=65536, input_text=None):
        """Never return client stderr; fail before output exceeds the fixed response budget."""
        try:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "HOME": "/dev/null", "LC_ALL": "C", **env},
                cwd="/",
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise SourceError("DATABASE_CLIENT_UNAVAILABLE") from None
        if input_text is not None:
            try:
                child.stdin.write(input_text.encode())
                child.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        output, errors, size, deadline = bytearray(), bytearray(), 0, time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selected:
                selected.register(child.stdout, selectors.EVENT_READ, True)
                selected.register(child.stderr, selectors.EVENT_READ, False)
                while selected.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SourceError("DATABASE_QUERY_TIMEOUT")
                    for key, _ in selected.select(min(remaining, 0.1)):
                        part = os.read(key.fd, 4096)
                        if not part:
                            selected.unregister(key.fileobj)
                            continue
                        size += len(part)
                        if size > max_bytes:
                            raise SourceError("DATABASE_RESPONSE_LIMIT: choose a smaller preview")
                        if key.data:
                            output.extend(part)
                        else:
                            errors.extend(part)
            if child.wait(timeout=max(0.01, deadline - time.monotonic())):
                error = errors.decode("utf-8", errors="replace").lower()
                code = "DATABASE_CONNECTION_FAILED"
                if re.search(r"\bdatabase\b[^\n]*\bdoes not exist\b|\bunknown database\b", error):
                    code = "DATABASE_NOT_FOUND"
                elif "authentication failed" in error or re.search(r"\berror 1045\b", error):
                    code = "DATABASE_AUTHENTICATION_FAILED"
                elif (
                    "permission denied" in error
                    or "not allowed" in error
                    or "access denied" in error
                ):
                    code = "DATABASE_PERMISSION_DENIED"
                elif "ssl" in error or "certificate" in error or "tls" in error:
                    code = "DATABASE_TLS_FAILED"
                elif "connection refused" in error or "could not connect" in error:
                    code = "DATABASE_SERVICE_UNAVAILABLE"
                raise SourceError(code)
            return output.decode("utf-8")
        except (UnicodeError, subprocess.TimeoutExpired):
            raise SourceError("DATABASE_RESPONSE_INVALID") from None
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()
            child.stderr.close()

    def _query(self, configuration, sql, *, use_stdin=True):
        from code_context.execution_environment import _fallback

        kind = configuration["env"]["COLINK_DATABASE_KIND"]
        if kind in {"postgresql", "pgvector"}:
            client = _fallback("psql")
            argv = [
                client,
                "--no-psqlrc",
                "--no-password",
                "--tuples-only",
                "--no-align",
                "--set",
                "ON_ERROR_STOP=1",
                "--command",
                sql,
            ]
            env = {
                key: value for key, value in configuration["env"].items() if key.startswith("PG")
            }
            env.update(PGCONNECT_TIMEOUT="3", PGOPTIONS="-c statement_timeout=5000")
        elif kind == "mysql":
            client = _fallback("mysql")
            env = {"MYSQL_PWD": configuration["env"]["MYSQL_PWD"]}
            argv = [
                client,
                "--no-defaults",
                "--no-login-paths",
                "--protocol=TCP",
                "--host=" + configuration["env"]["MYSQL_HOST"],
                "--port=" + str(configuration["port"]),
                "--user=" + configuration["user"],
                "--database=" + configuration["env"]["COLINK_DATABASE_NAME"],
                "--ssl-mode=" + ("VERIFY_IDENTITY" if configuration["tls"] else "DISABLED"),
                "--connect-timeout=3",
                "--local-infile=0",
                "--get-server-public-key",
                "--binary-mode",
                "--batch",
                "--raw",
                "--skip-column-names",
                "--execute=" + sql,
            ]
        else:
            raise SourceError("DATABASE_READ_UNSUPPORTED")
        if not client:
            raise SourceError("DATABASE_CLIENT_UNAVAILABLE")
        if use_stdin:
            if kind in {"postgresql", "pgvector"}:
                argv[-2:] = ["--file", "-"]
            else:
                argv.pop()
        return self._bounded_client(argv, env, input_text=sql if use_stdin else None)

    def _redis(self, profile, action, key, limit):
        from code_context.execution_environment import _fallback

        if not isinstance(key, str) or len(key.encode()) > 4096 or "\0" in key:
            raise SourceError("INVALID_REDIS_KEY")
        commands = {
            "scan": ["SCAN", "0", "COUNT", str(limit)],
            "get": ["GET", key],
            "type": ["TYPE", key],
            "ttl": ["TTL", key],
            "hgetall": ["HGETALL", key],
            "lrange": ["LRANGE", key, "0", str(limit - 1)],
            "scard": ["SCARD", key],
            "zrange": ["ZRANGE", key, "0", str(limit - 1)],
        }
        if action not in commands:
            raise SourceError("READ_ONLY_COMMAND_UNSUPPORTED")
        client = _fallback("redis-cli")
        if not client:
            raise SourceError("DATABASE_CLIENT_UNAVAILABLE")
        argv = [
            client,
            "--raw",
            "-e",
            "-h",
            profile["host"],
            "-p",
            str(profile["port"]),
            "-n",
            str(profile["database"]),
        ]
        if profile["user"]:
            argv += ["--user", profile["user"]]
        if profile["tls"]:
            argv += ["--tls"]
        argv += commands[action]
        return {
            "format": "text",
            "text": self._bounded_client(
                argv, {"REDISCLI_AUTH": profile["password"]} if profile["password"] else {}
            ),
        }

    def _sqlite(self, project_id, path, action, table, limit):
        self._validate_table(action, table, "public")
        source = self.source_for(project_id)
        source.ensure_available()
        if not isinstance(path, str) or not path or Path(path).is_absolute():
            raise SourceError("INVALID_SQLITE_PATH")
        database = (source.root / path).resolve(strict=True)
        if not database.is_relative_to(source.root.resolve()) or not database.is_file():
            raise SourceError("SQLITE_OUTSIDE_PROJECT")
        if action == "list_databases":
            return {"read_only": True, "data": [{"file": path}]}
        started = time.monotonic()
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
            connection.enable_load_extension(False)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}
            connection.set_authorizer(
                lambda op, arg1, arg2, db, cause: (
                    sqlite3.SQLITE_OK
                    if op in allowed or (op == sqlite3.SQLITE_PRAGMA and arg1 == "table_info")
                    else sqlite3.SQLITE_DENY
                )
            )
            connection.set_progress_handler(lambda: int(time.monotonic() - started > 5), 1000)
            if action == "list_tables":
                query = "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name LIMIT 100"
            else:
                found = connection.execute(
                    "SELECT type,sql FROM sqlite_schema WHERE name=?", (table,)
                ).fetchone()
                if (
                    not found
                    or found[0] != "table"
                    or not found[1]
                    or "VIRTUAL" in found[1].upper()
                ):
                    raise SourceError("DATABASE_BASE_TABLE_REQUIRED")
                query = (
                    f'PRAGMA table_info("{table}")'
                    if action == "describe"
                    else f'SELECT * FROM "{table}" LIMIT {limit}'
                )
            cursor = connection.execute(query)
            rows, size = [], 0
            for row in cursor:
                item = [v.hex() if isinstance(v, bytes) else v for v in row]
                size += len(json.dumps(item, ensure_ascii=False).encode())
                if size > 60000:
                    raise SourceError("DATABASE_RESPONSE_LIMIT")
                rows.append(item)
            return {
                "read_only": True,
                "data": {"columns": [v[0] for v in cursor.description], "rows": rows},
            }
        except sqlite3.Error:
            raise SourceError("SQLITE_READ_FAILED") from None
        finally:
            connection.close()

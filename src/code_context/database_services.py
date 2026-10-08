"""Native-only database service connections and journaled exact-target provisioning.

Administrator secrets remain in Keychain and fixed native client invocations.
Only a generated database-limited account is returned to the execution layer.
"""

import hashlib
import hmac
import json
import re
import secrets
import socket
import threading
import time

from code_context.database_discovery import NAME, USER
from code_context.local_control import private_directory, read_state, write_state
from code_context.source_access import SourceError


class DatabaseServices:
    def __init__(self, root, *, secret_reader, secret_writer, query, authorization_key):
        self.state = private_directory(root)
        self.reader, self.writer, self.query = secret_reader, secret_writer, query
        self.key = bytes.fromhex(authorization_key)
        self.lock = threading.RLock()
        self.services = self._load("services.json", "services")
        self.provisions = self._load("provisions.json", "provisions")

    def _load(self, filename, field):
        try:
            result = read_state(self.state, filename)[field]
            if not isinstance(result, dict):
                raise SourceError("DATABASE_SERVICE_STATE_INVALID")
            return result
        except SourceError:
            if (self.state.root / filename).exists():
                raise
            return {}

    @staticmethod
    def fingerprint(service):
        return hashlib.sha256(
            json.dumps(
                {k: v for k, v in service.items() if k != "verified_at"}, sort_keys=True
            ).encode()
        ).hexdigest()

    def _authentication_digest(self, service, password):
        identity = self._identity(service)
        raw = json.dumps({"identity": identity, "password": password}, sort_keys=True).encode()
        return hmac.new(self.key, raw, hashlib.sha256).hexdigest()

    @staticmethod
    def _identity(service):
        result = {key: service[key] for key in ("kind", "host", "port", "user", "tls")}
        if service["kind"] == "redis":
            result["database"] = service["database"]
        return result

    def _public(self, service_id, service):
        try:
            password = self.reader(service["credential_ref"])
            available = isinstance(password, str) and hmac.compare_digest(
                service.get("authentication_digest", ""),
                self._authentication_digest(service, password),
            )
        except SourceError:
            available = False
        verified = available and 0 <= time.time() - service.get("verified_at", 0) < 60
        return {
            "service_id": service_id,
            **DatabaseServices._identity(service),
            "authenticated_connection": "verified" if verified else "not_verified",
            "authenticated_at": service.get("verified_at"),
            "authentication_expires_at": service.get("verified_at", 0) + 60,
            "credential_available": available,
            "capabilities": ["connection_check"]
            if service["kind"] == "redis"
            else ["prepare_database", "approve_database"],
        }

    @staticmethod
    def _valid(service):
        return (
            service.get("kind") in {"mysql", "postgresql", "pgvector"}
            and service.get("host") in {"127.0.0.1", "localhost", "::1"}
            and type(service.get("port")) is int
            and 1024 <= service["port"] <= 65535
            and bool(USER.fullmatch(service.get("user", "")))
            and type(service.get("tls")) is bool
            and isinstance(service.get("credential_ref"), str)
            and bool(re.fullmatch(r"colink-db-[a-f0-9]{32}", service["credential_ref"]))
        )

    def configuration(self, service, *, database=None):
        if service["kind"] not in {"mysql", "postgresql", "pgvector"}:
            raise SourceError("DATABASE_EXECUTION_UNSUPPORTED")
        password = self.reader(service["credential_ref"])
        if not isinstance(password, str) or len(password) > 4096 or "\x00" in password:
            raise SourceError("INVALID_DATABASE_CREDENTIAL")
        kind, host = service["kind"], service["host"]
        if host == "localhost":
            host = "127.0.0.1"
        name = database or ("postgres" if kind != "mysql" else "information_schema")
        env = {"COLINK_DATABASE_KIND": kind, "COLINK_DATABASE_NAME": name}
        if kind != "mysql":
            env.update(
                PGHOST=host,
                PGPORT=str(service["port"]),
                PGUSER=service["user"],
                PGDATABASE=name,
                PGPASSWORD=password,
                PGSSLMODE="verify-full" if service["tls"] else "disable",
                PGSERVICEFILE="/dev/null",
            )
        else:
            env.update(MYSQL_HOST=host, MYSQL_PWD=password)
        return {"port": service["port"], "user": service["user"], "tls": service["tls"], "env": env}

    def connect(self, *, kind, host, port, user, credential_ref, tls):
        service = {
            "kind": kind,
            "host": "127.0.0.1" if host == "localhost" else host,
            "port": port,
            "user": user,
            "credential_ref": credential_ref,
            "tls": tls,
        }
        if not self._valid(service):
            raise SourceError("INVALID_DATABASE_SERVICE: use a local database service")
        configuration = self.configuration(service)
        sql = (
            "SELECT json_build_object('user',current_user)::text;"
            if kind != "mysql"
            else "SELECT JSON_OBJECT('user',SUBSTRING_INDEX(CURRENT_USER(),'@',1));"
        )
        try:
            result = json.loads(self.query(configuration, sql).strip())
        except (ValueError, TypeError):
            raise SourceError("DATABASE_RESPONSE_INVALID") from None
        if result.get("user") != user:
            raise SourceError("DATABASE_SERVICE_ACCOUNT_MISMATCH")
        return self._save_connection(service, self.reader(credential_ref))

    def _save_connection(self, service, password):
        identity = json.dumps(self._identity(service), sort_keys=True)
        service_id = "service-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        service["authentication_digest"] = self._authentication_digest(service, password)
        service["verified_at"] = time.time()
        with self.lock:
            values = {**self.services, service_id: service}
            write_state(self.state, "services.json", {"services": values})
            self.services = values
        return self._public(service_id, service)

    def connect_redis(self, *, host, port, user, credential_ref, tls, database):
        from code_context.redis_connection import probe, valid

        service = {
            "kind": "redis",
            "host": "127.0.0.1" if host == "localhost" else host,
            "port": port,
            "user": user,
            "credential_ref": credential_ref,
            "tls": tls,
            "database": database,
        }
        if (
            not valid(service)
            or not isinstance(credential_ref, str)
            or not re.fullmatch(r"colink-db-[a-f0-9]{32}", credential_ref)
        ):
            raise SourceError("INVALID_REDIS_CONNECTION")
        password = self.reader(credential_ref)
        probe(service, password)
        return self._save_connection(service, password)

    def require(self, service_id):
        with self.lock:
            saved = self.services.get(service_id)
        if not saved:
            raise SourceError(
                "DATABASE_SERVICE_CONNECTION_REQUIRED: connect the service once locally"
            )
        service = dict(saved)
        password = self.reader(service["credential_ref"])
        if not hmac.compare_digest(
            service.get("authentication_digest", ""), self._authentication_digest(service, password)
        ):
            raise SourceError("DATABASE_SERVICE_CREDENTIAL_CHANGED: reconnect this service locally")
        return service

    def environment(self):
        with self.lock:
            services = [
                self._public(identifier, value) for identifier, value in self.services.items()
            ]
        endpoints = {
            ("mysql", "127.0.0.1", 3306),
            ("postgresql", "127.0.0.1", 5432),
            ("redis", "127.0.0.1", 6379),
        }
        endpoints.update((s["kind"], s["host"], s["port"]) for s in services)
        detected = []
        for kind, host, port in sorted(endpoints):
            ready = False
            try:
                with socket.create_connection((host, port), timeout=0.15):
                    ready = True
            except OSError:
                pass
            detected.append(
                {
                    "kind": kind,
                    "host": host,
                    "port": port,
                    "tcp_ready": ready,
                    "evidence": "tcp_listener_only",
                }
            )
        return {
            "services": services,
            "detected": detected,
            "service_connection_required": not any(
                s["credential_available"] and s["kind"] != "redis" for s in services
            ),
        }

    def _journal(self, target_id, value):
        with self.lock:
            values = {**self.provisions, target_id: value}
            write_state(self.state, "provisions.json", {"provisions": values})
            self.provisions = values

    def provision(self, profile, target_id, *, existing=False):
        service = self.require(profile["service_id"])
        if self.fingerprint(service) != profile["service_digest"]:
            raise SourceError("DATABASE_SERVICE_CHANGED: prepare this connection again")
        if service["tls"]:
            raise SourceError("DATABASE_PROXY_TLS_UNSUPPORTED")
        name, user = profile["database"], profile["user"]
        if not NAME.fullmatch(name) or not re.fullmatch(r"colink_[a-f0-9]{24}", user):
            raise SourceError("INVALID_DATABASE_PROVISION")
        previous = self.provisions.get(target_id)
        cfg = self.configuration(service)
        exists_sql = (
            f"SELECT count(*) FROM pg_database WHERE datname='{name}';"
            if service["kind"] != "mysql"
            else f"SELECT count(*) FROM information_schema.schemata WHERE schema_name='{name}';"
        )
        exists = self.query(cfg, exists_sql).strip() == "1"
        if previous:
            if (
                previous.get("state") != "complete"
                or previous.get("service_digest") != profile["service_digest"]
            ):
                raise SourceError(
                    "DATABASE_PROVISIONING_RECOVERY_REQUIRED: inspect the preserved local journal"
                )
            if not exists:
                raise SourceError("DATABASE_PROVISIONED_TARGET_MISSING: inspect the local journal")
            if service["kind"] != "mysql":
                identity = self.query(
                    cfg, f"SELECT oid::text FROM pg_database WHERE datname='{name}';"
                ).strip()
                if previous.get("instance_identity") != identity:
                    raise SourceError("DATABASE_INSTANCE_CHANGED: inspect the local journal")
            return {
                key: previous[key]
                for key in ("credential_ref", "user", "service_id", "service_digest")
            }
        if exists != existing:
            raise SourceError("DATABASE_ALREADY_EXISTS" if exists else "DATABASE_NOT_FOUND")
        if existing and service["kind"] != "mysql":
            audit = self.query(
                self.configuration(service, database=name),
                "SELECT (EXISTS(SELECT 1 FROM pg_extension "
                "WHERE extname NOT IN ('plpgsql','vector')) "
                "OR EXISTS(SELECT 1 FROM pg_foreign_server) OR EXISTS(SELECT 1 FROM pg_proc p "
                "JOIN pg_namespace n ON p.pronamespace=n.oid JOIN pg_language l ON p.prolang=l.oid "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND (p.prosecdef OR l.lanname NOT IN ('sql','plpgsql','c'))) "
                "OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid "
                "JOIN pg_language l ON p.prolang=l.oid "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND l.lanname='c' AND NOT EXISTS(SELECT 1 FROM pg_depend d JOIN pg_extension e "
                "ON d.refobjid=e.oid WHERE d.objid=p.oid "
                "AND d.deptype='e' AND e.extname='vector')))::int;",
            )
            if audit.strip() != "0":
                raise SourceError(
                    "DATABASE_EXISTING_SCOPE_UNSAFE: review extensions and functions locally"
                )
        if existing and service["kind"] == "mysql":
            # Existing definer objects may execute with an unrelated administrator account.
            audit = self.query(
                cfg,
                "SELECT (EXISTS(SELECT 1 FROM information_schema.routines "
                f"WHERE routine_schema='{name}' AND security_type='DEFINER') "
                "OR EXISTS(SELECT 1 FROM information_schema.triggers "
                f"WHERE trigger_schema='{name}') "
                "OR EXISTS(SELECT 1 FROM information_schema.events "
                f"WHERE event_schema='{name}') "
                "OR EXISTS(SELECT 1 FROM information_schema.views "
                f"WHERE table_schema='{name}' AND security_type='DEFINER'));",
            )
            if audit.strip() != "0":
                raise SourceError("DATABASE_EXISTING_SCOPE_UNSAFE: review definer objects locally")
        credential_ref = profile["credential_ref"]
        password = secrets.token_urlsafe(32)
        journal = {
            "state": "pending",
            "service_id": profile["service_id"],
            "service_digest": profile["service_digest"],
            "database": name,
            "user": user,
            "credential_ref": credential_ref,
            "existing": existing,
            "created_at": time.time(),
        }
        self._journal(target_id, journal)
        self.writer(credential_ref, password)
        if service["kind"] == "mysql":
            partial_revokes = self.query(cfg, "SELECT @@GLOBAL.partial_revokes;").strip()
            if partial_revokes not in {"0", "1"}:
                raise SourceError("DATABASE_GRANT_POLICY_UNVERIFIED")
            literal_db = (
                name if partial_revokes == "1" else name.replace("_", "\\_").replace("%", "\\%")
            )
            sql = f"CREATE USER '{user}'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
            if not existing:
                sql += f"CREATE DATABASE `{name}`;\n"
            sql += f"GRANT ALL PRIVILEGES ON `{literal_db}`.* TO '{user}'@'127.0.0.1';\n"
            self.query(cfg, sql, use_stdin=True)
        else:
            sql = (
                f"CREATE ROLE {user} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;\n"
            )
            if not existing:
                sql += f'CREATE DATABASE "{name}" OWNER {user} TEMPLATE template0;\n'
            else:
                sql += f'GRANT CONNECT,TEMPORARY ON DATABASE "{name}" TO {user};\n'
            self.query(cfg, sql, use_stdin=True)
            target_cfg = self.configuration(service, database=name)
            sql = (
                f"GRANT USAGE,CREATE ON SCHEMA public TO {user};\n"
                f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {user};\n"
                f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {user};\n"
            )
            self.query(target_cfg, sql, use_stdin=True)
        if service["kind"] != "mysql":
            journal["instance_identity"] = self.query(
                cfg, f"SELECT oid::text FROM pg_database WHERE datname='{name}';"
            ).strip()
        self._journal(target_id, {**journal, "state": "complete", "completed_at": time.time()})
        return {
            key: journal[key] for key in ("credential_ref", "user", "service_id", "service_digest")
        }

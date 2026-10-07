"""Local project database profiles; passwords live in the macOS Keychain.

Client presence, TCP readiness and successful authenticated database operations
are distinct. Profiles never create/delete user databases or start global servers.
The webpage runs migrations only in an authorized native execution job.
"""

import hashlib
import json
import math
import re
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import quote

from code_context.local_control import private_directory, read_state, write_state
from code_context.source_access import SourceError

KEYCHAIN_SERVICE = "local.colink.database"


class DatabaseProfiles:
    def __init__(self, root, source_for, *, secret_reader=None):
        self.state = private_directory(root)
        self.lock = threading.RLock()
        self.source_for = source_for
        self.secret_reader = secret_reader or self._secret
        try:
            saved = read_state(self.state, "profiles.json")
            self.profiles = saved["profiles"]
            self.verifications = saved.get("verifications", {})
        except SourceError:
            if (self.state.root / "profiles.json").exists():
                raise
            self.profiles = {}
            self.verifications = {}

    @staticmethod
    def fingerprint(profile):
        return hashlib.sha256(
            json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    def _save(self, profiles, verifications):
        write_state(
            self.state,
            "profiles.json",
            {"profiles": profiles, "verifications": verifications},
        )
        self.profiles, self.verifications = profiles, verifications

    @staticmethod
    def _secret(reference):
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                reference,
                "-w",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            close_fds=True,
            timeout=5,
            check=False,
        )
        if result.returncode or len(result.stdout) > 4096:
            raise SourceError("DATABASE_CREDENTIAL_UNAVAILABLE: update this connection locally")
        return result.stdout.decode().removesuffix("\n")

    def configure(self, project_id, *, kind, host, port, database, user, credential_ref, tls=True):
        source = self.source_for(project_id)
        source.ensure_available()
        if (
            kind not in {"mysql", "postgresql", "pgvector", "qdrant"}
            or host not in {"127.0.0.1", "::1", "localhost"}
            or type(port) is not int
            or not 1024 <= port <= 65535
            or type(tls) is not bool
            or re.fullmatch(r"colink_[a-z0-9_]{1,48}", database) is None
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user) is None
            or user.lower() in {"root", "postgres", "admin"}
            or re.fullmatch(r"colink-db-[a-f0-9]{32}", credential_ref) is None
        ):
            raise SourceError(
                "INVALID_DATABASE_PROFILE: use a local project-specific database and role"
            )
        profile = {
            "project_id": project_id,
            "source_id": source.source_id,
            "kind": kind,
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "credential_ref": credential_ref,
            "tls": tls,
        }
        with self.lock:
            profiles = {**self.profiles, project_id: profile}
            verifications = {k: v for k, v in self.verifications.items() if k != project_id}
            self._save(profiles, verifications)
        return self.status(project_id)

    def record_proof(self, project_id, fingerprint, job_id, proof):
        with self.lock:
            return self._record_proof(project_id, fingerprint, job_id, proof)

    def _record_proof(self, project_id, fingerprint, job_id, proof):
        profile = self.profile(project_id)
        if not profile or self.fingerprint(profile) != fingerprint:
            return
        if (
            not isinstance(proof, dict)
            or not isinstance(job_id, str)
            or re.fullmatch(r"job-[a-f0-9]{32}", job_id) is None
            or (
                type(proof.get("vector_extension", "not_verified")) is not bool
                and proof.get("vector_extension", "not_verified") != "not_verified"
            )
            or proof.get("kind") != profile["kind"]
            or proof.get("authenticated") is not True
            or proof.get("password_enforced") is not True
            or proof.get("role_check") != "passed"
        ):
            raise SourceError("DATABASE_PROOF_INVALID")
        verification = {
            "profile_fingerprint": fingerprint,
            "job_id": job_id,
            "verified_at": time.time(),
            "authenticated_connection": "verified_for_job",
            "password_enforced": True,
            "role_check": "passed",
            "vector_extension": proof.get("vector_extension", "not_verified"),
        }
        self._save(self.profiles, {**self.verifications, project_id: verification})

    def profile(self, project_id):
        source = self.source_for(project_id)
        source.ensure_available()
        with self.lock:
            profile = self.profiles.get(project_id)
        if profile and profile["source_id"] != source.source_id:
            raise SourceError("DATABASE_SOURCE_CHANGED: bind the connection again locally")
        return dict(profile) if profile else None

    def status(self, project_id):
        profile = self.profile(project_id)
        from code_context.execution_environment import _fallback

        clients = {name: bool(shutil.which(name) or _fallback(name)) for name in ("mysql", "psql")}
        steps = [
            "先查看本机数据库客户端与服务。",
            "在本机为本项目配置独立数据库和有限权限角色。",
            "在 CoLink 本机界面保存连接，密码保存在钥匙串。",
            "原生执行权限通过后，在隔离任务中运行迁移和后端。",
            "用真实接口验证写入、读取和重启后数据；向量项目还要验证实际检索。",
        ]
        if not profile:
            return {
                "configured": False,
                "project_id": project_id,
                "clients": clients,
                "recommended": "PostgreSQL + pgvector",
                "existing_mysql_supported": clients["mysql"],
                "steps": steps,
                "authenticated_connection": "not_verified",
                "migrations": "not_verified",
                "crud": "not_verified",
                "vector_extension": "not_verified",
            }
        ready = False
        try:
            with socket.create_connection((profile["host"], profile["port"]), timeout=0.2):
                ready = True
        except OSError:
            pass
        public = {
            key: value
            for key, value in profile.items()
            if key not in {"credential_ref", "source_id", "user"}
        }
        result = {
            "configured": True,
            **public,
            "tcp_ready": ready,
            "clients": clients,
            "steps": steps,
            "authenticated_connection": "not_verified",
            "migrations": "not_verified",
            "crud": "not_verified",
            "vector_extension": "not_verified",
            "scope": "locally configured project role/database",
            "data_scope": "server database grants remain authoritative",
            "execution_supported": profile["kind"] in {"mysql", "postgresql", "pgvector"},
        }
        verification = self.verifications.get(project_id)
        if (
            isinstance(verification, dict)
            and verification.get("profile_fingerprint") == self.fingerprint(profile)
            and type(verification.get("verified_at")) in (int, float)
            and math.isfinite(verification["verified_at"])
            and 0 <= time.time() - verification["verified_at"] < 86400
            and isinstance(verification.get("job_id"), str)
            and re.fullmatch(r"job-[a-f0-9]{32}", verification["job_id"])
            and verification.get("authenticated_connection") == "verified_for_job"
            and verification.get("password_enforced") is True
            and verification.get("role_check") == "passed"
            and (
                type(verification.get("vector_extension")) is bool
                or verification.get("vector_extension") == "not_verified"
            )
        ):
            result.update(
                {
                    key: verification[key]
                    for key in (
                        "job_id",
                        "verified_at",
                        "authenticated_connection",
                        "password_enforced",
                        "role_check",
                        "vector_extension",
                    )
                }
            )
        if profile["kind"] == "qdrant":
            result["steps"].insert(
                0, "Qdrant 当前仅保存连接配置；项目级 API 权限未验证前不向项目进程交付密钥。"
            )
        return result

    def job_configuration(self, project_id):
        profile = self.profile(project_id)
        if not profile:
            raise SourceError("DATABASE_NOT_CONFIGURED: follow the local database setup guide")
        password = self.secret_reader(profile["credential_ref"])
        if not isinstance(password, str) or len(password) > 4096 or "\x00" in password:
            raise SourceError("INVALID_DATABASE_CREDENTIAL")
        kind, host, port, user, name = (
            profile[key] for key in ("kind", "host", "port", "user", "database")
        )
        address = f"[{host}]" if ":" in host else host
        if host == "localhost":
            host, address = "127.0.0.1", "127.0.0.1"
        scheme = "postgresql" if kind in {"postgresql", "pgvector"} else kind
        url = (
            f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{address}:{port}/{name}"
        )
        if kind in {"postgresql", "pgvector"}:
            url += "?sslmode=" + ("verify-full" if profile["tls"] else "disable")
        env = {"COLINK_DATABASE_NAME": name, "COLINK_DATABASE_KIND": kind, "DATABASE_URL": url}
        if kind in {"postgresql", "pgvector"}:
            env.update(
                PGHOST=host,
                PGPORT=str(port),
                PGDATABASE=name,
                PGUSER=user,
                PGPASSWORD=password,
                PGSSLMODE="verify-full" if profile["tls"] else "disable",
                PGSERVICEFILE="/dev/null",
            )
        elif kind == "mysql":
            env.update(MYSQL_HOST=host, MYSQL_TCP_PORT=str(port), MYSQL_PWD=password)
        elif kind == "qdrant":
            env.update(
                QDRANT_URL=f"http{'s' if profile['tls'] else ''}://{address}:{port}",
                QDRANT_API_KEY=password,
            )
        return {
            "port": port,
            "user": user,
            "tls": profile["tls"],
            "env": env,
            "redactions": [password, quote(password, safe=""), url],
            "profile_digest": self.fingerprint(profile),
        }

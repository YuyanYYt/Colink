"""Local project database profiles; passwords live in the macOS Keychain.

Client presence, TCP readiness and successful authenticated database operations
are distinct. Native approval may provision one exact database and a limited
account. The webpage runs migrations only in an authorized native execution job.
"""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import selectors
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import quote

from code_context.database_discovery import discover, target_id, target_identity, valid_target
from code_context.local_control import private_directory, read_state, write_state
from code_context.source_access import SourceError

KEYCHAIN_SERVICE = "local.colink.database"


class DatabaseProfiles:
    def __init__(
        self,
        root,
        source_for,
        *,
        secret_reader=None,
        secret_writer=None,
        grant_root=None,
        on_revoke=None,
        scope_enforcement=True,
    ):
        self.state = private_directory(root)
        self.lock = threading.RLock()
        self.source_for = source_for
        self.secret_reader = secret_reader or self._secret
        if secret_writer is None:
            from code_context.database_keychain import store_secret

            secret_writer = store_secret
        self.secret_writer = secret_writer
        self.scope_enforcement = scope_enforcement
        self.proxies = []
        self._read_context = threading.local()
        self.on_revoke = on_revoke
        try:
            saved = read_state(self.state, "profiles.json")
            self.profiles = saved["profiles"]
            self.verifications = saved.get("verifications", {})
        except SourceError:
            if (self.state.root / "profiles.json").exists():
                raise
            self.profiles = {}
            self.verifications = {}
        self.grant_state = private_directory(grant_root or self.state.root / "grants")
        try:
            self.grants = read_state(self.grant_state, "grants.json")["grants"]
        except SourceError:
            if (self.grant_state.root / "grants.json").exists():
                raise
            self.grants = {}
        try:
            self.selections = read_state(self.state, "selections.json")["selections"]
        except SourceError:
            if (self.state.root / "selections.json").exists():
                raise
            self.selections = {}
        try:
            self.authorization_key = read_state(self.grant_state, "authorization-key.json")["key"]
        except SourceError:
            if (self.grant_state.root / "authorization-key.json").exists():
                raise
            self.authorization_key = secrets.token_hex(32)
            write_state(self.grant_state, "authorization-key.json", {"key": self.authorization_key})
        if not isinstance(self.authorization_key, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.authorization_key
        ):
            raise SourceError("DATABASE_AUTHORIZATION_STATE_INVALID")
        self.probes = {}
        try:
            self.overlays = read_state(self.grant_state, "runtime-overlays.json")["overlays"]
        except SourceError:
            if (self.grant_state.root / "runtime-overlays.json").exists():
                raise
            self.overlays = {}
        from code_context.database_services import DatabaseServices

        self.services = DatabaseServices(
            self.grant_state.root / "services",
            secret_reader=self.secret_reader,
            secret_writer=self.secret_writer,
            query=self._query,
            authorization_key=self.authorization_key,
        )

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
        from code_context.database_keychain import read_secret

        return read_secret(reference)

    def configure(self, project_id, *, kind, host, port, database, user, credential_ref, tls=True):
        source = self.source_for(project_id)
        source.ensure_available()
        if (
            not valid_target(
                {
                    "kind": kind,
                    "host": host,
                    "port": port,
                    "database": database,
                    "user": user,
                    "tls": tls,
                }
            )
            or re.fullmatch(r"colink-db-[a-f0-9]{32}", credential_ref) is None
        ):
            raise SourceError("INVALID_DATABASE_PROFILE: a valid local database target is required")
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
            or (
                proof.get("password_enforced") is not True
                and not (
                    (
                        proof.get("role_check") == "approved_account"
                        and not self.scope_enforcement
                        or proof.get("role_check") == "passed"
                        and proof.get("target_enforced") is True
                        and self.scope_enforcement
                    )
                    and proof.get("password_enforced") is False
                )
            )
            or proof.get("role_check") not in {"passed", "approved_account"}
            or self.scope_enforcement
            and (proof.get("role_check") != "passed" or proof.get("target_enforced") is not True)
        ):
            raise SourceError("DATABASE_PROOF_INVALID")
        if proof.get("database_action") in {"create", "drop"}:
            # A maintenance-connection proof is never target database authentication proof.
            return
        verification = {
            "profile_fingerprint": fingerprint,
            "job_id": job_id,
            "verified_at": time.time(),
            "authenticated_connection": "verified_for_job",
            "password_enforced": proof["password_enforced"],
            "role_check": proof["role_check"],
            "vector_extension": proof.get("vector_extension", "not_verified"),
        }
        self._save(self.profiles, {**self.verifications, project_id: verification})

    def _resolve(self, project_id):
        source = self.source_for(project_id)
        source.ensure_available()
        with self.lock:
            saved = self.profiles.get(project_id)
            selection = self.selections.get(project_id)
        manual = dict(saved) if saved else None
        if manual and manual["source_id"] != source.source_id:
            raise SourceError("DATABASE_SOURCE_CHANGED: bind the connection again locally")
        injected = {}
        if manual and "service_id" in manual:
            service = self.services.require(manual["service_id"])
            if self.services.fingerprint(service) != manual["service_digest"]:
                raise SourceError("DATABASE_SERVICE_CHANGED: prepare the connection again")
            try:
                password = self.secret_reader(manual["credential_ref"])
            except SourceError:
                if self.services.provisions.get(target_id(manual), {}).get("state") == "complete":
                    raise
                password = ""
            manual["_password"] = password
            injected["COLINK_DB_PASSWORD"] = password
        if manual and "credential_source" in manual:
            reference = manual["credential_source"]
            if (
                not isinstance(reference, dict)
                or reference.get("project_id") == project_id
                or "credential_source" in self.profiles.get(reference.get("project_id"), {})
            ):
                raise SourceError("DATABASE_CONNECTION_SOURCE_INVALID")
            try:
                baseline = self.require_target(
                    reference["project_id"], reference["database_target_id"]
                )
            except (SourceError, KeyError):
                raise SourceError(
                    "DATABASE_CONNECTION_SOURCE_UNAVAILABLE: refresh the authorized connection"
                ) from None
            if self.fingerprint(baseline) != reference.get("profile_digest"):
                raise SourceError(
                    "DATABASE_CONNECTION_SOURCE_CHANGED: prepare this connection again"
                )
            password = (
                baseline["_password"]
                if "_password" in baseline
                else self.secret_reader(baseline["credential_ref"])
            )
            manual["_password"] = password
            injected["COLINK_DB_PASSWORD"] = password
        discovered, unresolved, config_digest = discover(source, connection_environment=injected)
        candidates = {}
        conflicts = set()
        for item in discovered:
            identifier = item["database_target_id"]
            profile = {
                "project_id": project_id,
                "source_id": source.source_id,
                **target_identity(item),
                "_password": item["password"],
                "_config_digest": config_digest,
            }
            if identifier in candidates:
                if candidates[identifier]["profile"].get("_password") != item["password"]:
                    conflicts.add(identifier)
                candidates[identifier]["origins"].append(item["origin"])
            else:
                candidates[identifier] = {"profile": profile, "origins": [item["origin"]]}
        if manual:
            identifier = target_id(manual)
            if identifier in candidates:
                # The current project config supplies credentials for the same target.
                candidates[identifier]["origins"].append("saved_connection")
                if "service_id" in manual:
                    candidates[identifier]["profile"].update(
                        {
                            key: manual[key]
                            for key in (
                                "service_id",
                                "service_digest",
                                "prepared",
                                "credential_ref",
                            )
                            if key in manual
                        }
                    )
            else:
                candidates[identifier] = {"profile": dict(manual), "origins": ["saved_connection"]}
        public = []
        for identifier, candidate in candidates.items():
            public.append(
                {
                    "database_target_id": identifier,
                    **target_identity(candidate["profile"]),
                    "origins": sorted(set(candidate["origins"])),
                    "credential_conflict": identifier in conflicts,
                }
            )
        chosen = None
        if len(candidates) == 1 and not conflicts and not unresolved:
            chosen = next(iter(candidates))
        elif (
            isinstance(selection, dict)
            and selection.get("source_id") == source.source_id
            and selection.get("config_digest") == config_digest
            and selection.get("database_target_id") in candidates
            and selection["database_target_id"] not in conflicts
        ):
            chosen = selection["database_target_id"]
        profile = dict(candidates[chosen]["profile"]) if chosen else None
        return profile, public, unresolved, config_digest

    def profile(self, project_id):
        return self._resolve(project_id)[0]

    def select(self, project_id, database_target_id):
        source = self.source_for(project_id)
        _, candidates, _, config_digest = self._resolve(project_id)
        if not any(
            c["database_target_id"] == database_target_id and not c["credential_conflict"]
            for c in candidates
        ):
            raise SourceError("DATABASE_TARGET_UNAVAILABLE: refresh discovered targets")
        with self.lock:
            selections = {
                **self.selections,
                project_id: {
                    "database_target_id": database_target_id,
                    "config_digest": config_digest,
                    "source_id": source.source_id,
                },
            }
            write_state(self.state, "selections.json", {"selections": selections})
            self.selections = selections
        return self.status(project_id)

    def prepare(self, project_id, service_id, database_name):
        """Prepare one new target from a native service connection; no project file writes."""
        source = self.source_for(project_id)
        source.ensure_available()
        service = self.services.require(service_id)
        if service["kind"] not in {"mysql", "postgresql", "pgvector"}:
            raise SourceError("DATABASE_PREPARE_UNSUPPORTED: Redis supports connection checks only")
        if not isinstance(database_name, str):
            raise SourceError("INVALID_DATABASE_PROFILE")
        with self.lock:
            previous = self.profiles.get(project_id)
        if previous:
            if (
                previous.get("service_id") != service_id
                or previous.get("database") != database_name
            ):
                raise SourceError(
                    "DATABASE_TARGET_CONFLICT: existing connection must be reviewed locally"
                )
            profile = {**previous, "service_digest": self.services.fingerprint(service)}
        else:
            profile = {
                "project_id": project_id,
                "source_id": source.source_id,
                "kind": service["kind"],
                "host": service["host"],
                "port": service["port"],
                "database": database_name,
                "user": "colink_" + secrets.token_hex(12),
                "credential_ref": "colink-db-" + secrets.token_hex(16),
                "tls": service["tls"],
                "service_id": service_id,
                "service_digest": self.services.fingerprint(service),
                "prepared": True,
            }
        if not valid_target(profile):
            raise SourceError("INVALID_DATABASE_PROFILE: choose a valid local database name")
        discovered, unresolved, _ = discover(source)
        desired = target_id(profile)
        if any(item["database_target_id"] != desired for item in discovered):
            raise SourceError(
                "DATABASE_TARGET_CONFLICT: project config points to a different target"
            )
        if unresolved and not previous:
            raise SourceError("DATABASE_CONFIG_UNRESOLVED: review existing project configuration")
        with self.lock:
            self._save(
                {**self.profiles, project_id: profile},
                {k: v for k, v in self.verifications.items() if k != project_id},
            )
        result = self.status(project_id)
        kind = "postgresql" if profile["kind"] == "pgvector" else profile["kind"]
        host = "[::1]" if profile["host"] == "::1" else profile["host"]
        url = f"jdbc:{kind}://{host}:{profile['port']}/{quote(database_name, safe='')}"
        if kind == "postgresql":
            url += "?sslmode=" + ("verify-full" if profile["tls"] else "disable")
        result["configuration_template"] = {
            "format": "spring_properties",
            "suggested_path": "src/main/resources/application.properties",
            "content": f"spring.datasource.url={url}\n"
            f"spring.datasource.username={profile['user']}\n"
            "spring.datasource.password=${COLINK_DB_PASSWORD}\n",
            "write_required": True,
        }
        result["next_steps"] = [
            "write_configuration",
            "approve_target_locally",
            "enable_project_development_locally",
            "run_project",
        ]
        return result

    def environment(self, project_id):
        source = self.source_for(project_id)
        source.ensure_available()
        from code_context.execution_environment import _fallback
        from code_context.redis_connection import discover as discover_redis
        from code_context.redis_connection import public

        redis, unresolved = discover_redis(source)

        return {
            "project_id": project_id,
            **self.services.environment(),
            "clients": {
                name: bool(shutil.which(name) or _fallback(name))
                for name in ("mysql", "psql", "redis-cli", "redis-server")
            },
            "redis_configurations": [public(item) for item in redis],
            "redis_unresolved_configs": unresolved,
            "bootstrap": {
                "supported_engines": ["mysql", "postgresql", "pgvector"],
                "prepare_tool": "database_prepare",
                "approval_location": "desktop",
                "configuration_required_before_prepare": False,
                "creates_only_exact_named_database": True,
            },
        }

    def connect_service(self, project_id, **values):
        self.source_for(project_id).ensure_available()
        return self.services.connect(**values)

    def connect_redis_service(self, project_id, **values):
        self.source_for(project_id).ensure_available()
        return self.services.connect_redis(**values)

    def connect_redis_configuration(self, project_id, config_id):
        from code_context.redis_connection import discover as discover_redis
        from code_context.redis_connection import probe

        candidates, _ = discover_redis(self.source_for(project_id))
        candidate = next((item for item in candidates if item["config_id"] == config_id), None)
        if candidate is None or candidate.get("credential_conflict"):
            raise SourceError("REDIS_CONFIG_CHANGED: rediscover the project configuration")
        # Validate before creating a credential record; no data commands are sent.
        probe(candidate, candidate["password"])
        latest, _ = discover_redis(self.source_for(project_id))
        if candidate not in latest:
            raise SourceError("REDIS_CONFIG_CHANGED: rediscover the project configuration")
        reference = "colink-db-" + secrets.token_hex(16)
        self.secret_writer(reference, candidate["password"])
        service = {
            key: candidate[key] for key in ("kind", "host", "port", "user", "tls", "database")
        }
        service["credential_ref"] = reference
        return self.services._save_connection(service, candidate["password"])

    def _install_overlay(self, profile, identifier):
        old = self.overlays.get(identifier)
        if old and old.get("source_authentication_digest") == self._authorization_fingerprint(
            profile
        ):
            service = self.services.require(old["service_id"])
            if self.services.fingerprint(service) == old["service_digest"]:
                return
        if "service_id" in profile:
            provision_profile = profile
            existing = not profile.get("prepared", False)
        else:
            # This native-local action may reuse current project credentials for bootstrap only.
            reference = "colink-db-" + secrets.token_hex(16)
            password = (
                profile["_password"]
                if "_password" in profile
                else self.secret_reader(profile["credential_ref"])
            )
            reusable = next(
                (
                    sid
                    for sid, entry in self.services.services.items()
                    if all(entry[k] == profile[k] for k in ("kind", "host", "port", "user", "tls"))
                    and entry.get("authentication_digest")
                    == self.services._authentication_digest(entry, password)
                ),
                None,
            )
            if reusable:
                saved = {"service_id": reusable}
            else:
                self.secret_writer(reference, password)
                saved = self.services.connect(
                    kind=profile["kind"],
                    host=profile["host"],
                    port=profile["port"],
                    user=profile["user"],
                    credential_ref=reference,
                    tls=profile["tls"],
                )
            service = self.services.require(saved["service_id"])
            provision_profile = {
                **profile,
                "service_id": saved["service_id"],
                "service_digest": self.services.fingerprint(service),
                "user": "colink_" + secrets.token_hex(12),
                "credential_ref": "colink-db-" + secrets.token_hex(16),
            }
            existing = True
        provision = self.services.provision(provision_profile, identifier, existing=existing)
        if profile.get("prepared"):
            profile = self.require_target(profile["project_id"], identifier, authorized=False)
        overlay = {
            **target_identity(profile),
            **provision,
            "state": "complete",
            "source_authentication_digest": self._authorization_fingerprint(profile),
        }
        with self.lock:
            overlays = {**self.overlays, identifier: overlay}
            write_state(self.grant_state, "runtime-overlays.json", {"overlays": overlays})
            self.overlays = overlays

    def _authorization_fingerprint(self, profile):
        password = (
            profile["_password"]
            if "_password" in profile
            else self.secret_reader(profile["credential_ref"])
        )
        if not isinstance(password, str) or len(password) > 4096 or "\x00" in password:
            raise SourceError("INVALID_DATABASE_CREDENTIAL")
        material = json.dumps(
            {"target": target_identity(profile), "password": password}, sort_keys=True
        )
        return hmac.new(
            bytes.fromhex(self.authorization_key), material.encode(), hashlib.sha256
        ).hexdigest()

    def authorized(self, profile):
        with self.lock:
            grant = self.grants.get(target_id(profile))
        if (
            not isinstance(grant, dict)
            or grant.get("target") != target_identity(profile)
            or self.scope_enforcement
            and self.overlays.get(target_id(profile), {}).get("state") != "complete"
        ):
            return False
        try:
            return hmac.compare_digest(
                grant.get("authentication_digest", ""), self._authorization_fingerprint(profile)
            )
        except SourceError:
            return False

    def authorize_target(self, project_id, database_target_id):
        profile = self.require_target(project_id, database_target_id, authorized=False)
        if self.scope_enforcement:
            self._install_overlay(profile, database_target_id)
            profile = self.require_target(project_id, database_target_id, authorized=False)
        with self.lock:
            grants = {
                **self.grants,
                database_target_id: {
                    "target": target_identity(profile),
                    "authentication_digest": self._authorization_fingerprint(profile),
                    "approved_at": time.time(),
                    "existed": False,
                },
            }
            write_state(self.grant_state, "grants.json", {"grants": grants})
            self.grants = grants
        return self.status(project_id)

    def mark_existing(self, database_target_id, instance_identity=None):
        with self.lock:
            grant = self.grants.get(database_target_id)
            if not grant:
                return
            previous = grant.get("instance_identity")
            if instance_identity and previous and previous != instance_identity:
                self.revoke(database_target_id)
                raise SourceError(
                    "DATABASE_INSTANCE_CHANGED: approve the recreated database locally"
                )
            if grant.get("existed") is True and (
                not instance_identity or previous == instance_identity
            ):
                return
            updated = {**grant, "existed": True}
            if instance_identity:
                updated["instance_identity"] = instance_identity
            grants = {**self.grants, database_target_id: updated}
            write_state(self.grant_state, "grants.json", {"grants": grants})
            self.grants = grants

    def revoke(self, database_target_id):
        if not isinstance(database_target_id, str) or not re.fullmatch(
            r"db-[a-f0-9]{32}", database_target_id
        ):
            raise SourceError("INVALID_DATABASE_TARGET")
        with self.lock:
            grants = {key: value for key, value in self.grants.items() if key != database_target_id}
            write_state(self.grant_state, "grants.json", {"grants": grants})
            self.grants = grants
            self.probes.pop(database_target_id, None)
        jobs = self.on_revoke(database_target_id) if self.on_revoke else []
        return {"revoked": True, "database_target_id": database_target_id, "jobs": jobs}

    def require_target(self, project_id, database_target_id, *, authorized=True):
        if not isinstance(database_target_id, str) or not re.fullmatch(
            r"db-[a-f0-9]{32}", database_target_id
        ):
            raise SourceError("DATABASE_TARGET_REQUIRED: use the current selected target")
        profile = self.profile(project_id)
        if not profile or target_id(profile) != database_target_id:
            raise SourceError("DATABASE_TARGET_CHANGED: refresh and select the current target")
        if authorized and not self.authorized(profile):
            raise SourceError("DATABASE_AUTHORIZATION_REQUIRED: approve this target locally")
        return profile

    def status(self, project_id):
        profile, candidates, unresolved, _ = self._resolve(project_id)
        from code_context.execution_environment import _fallback

        clients = {name: bool(shutil.which(name) or _fallback(name)) for name in ("mysql", "psql")}
        result = {
            "project_id": project_id,
            "configured": profile is not None,
            "candidates": candidates,
            "unresolved_configs": unresolved,
            "selection_required": bool(candidates) and profile is None,
            "database_target_id": target_id(profile) if profile else None,
            "authorized": bool(profile and self.authorized(profile)),
            "clients": clients,
            "existing_mysql_supported": clients["mysql"],
            "authenticated_connection": "not_verified",
            "migrations": "not_verified",
            "crud": "not_verified",
            "vector_extension": "not_verified",
            "connection_status": "selection_required" if candidates else "not_configured",
            "data_scope": "server database grants remain authoritative",
            "approval_location": "desktop",
            "bootstrap_available": True,
            "next_action": "select_database" if candidates else "database_environment",
        }
        if not profile:
            return result
        # Keep legacy top-level shape without exporting user/credential references.
        result.update({key: profile[key] for key in ("kind", "host", "port", "database", "tls")})
        result["broad_account"] = profile["user"].lower() in {"root", "postgres", "admin"}
        result["execution_supported"] = profile["kind"] in {"mysql", "postgresql", "pgvector"}
        result["connection_status"] = (
            "not_verified" if result["authorized"] else "authorization_required"
        )
        result["selection_required"] = False
        result["provisioning_required"] = bool(
            profile.get("prepared")
            and self.overlays.get(target_id(profile), {}).get("state") != "complete"
        )
        result["next_action"] = (
            "check_project_execution_permission"
            if result["authorized"]
            else "approve_target_locally"
        )
        ready = False
        try:
            with socket.create_connection((profile["host"], profile["port"]), timeout=0.2):
                ready = True
        except OSError:
            pass
        result["tcp_ready"] = ready
        probe = self.probes.get(target_id(profile))
        if (
            result["authorized"]
            and isinstance(probe, dict)
            and probe.get("fingerprint") == self.fingerprint(profile)
            and 0 <= time.time() - probe.get("verified_at", 0) < 60
        ):
            result.update(
                {key: probe[key] for key in ("authenticated_connection", "connection_status")}
            )
            result["authenticated_at"] = probe["verified_at"]
            result["authentication_expires_at"] = probe["verified_at"] + 60
        verification = self.verifications.get(project_id)
        if (
            isinstance(verification, dict)
            and verification.get("profile_fingerprint") == self.fingerprint(profile)
            and type(verification.get("verified_at")) in (int, float)
            and math.isfinite(verification["verified_at"])
            and 0 <= time.time() - verification["verified_at"] < 86400
            and verification.get("authenticated_connection") == "verified_for_job"
            and type(verification.get("password_enforced")) is bool
            and verification.get("role_check") in {"passed", "approved_account"}
        ):
            for key in (
                "job_id",
                "verified_at",
                "password_enforced",
                "role_check",
                "vector_extension",
            ):
                result[key] = verification[key]
            # Job proof is distinct from the short-lived live connection indicator.
            result["job_authenticated_connection"] = "verified_for_job"
        return result

    def job_configuration(
        self, project_id, database_target_id=None, *, require_authorized=False, database_action=None
    ):
        profile = (
            self.require_target(project_id, database_target_id)
            if require_authorized
            else self.profile(project_id)
        )
        if not profile:
            raise SourceError("DATABASE_NOT_CONFIGURED: follow the local database setup guide")
        effective = profile
        if self.scope_enforcement:
            effective = self.overlays.get(target_id(profile))
            if not effective or effective.get("state") != "complete":
                raise SourceError(
                    "DATABASE_SCOPE_AUTHORIZATION_REQUIRED: approve the exact database locally"
                )
            if effective.get("source_authentication_digest") != self._authorization_fingerprint(
                profile
            ):
                raise SourceError("DATABASE_SOURCE_CHANGED: approve current connection again")
        password = (
            effective["_password"]
            if "_password" in effective
            else self.secret_reader(effective["credential_ref"])
        )
        if not isinstance(password, str) or len(password) > 4096 or "\x00" in password:
            raise SourceError("INVALID_DATABASE_CREDENTIAL")
        kind, host, port, user, name = (
            effective[key] for key in ("kind", "host", "port", "user", "database")
        )
        target_name = name
        if database_action is not None:
            if self.scope_enforcement:
                raise SourceError(
                    "DATABASE_NATIVE_PROVISION_REQUIRED: creating or dropping databases stays local"
                )
            if database_action not in {"create", "drop"} or not require_authorized:
                raise SourceError("INVALID_DATABASE_ACTION")
            name = "postgres" if kind in {"postgresql", "pgvector"} else "information_schema"
        address = f"[{host}]" if ":" in host else host
        if host == "localhost":
            host, address = "127.0.0.1", "127.0.0.1"
        scheme = "postgresql" if kind in {"postgresql", "pgvector"} else kind
        url = (
            f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{address}:{port}/"
            f"{quote(name, safe='')}"
        )
        if kind in {"postgresql", "pgvector"}:
            url += "?sslmode=" + ("verify-full" if profile["tls"] else "disable")
        env = {
            "COLINK_DATABASE_NAME": name,
            "COLINK_DATABASE_KIND": kind,
            "DATABASE_URL": url,
            "COLINK_DB_PASSWORD": password,
        }
        if kind in {"postgresql", "pgvector", "mysql"}:
            env.update(
                SPRING_DATASOURCE_URL=f"jdbc:{scheme}://{address}:{port}/{name}",
                SPRING_DATASOURCE_USERNAME=user,
                SPRING_DATASOURCE_PASSWORD=password,
            )
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
        proxy = None
        if self.scope_enforcement:
            if effective["tls"]:
                raise SourceError("DATABASE_PROXY_TLS_UNSUPPORTED")
            if kind in {"postgresql", "pgvector"}:
                from code_context.postgres_target_proxy import PostgresTargetProxy

                proxy = PostgresTargetProxy(host, port, name, user, tls=False).start()
            elif kind == "mysql":
                from code_context.mysql_target_proxy import MySQLTargetProxy

                proxy = MySQLTargetProxy(host, port, name, user, tls=False).start()
            else:
                raise SourceError("DATABASE_EXECUTION_UNSUPPORTED")
            host, address, port = proxy.host, proxy.host, proxy.port
            url = (
                f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{address}:{port}/"
                f"{quote(name, safe='')}"
            )
            env["DATABASE_URL"] = url + ("?sslmode=disable" if kind != "mysql" else "")
            jdbc_query = (
                "?sslmode=disable"
                if kind != "mysql"
                else "?sslMode=DISABLED&allowPublicKeyRetrieval=true&allowLoadLocalInfile=false"
            )
            env["SPRING_DATASOURCE_URL"] = (
                f"jdbc:{scheme}://{address}:{port}/{quote(name, safe='')}" + jdbc_query
            )
            if kind != "mysql":
                env.update(PGHOST=host, PGPORT=str(port), PGSSLMODE="disable")
            else:
                env.update(MYSQL_HOST=host, MYSQL_TCP_PORT=str(port))
            with self.lock:
                self.proxies.append(proxy)
        return {
            "proxy": proxy,
            "real_port": effective["port"],
            "port": port,
            "user": user,
            "tls": effective["tls"],
            "approved_account": not self.scope_enforcement and self.authorized(profile),
            "target_enforced": self.scope_enforcement,
            "database_target_id": target_id(profile),
            "database_action": database_action,
            "target_name": target_name,
            "instance_identity": self.grants.get(target_id(profile), {}).get("instance_identity"),
            "env": env,
            "redactions": [password, quote(password, safe=""), url],
            "profile_digest": self.fingerprint(profile),
        }

    def validate_execution_target(self, project_id, database_target_id, database_action=None):
        profile = self.require_target(project_id, database_target_id)
        if database_action == "create" and not self.grants.get(database_target_id, {}).get(
            "existed"
        ):
            return profile
        connection = self.check_connection(project_id)
        if connection["authenticated_connection"] != "verified":
            raise SourceError(connection.get("connection_error", "DATABASE_CONNECTION_FAILED"))
        return self.require_target(project_id, database_target_id)

    def administrative_argv(self, project_id, database_target_id, database_action, tool):
        profile = self.require_target(project_id, database_target_id)
        if database_action not in {"create", "drop"}:
            raise SourceError("INVALID_DATABASE_ACTION")
        if profile["kind"] in {"postgresql", "pgvector"}:
            if tool != "psql":
                raise SourceError("DATABASE_CLIENT_MISMATCH")
            sql = f'{database_action.upper()} DATABASE "{profile["database"]}"'
            return ["--no-psqlrc", "--no-password", "--set", "ON_ERROR_STOP=1", "--command", sql]
        if profile["kind"] == "mysql":
            if tool != "mysql":
                raise SourceError("DATABASE_CLIENT_MISMATCH")
            return ["--execute=" + f"{database_action.upper()} DATABASE `{profile['database']}`"]
        raise SourceError("DATABASE_EXECUTION_UNSUPPORTED")

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

    def _query(self, configuration, sql, *, use_stdin=False):
        from code_context.execution_environment import _fallback

        kind = configuration["env"]["COLINK_DATABASE_KIND"]
        if kind in {"postgresql", "pgvector"}:
            client = shutil.which("psql") or _fallback("psql")
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
            client = shutil.which("mysql") or _fallback("mysql")
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

    def check_connection(self, project_id):
        status = self.status(project_id)
        if not status["configured"] or not status["authorized"]:
            return status
        identifier = status["database_target_id"]
        configuration = self.job_configuration(project_id, identifier, require_authorized=True)
        kind = configuration["env"]["COLINK_DATABASE_KIND"]
        sql = (
            (
                "SELECT json_build_object('database',current_database(),'user',current_user,"
                "'instance_identity',(SELECT oid::text FROM pg_database "
                "WHERE datname=current_database()))::text;"
            )
            if kind in {"postgresql", "pgvector"}
            else (
                "SELECT JSON_OBJECT('database',DATABASE(),"
                "'user',SUBSTRING_INDEX(CURRENT_USER(),'@',1));"
            )
        )
        fingerprint = configuration["profile_digest"]
        connection, failure = "verified", None
        try:
            result = json.loads(self._query(configuration, sql).strip())
            profile = self.require_target(project_id, identifier)
            if (
                self.fingerprint(profile) != fingerprint
                or result.get("database") != profile["database"]
                or result.get("user") != configuration["user"]
            ):
                raise SourceError("DATABASE_TARGET_VERIFICATION_FAILED")
            self.mark_existing(identifier, result.get("instance_identity"))
        except SourceError as exc:
            connection, failure = "failed", str(exc).split(":", 1)[0]
            if failure == "DATABASE_NOT_FOUND" and self.grants.get(identifier, {}).get("existed"):
                self.revoke(identifier)
        except (ValueError, TypeError, AttributeError):
            connection, failure = "failed", "DATABASE_RESPONSE_INVALID"
        with self.lock:
            self.probes[identifier] = {
                "fingerprint": fingerprint,
                "verified_at": time.time(),
                "authenticated_connection": connection,
                "connection_status": "connected" if connection == "verified" else "failed",
                "error_code": failure,
            }
        self.release_configuration(configuration)
        result = self.status(project_id)
        if failure:
            result["connection_error"] = failure
        return result

    def read(self, project_id, database_target_id, action, *, table="", schema="public", limit=100):
        self._read_context.configuration = None
        try:
            return self._read(
                project_id, database_target_id, action, table=table, schema=schema, limit=limit
            )
        finally:
            # _read publishes its client configuration before issuing queries.
            configuration = getattr(self._read_context, "configuration", None)
            if configuration is not None:
                self.release_configuration(configuration)
                self._read_context.configuration = None

    def _read(
        self, project_id, database_target_id, action, *, table="", schema="public", limit=100
    ):
        """Fixed metadata and base-table previews. No caller-provided SQL or client options."""
        if (
            action not in {"list_tables", "describe", "preview"}
            or type(limit) is not int
            or not 1 <= limit <= 100
            or not isinstance(table, str)
            or not isinstance(schema, str)
            or action != "list_tables"
            and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,63}", table)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,63}", schema)
        ):
            raise SourceError("INVALID_DATABASE_READ: select a table and a bounded preview")
        profile = self.require_target(project_id, database_target_id)
        connection = self.check_connection(project_id)
        if connection["authenticated_connection"] != "verified":
            raise SourceError(connection.get("connection_error", "DATABASE_CONNECTION_FAILED"))
        profile = self.require_target(project_id, database_target_id)
        fingerprint = self.fingerprint(profile)
        configuration = self.job_configuration(
            project_id, database_target_id, require_authorized=True
        )
        self._read_context.configuration = configuration
        if profile["kind"] in {"postgresql", "pgvector"}:
            if action == "list_tables":
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
            if action == "list_tables":
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
        connection = self.check_connection(project_id)
        if connection["authenticated_connection"] != "verified":
            raise SourceError(connection.get("connection_error", "DATABASE_CONNECTION_FAILED"))
        current = self.require_target(project_id, database_target_id)
        if self.fingerprint(current) != fingerprint:
            raise SourceError("DATABASE_CONFIG_CHANGED: discard this result and refresh")
        result = {
            "project_id": project_id,
            "database_target_id": database_target_id,
            "action": action,
            "data": data,
            "max_rows": limit if action == "preview" else 100,
        }
        if len(json.dumps(result, ensure_ascii=False).encode()) > 65536:
            raise SourceError("DATABASE_RESPONSE_LIMIT: choose a smaller preview")
        return result

    def raw_service_ports(self):
        ports = {3306, 5432}
        ports.update(value["port"] for value in self.services.services.values())
        ports.update(value["port"] for value in self.profiles.values())
        ports.update(proxy.port for proxy in self.proxies)
        return ports

    def release_configuration(self, configuration):
        proxy = configuration.get("proxy")
        if proxy:
            proxy.close()
            with self.lock:
                self.proxies = [active for active in self.proxies if active is not proxy]

    def close(self):
        with self.lock:
            proxies, self.proxies = self.proxies, []
        for proxy in proxies:
            proxy.close()

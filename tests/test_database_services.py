"""Native service bootstrap boundaries; all passwords and query results are fixtures."""

import json
import re
from types import SimpleNamespace

import pytest

import code_context.database_services as service_module
from code_context.database_services import DatabaseServices
from code_context.execution_database import DatabaseProfiles
from code_context.source_access import SourceAccess, SourceError

REFERENCE = "colink-db-" + "a" * 32
ADMIN_PASSWORD = "fixture-administrator-secret"


class FakeServer:
    def __init__(self):
        self.databases = {"existing": "41"}
        self.queries = []
        self.partial_revokes = "0"
        self.unsafe = False

    def query(self, cfg, sql, *, use_stdin=False):
        self.queries.append((cfg, sql, use_stdin))
        if "json_build_object('user'" in sql or "JSON_OBJECT('user'" in sql:
            return json.dumps({"user": cfg["user"]})
        if "SELECT count(*)" in sql:
            name = re.search(r"(?:datname|schema_name)='([^']+)'", sql)[1]
            return "1" if name in self.databases else "0"
        if "SELECT oid::text" in sql:
            return self.databases[re.search(r"datname='([^']+)'", sql)[1]]
        if "SELECT (EXISTS" in sql:
            return "1" if self.unsafe else "0"
        if "@@GLOBAL.partial_revokes" in sql:
            return self.partial_revokes
        if "CREATE DATABASE" in sql:
            self.databases[re.search(r'CREATE DATABASE [`"]([^`"]+)[`"]', sql)[1]] = "42"
        if "current_database()" in sql or "'database',DATABASE()" in sql:
            name = cfg["env"]["COLINK_DATABASE_NAME"]
            return json.dumps(
                {"database": name, "user": cfg["user"], "instance_identity": self.databases[name]}
            )
        return ""


def fixture(tmp_path, kind="postgresql"):
    secrets = {REFERENCE: ADMIN_PASSWORD}
    server = FakeServer()

    def reader(reference):
        if reference not in secrets:
            raise SourceError("DATABASE_CREDENTIAL_UNAVAILABLE")
        return secrets[reference]

    def writer(ref, value):
        secrets[ref] = value

    services = DatabaseServices(
        tmp_path / "services",
        secret_reader=reader,
        secret_writer=writer,
        query=server.query,
        authorization_key="b" * 64,
    )
    connected = services.connect(
        kind=kind,
        host="127.0.0.1",
        port=15432 if kind != "mysql" else 13306,
        user="postgres" if kind != "mysql" else "root",
        credential_ref=REFERENCE,
        tls=False,
    )
    service = services.require(connected["service_id"])
    return SimpleNamespace(
        services=services,
        server=server,
        secrets=secrets,
        reader=reader,
        writer=writer,
        connected=connected,
        service=service,
        kind=kind,
    )


def prepared(parts, name="ordinary_store"):
    return {
        "service_id": parts.connected["service_id"],
        "service_digest": parts.services.fingerprint(parts.service),
        "database": name,
        "user": "colink_" + "c" * 24,
        "credential_ref": "colink-db-" + "d" * 32,
    }


def test_service_metadata_never_contains_password_and_missing_keychain_is_not_verified(tmp_path):
    parts = fixture(tmp_path)
    connected = parts.connected
    assert connected["authenticated_connection"] == "verified"
    assert connected["authentication_expires_at"] - connected["authenticated_at"] == 60
    assert ADMIN_PASSWORD not in json.dumps(connected)
    assert ADMIN_PASSWORD not in (parts.services.state.root / "services.json").read_text()
    del parts.secrets[REFERENCE]
    public = parts.services._public(connected["service_id"], parts.service)
    assert public["credential_available"] is False
    assert public["authenticated_connection"] == "not_verified"


def test_service_credential_rotation_invalidates_prepared_targets(tmp_path):
    parts = fixture(tmp_path)
    parts.secrets[REFERENCE] = "fixture-rotated"
    with pytest.raises(SourceError, match="DATABASE_SERVICE_CREDENTIAL_CHANGED"):
        parts.services.provision(prepared(parts), "db-" + "1" * 32)


def test_service_failed_save_does_not_publish_verified_connection(tmp_path, monkeypatch):
    parts = fixture(tmp_path)
    before = dict(parts.services.services)

    def failed(*args):
        raise SourceError("CONTROL_STATE_FAILED")

    monkeypatch.setattr(service_module, "write_state", failed)
    with pytest.raises(SourceError, match="CONTROL_STATE_FAILED"):
        parts.services.connect(
            kind="mysql",
            host="127.0.0.1",
            port=13306,
            user="root",
            credential_ref=REFERENCE,
            tls=False,
        )
    assert parts.services.services == before


@pytest.mark.parametrize("kind", ["postgresql", "mysql"])
def test_new_database_provision_is_exact_limited_and_admin_secret_not_persisted(tmp_path, kind):
    parts = fixture(tmp_path, kind)
    profile = prepared(parts)
    provision = parts.services.provision(profile, "db-" + "2" * 32)
    assert provision["user"] == profile["user"]
    child_password = parts.secrets[profile["credential_ref"]]
    assert child_password != ADMIN_PASSWORD
    sql_writes = [sql for _, sql, stdin in parts.server.queries if stdin]
    assert sql_writes and all(
        child_password not in sql for _, sql, stdin in parts.server.queries if not stdin
    )
    if kind == "postgresql":
        assert "TEMPLATE template0" in sql_writes[0]
        assert (
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
            in sql_writes[0]
        )
    else:
        assert "ON `ordinary\\_store`.*" in sql_writes[0]
        assert "ON *.*" not in sql_writes[0]
    for path in parts.services.state.root.rglob("*.json"):
        raw = path.read_text()
        assert ADMIN_PASSWORD not in raw and child_password not in raw
    assert parts.services.provisions["db-" + "2" * 32]["state"] == "complete"


def test_mysql_partial_revokes_uses_exact_literal_database_name(tmp_path):
    parts = fixture(tmp_path, "mysql")
    parts.server.partial_revokes = "1"
    parts.services.provision(prepared(parts, "normal_db with space"), "db-" + "3" * 32)
    sql = next(sql for _, sql, stdin in parts.server.queries if stdin)
    assert "ON `normal_db with space`.*" in sql
    assert "normal\\_db" not in sql


@pytest.mark.parametrize("kind", ["postgresql", "mysql"])
def test_prepare_new_target_refuses_existing_database_and_unsafe_existing_objects(tmp_path, kind):
    parts = fixture(tmp_path, kind)
    with pytest.raises(SourceError, match="DATABASE_ALREADY_EXISTS"):
        parts.services.provision(prepared(parts, "existing"), "db-" + "4" * 32)
    assert not parts.services.provisions
    parts.server.unsafe = True
    with pytest.raises(SourceError, match="DATABASE_EXISTING_SCOPE_UNSAFE"):
        parts.services.provision(prepared(parts, "existing"), "db-" + "4" * 32, existing=True)
    assert not parts.services.provisions


def test_pending_provision_journal_never_retries_or_cleans_up_role(tmp_path):
    parts = fixture(tmp_path)
    parts.services.writer = lambda *_: (_ for _ in ()).throw(SourceError("KEYCHAIN_FAILED"))
    identifier = "db-" + "5" * 32
    with pytest.raises(SourceError, match="KEYCHAIN_FAILED"):
        parts.services.provision(prepared(parts), identifier)
    assert parts.services.provisions[identifier]["state"] == "pending"
    with pytest.raises(SourceError, match="DATABASE_PROVISIONING_RECOVERY_REQUIRED"):
        parts.services.provision(prepared(parts), identifier)
    assert not any("DROP " in sql for _, sql, _ in parts.server.queries)


def test_completed_postgres_provision_rejects_recreated_database_even_after_restart(tmp_path):
    parts = fixture(tmp_path)
    profile, identifier = prepared(parts), "db-" + "6" * 32
    parts.services.provision(profile, identifier)
    restarted = DatabaseServices(
        parts.services.state.root,
        secret_reader=parts.reader,
        secret_writer=parts.writer,
        query=parts.server.query,
        authorization_key="b" * 64,
    )
    assert restarted.provision(profile, identifier)["user"] == profile["user"]
    parts.server.databases[profile["database"]] = "43"
    with pytest.raises(SourceError, match="DATABASE_INSTANCE_CHANGED"):
        restarted.provision(profile, identifier)


@pytest.mark.parametrize("kind", ["postgresql", "mysql"])
def test_prepare_then_original_config_write_approval_overrides_only_with_child_secret(
    tmp_path, monkeypatch, kind
):
    parts = fixture(tmp_path, kind)
    root = tmp_path / "empty-project"
    root.mkdir()
    source = SourceAccess(root)
    database = DatabaseProfiles(
        tmp_path / "profiles",
        lambda _: source,
        secret_reader=parts.reader,
        secret_writer=parts.writer,
    )
    monkeypatch.setattr(database, "_query", parts.server.query)
    database.services.query = parts.server.query
    connected = database.connect_service(
        "p",
        kind=kind,
        host="127.0.0.1",
        port=parts.service["port"],
        user=parts.service["user"],
        credential_ref=REFERENCE,
        tls=False,
    )
    result = database.prepare("p", connected["service_id"], "ordinary_store")
    identifier = result["database_target_id"]
    assert not result["authorized"] and not list(root.iterdir())
    retry = database.prepare("p", connected["service_id"], "ordinary_store")
    assert retry["database_target_id"] == identifier
    (root / "application.properties").write_text(result["configuration_template"]["content"])
    assert database.status("p")["database_target_id"] == identifier
    database.authorize_target("p", identifier)

    class Proxy:
        def __init__(self, host, port, name, user, **kwargs):
            self.host, self.port, self.closed = "127.0.0.1", 33333, False

        def start(self):
            return self

        def close(self):
            self.closed = True

    import code_context.mysql_target_proxy as mysql
    import code_context.postgres_target_proxy as pg

    monkeypatch.setattr(pg, "PostgresTargetProxy", Proxy)
    monkeypatch.setattr(mysql, "MySQLTargetProxy", Proxy)
    config = database.job_configuration("p", identifier, require_authorized=True)
    assert config["user"].startswith("colink_")
    assert config["target_enforced"] and config["port"] == 33333
    assert ADMIN_PASSWORD not in json.dumps(config["env"])
    assert "33333" in config["env"]["SPRING_DATASOURCE_URL"]
    assert config["env"]["SPRING_DATASOURCE_PASSWORD"] == config["env"]["COLINK_DB_PASSWORD"]
    database.release_configuration(config)
    assert config["proxy"].closed and not database.proxies
    assert parts.service["port"] in database.raw_service_ports()
    restarted = DatabaseProfiles(
        tmp_path / "profiles",
        lambda _: source,
        secret_reader=parts.reader,
        secret_writer=parts.writer,
    )
    assert restarted.status("p")["authorized"]
    assert restarted.profile("p")["user"].startswith("colink_")
    parts.secrets[REFERENCE] = "fixture-rotated"
    with pytest.raises(SourceError, match="DATABASE_SERVICE_CREDENTIAL_CHANGED"):
        restarted.status("p")
    restarted.close()
    database.close()

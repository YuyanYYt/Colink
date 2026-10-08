"""Static configuration and persistent target grants; all credentials are fixtures."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

import code_context.execution_database as database_module
from code_context.database_discovery import discover
from code_context.execution_database import DatabaseProfiles
from code_context.execution_tools import register_execution_tools
from code_context.source_access import SourceAccess, SourceError

SECRET = "fixture-secret-no-real-credentials"


def project(tmp_path, files):
    root = tmp_path / "project"
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    source = SourceAccess(root)
    return (
        root,
        source,
        DatabaseProfiles(tmp_path / "profiles", lambda _: source, scope_enforcement=False),
    )


def fixture_env(database="store", user="postgres", password=SECRET):
    return f"DATABASE_URL=postgresql://{user}:{password}@127.0.0.1:15432/{database}\n"


@pytest.mark.parametrize(
    "files,expected",
    [
        ({".env": fixture_env()}, ("postgresql", "store", "postgres", 15432)),
        (
            {
                ".env": (
                    "DB_HOST=localhost\nDB_PORT=3306\nDB_NAME=existing_db\n"
                    + "DB_USER=root\nDB_PASSWORD="
                    + SECRET
                )
            },
            ("mysql", "existing_db", "root", 3306),
        ),
        (
            {
                "backend/src/main/resources/application.yml": (
                    "spring:\n"
                    "  datasource:\n"
                    "    url: jdbc:postgresql://localhost:15432/store\n"
                    "    username: postgres\n"
                    "    password: ${DB_PASSWORD}\n"
                ),
                ".env": "DB_PASSWORD=" + SECRET,
            },
            ("postgresql", "store", "postgres", 15432),
        ),
        (
            {
                "application.properties": (
                    "spring.datasource.url=jdbc:mysql://localhost:13306/store\n"
                    "spring.datasource.username=root\n"
                    "spring.datasource.password="
                )
                + SECRET
            },
            ("mysql", "store", "root", 13306),
        ),
        (
            {
                "prisma/schema.prisma": (
                    'datasource db {\n provider = "postgresql"\n'
                    + ' url = env("DATABASE_URL")\n}\n'
                ),
                ".env": fixture_env(),
            },
            ("postgresql", "store", "postgres", 15432),
        ),
        (
            {
                "compose.yaml": (
                    "services:\n"
                    "  db:\n"
                    "    image: postgres:18\n"
                    "    environment:\n"
                    "      POSTGRES_DB: store\n"
                    "      POSTGRES_USER: postgres\n"
                    "      POSTGRES_PASSWORD: "
                )
                + SECRET
                + '\n    ports:\n      - "127.0.0.1:15432:5432"\n'
            },
            ("postgresql", "store", "postgres", 15432),
        ),
        (
            {
                "docker-compose.yml": (
                    "services:\n"
                    "  db:\n"
                    "    image: mysql:8.4\n"
                    "    environment:\n"
                    "      - MYSQL_DATABASE=store\n"
                    "      - MYSQL_ROOT_PASSWORD="
                )
                + SECRET
                + '\n    ports:\n      - "13306:3306"\n'
            },
            ("mysql", "store", "root", 13306),
        ),
    ],
)
def test_static_formats_never_return_or_persist_password(tmp_path, files, expected):
    _, source, database = project(tmp_path, files)
    candidates, unresolved, digest = discover(source)
    assert not unresolved and len(digest) == 64
    assert tuple(candidates[0][key] for key in ("kind", "database", "user", "port")) == expected
    status = database.status("p")
    assert status["configured"] and not status["authorized"]
    assert SECRET not in json.dumps(status)
    assert not (database.state.root / "profiles.json").exists()
    for path in database.grant_state.root.rglob("*.json"):
        assert SECRET not in path.read_text()
    if ".env" in files:
        with pytest.raises(SourceError):
            source.read(".env")


def test_multi_environment_and_multi_document_spring_require_explicit_selection(tmp_path):
    files = {
        "application.yml": (
            "spring:\n"
            "  datasource:\n"
            "    url: jdbc:postgresql://localhost:15432/dev\n"
            "    username: postgres\n"
            "---\n"
            "spring:\n"
            "  datasource:\n"
            "    url: jdbc:postgresql://localhost:15432/test\n"
            "    username: postgres\n"
        ),
    }
    root, _, database = project(tmp_path, files)
    status = database.status("p")
    assert status["selection_required"] and not status["configured"]
    assert {c["database"] for c in status["candidates"]} == {"dev", "test"}
    identifier = status["candidates"][0]["database_target_id"]
    assert database.select("p", identifier)["database_target_id"] == identifier
    assert not database.status("p")["authorized"]
    (root / "application.yml").write_text(files["application.yml"] + "# changed\n")
    assert database.status("p")["selection_required"]


def test_missing_variable_cannot_inherit_controller_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", fixture_env().removeprefix("DATABASE_URL=").strip())
    _, _, database = project(tmp_path, {"schema.prisma": 'url = env("DATABASE_URL")'})
    status = database.status("p")
    assert not status["configured"] and status["unresolved_configs"] == ["schema.prisma"]


def test_same_target_different_credentials_stops_for_local_correction(tmp_path):
    _, _, database = project(
        tmp_path, {".env": fixture_env(), ".env.test": fixture_env(password="different-fixture")}
    )
    status = database.status("p")
    assert status["selection_required"] and status["candidates"][0]["credential_conflict"]
    with pytest.raises(SourceError, match="DATABASE_TARGET_UNAVAILABLE"):
        database.select("p", status["candidates"][0]["database_target_id"])


def test_symlinks_and_hardlink_secret_config_cannot_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text(fixture_env())
    root, source, database = project(tmp_path, {})
    (root / ".env").symlink_to(outside)
    assert not database.status("p")["configured"]
    # The symlink remains retained; a different allowlisted file demonstrates hardlink rejection.
    (root / ".env.local").hardlink_to(outside)
    with pytest.raises(SourceError, match="DATABASE_CONFIG_UNSAFE"):
        discover(source)


def test_grant_reused_across_projects_and_restart_but_password_change_requires_approval(tmp_path):
    root, source, database = project(tmp_path, {".env": fixture_env()})
    identifier = database.status("first")["database_target_id"]
    assert not database.status("first")["authorized"]
    database.authorize_target("first", identifier)
    assert database.status("second")["authorized"]
    restarted = DatabaseProfiles(database.state.root, lambda _: source, scope_enforcement=False)
    assert restarted.status("second")["authorized"]
    raw = (database.grant_state.root / "grants.json").read_text()
    assert SECRET not in raw and "authentication_digest" in raw
    (root / ".env").write_text(fixture_env(password="rotated-fixture"))
    assert not restarted.status("first")["authorized"]
    with pytest.raises(SourceError, match="DATABASE_AUTHORIZATION_REQUIRED"):
        restarted.require_target("first", identifier)


@pytest.mark.parametrize("action", ["grant", "revoke", "select"])
def test_failed_persistent_write_never_publishes_unsaved_permission_or_selection(
    tmp_path, monkeypatch, action
):
    _, _, database = project(
        tmp_path, {".env": fixture_env(), ".env.test": fixture_env(database="test")}
    )
    identifier = database.status("p")["candidates"][0]["database_target_id"]
    database.select("p", identifier)
    if action == "revoke":
        database.authorize_target("p", identifier)
    before_grants, before_selections = dict(database.grants), dict(database.selections)

    def fail(*_):
        raise SourceError("CONTROL_STATE_FAILED")

    monkeypatch.setattr(database_module, "write_state", fail)
    with pytest.raises(SourceError, match="CONTROL_STATE_FAILED"):
        if action == "grant":
            database.authorize_target("p", identifier)
        elif action == "revoke":
            database.revoke(identifier)
        else:
            database.select("p", database.status("p")["candidates"][1]["database_target_id"])
    assert database.grants == before_grants and database.selections == before_selections


def test_real_probe_is_target_and_account_checked_and_failure_replaces_previous_success(
    tmp_path, monkeypatch
):
    _, _, database = project(tmp_path, {".env": fixture_env()})
    identifier = database.status("p")["database_target_id"]
    database.authorize_target("p", identifier)
    monkeypatch.setattr(
        database,
        "_query",
        lambda *_: json.dumps(
            {"database": "store", "user": "postgres", "instance_identity": "111"}
        ),
    )
    result = database.check_connection("p")
    assert result["authenticated_connection"] == "verified"
    assert result["authentication_expires_at"] - result["authenticated_at"] == 60
    monkeypatch.setattr(
        database, "_query", lambda *_: json.dumps({"database": "another", "user": "postgres"})
    )
    result = database.check_connection("p")
    assert result["authenticated_connection"] == "failed"
    assert result["connection_error"] == "DATABASE_TARGET_VERIFICATION_FAILED"


def test_pg_recreated_database_identity_revokes_old_grant(tmp_path, monkeypatch):
    _, _, database = project(tmp_path, {".env": fixture_env()})
    identifier = database.status("p")["database_target_id"]
    database.authorize_target("p", identifier)
    identity = ["11"]
    monkeypatch.setattr(
        database,
        "_query",
        lambda *_: json.dumps(
            {"database": "store", "user": "postgres", "instance_identity": identity[0]}
        ),
    )
    assert database.check_connection("p")["authorized"]
    identity[0] = "12"
    result = database.check_connection("p")
    assert not result["authorized"] and result["connection_error"] == "DATABASE_INSTANCE_CHANGED"


def test_missing_existing_database_revokes_approval_but_missing_new_database_can_be_created(
    tmp_path, monkeypatch
):
    _, _, database = project(tmp_path, {".env": fixture_env()})
    identifier = database.status("p")["database_target_id"]
    database.authorize_target("p", identifier)

    def missing(*_):
        raise SourceError("DATABASE_NOT_FOUND")

    monkeypatch.setattr(database, "_query", missing)
    assert database.check_connection("p")["authorized"]
    assert database.validate_execution_target("p", identifier, "create")
    database.mark_existing(identifier)
    assert not database.check_connection("p")["authorized"]
    with pytest.raises(SourceError, match="DATABASE_AUTHORIZATION_REQUIRED"):
        database.validate_execution_target("p", identifier, "create")


@pytest.mark.parametrize(
    "action,table,limit",
    [("execute", "", 1), ("preview", "x;DROP", 1), ("preview", "x", 101), ("preview", "x", True)],
)
def test_read_tool_cannot_accept_sql_or_expand_budget(tmp_path, action, table, limit):
    _, _, database = project(tmp_path, {".env": fixture_env()})
    with pytest.raises(SourceError, match="INVALID_DATABASE_READ"):
        database.read(
            "p", database.status("p")["database_target_id"], action, table=table, limit=limit
        )


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("password authentication failed fixture-secret", "DATABASE_AUTHENTICATION_FAILED"),
        ("database x does not exist fixture-secret", "DATABASE_NOT_FOUND"),
        ("permission denied fixture-secret", "DATABASE_PERMISSION_DENIED"),
        ("SSL certificate fixture-secret", "DATABASE_TLS_FAILED"),
        ("connection refused fixture-secret", "DATABASE_SERVICE_UNAVAILABLE"),
    ],
)
def test_bounded_client_errors_are_classified_without_echoing_credentials(reason, expected):
    code = "import sys;sys.stderr.write(" + repr(reason) + ");sys.exit(1)"
    with pytest.raises(SourceError) as error:
        DatabaseProfiles._bounded_client([sys.executable, "-S", "-c", code], {})
    assert str(error.value) == expected


def test_bounded_client_limits_large_and_slow_output():
    with pytest.raises(SourceError, match="DATABASE_RESPONSE_LIMIT"):
        DatabaseProfiles._bounded_client([sys.executable, "-S", "-c", "print('x'*70000)"], {})
    with pytest.raises(SourceError, match="DATABASE_QUERY_TIMEOUT"):
        DatabaseProfiles._bounded_client(
            [sys.executable, "-S", "-c", "import time;time.sleep(1)"], {}, timeout=0.05
        )


def test_terminal_database_tools_replace_connections_without_model_grants(tmp_path):
    _, _, database = project(tmp_path, {".env": fixture_env()})
    mcp = MCPServer("fixture")
    register_execution_tools(mcp, SimpleNamespace(databases=database), lambda _: None)
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "terminal_start",
        "terminal_input",
        "terminal_read_targets",
        "terminal_read",
    } <= names
    assert (
        not {
            "database_status",
            "database_read",
            "select_database",
            "database_environment",
            "database_prepare",
            "authorize_database",
            "grant_database",
        }
        & names
    )
    for name in ("terminal_read_targets", "terminal_read"):
        read_tool = next(tool for tool in tools if tool.name == name)
        assert read_tool.annotations.read_only_hint is True
        assert read_tool.annotations.destructive_hint is False
        assert "ui" not in (read_tool.meta or {})
    for name in ("terminal_start", "terminal_input"):
        command_tool = next(tool for tool in tools if tool.name == name)
        assert command_tool.annotations.read_only_hint is False
        assert command_tool.annotations.destructive_hint is True
    targets = next(tool for tool in tools if tool.name == "terminal_read_targets")
    assert set(targets.input_schema["properties"]) == {"project_id"}
    assert asyncio.run(mcp.list_resources()) == []
    assert not database.status("p")["authorized"]


def test_fixed_read_requires_grant_and_uses_read_only_transaction_and_limits(tmp_path, monkeypatch):
    _, _, database = project(tmp_path, {".env": fixture_env()})
    identifier = database.status("p")["database_target_id"]
    with pytest.raises(SourceError, match="DATABASE_AUTHORIZATION_REQUIRED"):
        database.read("p", identifier, "preview", table="items")
    database.authorize_target("p", identifier)
    queries = []

    def query(configuration, sql):
        queries.append(sql)
        if "instance_identity" in sql:
            return json.dumps({"database": "store", "user": "postgres", "instance_identity": "42"})
        if "SELECT count(*)" in sql:
            return "1"
        return '[{"id":1}]'

    monkeypatch.setattr(database, "_query", query)
    result = database.read("p", identifier, "preview", table="items", limit=3)
    assert result["data"] == [{"id": 1}]
    actual = next(sql for sql in queries if "BEGIN READ ONLY" in sql)
    assert '"public"."items" LIMIT 3' in actual and "ROLLBACK" in actual
    assert result["max_rows"] == 3
    assert SECRET not in json.dumps(result)
    monkeypatch.setattr(
        database, "_query", lambda _, sql: query(_, sql) if "instance_identity" in sql else "0"
    )
    with pytest.raises(SourceError, match="DATABASE_BASE_TABLE_REQUIRED"):
        database.read("p", identifier, "preview", table="a_view")


@pytest.mark.parametrize(
    "content",
    [
        "spring.datasource.password=fixture-plain-password\n",
        "spring:\n  datasource:\n    password: fixture-plain-password\n",
        "environment:\n  - MYSQL_ROOT_PASSWORD=fixture-plain-password\n",
        "DATABASE_URL=postgresql://postgres:fixture-plain-password@localhost:5432/store\n",
        "password: ${DB_PASSWORD:fixture-plain-password}\n",
    ],
)
def test_database_plaintext_credentials_remain_private_to_local_discovery(tmp_path, content):
    root, source, _ = project(tmp_path, {"application.properties": content})
    with pytest.raises(SourceError, match="FILE_EXCLUDED") as error:
        source.read("application.properties")
    assert "fixture-plain-password" not in str(error.value)
    # The private configuration parser still has its own bounded allowlisted read.
    assert (root / "application.properties").read_text() == content


@pytest.mark.parametrize(
    "content",
    [
        "spring.datasource.password=${DB_PASSWORD}\n",
        "password: '${DB_PASSWORD}'\n",
        'password: ""\n',
        "DATABASE_URL=postgresql://postgres:${DB_PASSWORD}@localhost:5432/store\n",
    ],
)
def test_variable_references_and_empty_password_config_are_not_source_secrets(tmp_path, content):
    _, source, _ = project(tmp_path, {"application.properties": content})
    assert source.read("application.properties").content == content


def test_removed_database_connection_card_is_not_packaged_or_advertised():
    from importlib.resources import files

    assert not files("code_context").joinpath("resources/database/connection.html").is_file()
    mcp = MCPServer("fixture")
    register_execution_tools(mcp, SimpleNamespace(), lambda _: None)
    assert asyncio.run(mcp.list_resources()) == []
    assert all("ui" not in (tool.meta or {}) for tool in asyncio.run(mcp.list_tools()))


@pytest.mark.parametrize(
    "body",
    [
        (
            "spring:\n  datasource:\n"
            "    url: jdbc:postgresql://localhost:15432/store\n"
            "    username: postgres\n    password: *password_alias\n"
        ),
        "spring:\n  datasource: *database_alias\n",
        (
            "spring:\n  datasource:\n"
            "    url: jdbc:postgresql://localhost:15432/store\n"
            "    url: jdbc:postgresql://localhost:15432/other\n"
            "    username: postgres\n"
        ),
    ],
)
def test_complex_or_duplicate_yaml_never_silently_chooses_a_target(tmp_path, body):
    _, _, database = project(tmp_path, {"application.yml": body})
    result = database.status("p")
    assert not result["configured"]
    assert result["unresolved_configs"] == ["application.yml"]


def test_native_administration_uses_fixed_exact_target_and_maintenance_connection(tmp_path):
    _, _, database = project(tmp_path, {".env": fixture_env(database="store with space")})
    identifier = database.status("p")["database_target_id"]
    database.authorize_target("p", identifier)
    argv = database.administrative_argv("p", identifier, "create", "psql")
    assert argv[-1] == 'CREATE DATABASE "store with space"'
    configuration = database.job_configuration(
        "p", identifier, require_authorized=True, database_action="create"
    )
    assert configuration["env"]["PGDATABASE"] == "postgres"
    assert configuration["target_name"] == "store with space"
    assert SECRET not in json.dumps(database.status("p"))
    with pytest.raises(SourceError, match="DATABASE_CLIENT_MISMATCH"):
        database.administrative_argv("p", identifier, "drop", "mysql")


def test_revocation_callback_runs_only_after_permission_was_saved(tmp_path, monkeypatch):
    _, _, database = project(tmp_path, {".env": fixture_env()})
    identifier = database.status("p")["database_target_id"]
    database.authorize_target("p", identifier)
    events = []
    database.on_revoke = lambda target: events.append(target) or [{"state": "stop_pending"}]
    actual = database_module.write_state

    def fail(*_):
        raise SourceError("CONTROL_STATE_FAILED")

    monkeypatch.setattr(database_module, "write_state", fail)
    with pytest.raises(SourceError):
        database.revoke(identifier)
    assert not events and database.status("p")["authorized"]
    monkeypatch.setattr(database_module, "write_state", actual)
    result = database.revoke(identifier)
    assert result["jobs"] == [{"state": "stop_pending"}] and events == [identifier]

"""Project DB metadata and secret handling, without real credentials or servers."""

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest

import code_context.execution_database as implementation
from code_context.execution_database import DatabaseProfiles
from code_context.execution_sandbox import NativeSandbox
from code_context.source_access import SourceAccess, SourceError

REFERENCE = "colink-db-" + "1" * 32
FAKE_PASSWORD = "fixture-only:+%/@ secret"


class Connected:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("fixture\n")
    sources = {"project": SourceAccess(root)}
    seen = []

    def secret(reference):
        seen.append(reference)
        return FAKE_PASSWORD

    monkeypatch.setattr(implementation.shutil, "which", lambda name: "/fixture/" + name)
    monkeypatch.setattr(
        implementation.socket, "create_connection", lambda *args, **kwargs: Connected()
    )
    database = DatabaseProfiles(
        tmp_path / "database-state",
        sources.__getitem__,
        secret_reader=secret,
        scope_enforcement=False,
    )
    return SimpleNamespace(database=database, sources=sources, seen=seen, root=root, tmp=tmp_path)


def configure(parts, **kwargs):
    values = {
        "kind": "postgresql",
        "host": "127.0.0.1",
        "port": 15432,
        "database": "colink_fixture",
        "user": "fixture_role",
        "credential_ref": REFERENCE,
        "tls": True,
    }
    values.update(kwargs)
    return parts.database.configure("project", **values)


def test_status_is_readonly_and_never_requests_password_or_claims_authenticated(profiles):
    empty = profiles.database.status("project")
    assert not empty["configured"] and empty["existing_mysql_supported"]
    status = configure(profiles)
    assert status["configured"] and status["tcp_ready"]
    assert not profiles.seen
    assert "credential_ref" not in status and "user" not in status and "source_id" not in status
    for check in ("authenticated_connection", "migrations", "crud", "vector_extension"):
        assert status[check] == "not_verified"
    assert FAKE_PASSWORD not in json.dumps(status)


def test_profile_file_is_private_and_contains_only_keychain_reference(profiles):
    configure(profiles)
    profiles.database.job_configuration("project")
    path = profiles.database.state.root / "profiles.json"
    raw = path.read_text()
    assert REFERENCE in raw and FAKE_PASSWORD not in raw
    assert quote(FAKE_PASSWORD, safe="") not in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert profiles.seen == [REFERENCE]


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "example.invalid"),
        ("host", "127.0.0.1; untrusted"),
        ("port", 543),
        ("port", True),
        ("port", 65536),
        ("database", "colink_fixture; SELECT"),
        ("user", "role; untrusted"),
        ("credential_ref", FAKE_PASSWORD),
        ("tls", "true"),
        ("kind", "arbitrary-driver"),
    ],
)
def test_scope_rejections_do_not_echo_values_or_persist_profile(profiles, field, value):
    with pytest.raises(SourceError, match="INVALID_DATABASE_PROFILE") as error:
        configure(profiles, **{field: value})
    assert str(value) not in str(error.value)
    assert not profiles.seen and not profiles.database.profiles
    assert not (profiles.database.state.root / "profiles.json").exists()


@pytest.mark.parametrize("kind", ["postgresql", "pgvector", "mysql", "qdrant"])
def test_job_credentials_are_explicit_with_complete_redaction_values(profiles, kind):
    configure(profiles, kind=kind, host="::1", tls=False)
    config = profiles.database.job_configuration("project")
    env = config["env"]
    url = urlsplit(env["DATABASE_URL"])
    assert url.hostname == "::1" and url.port == 15432
    assert url.username == "fixture_role"
    assert url.password == quote(FAKE_PASSWORD, safe="")
    assert url.path == "/colink_fixture"
    assert config["port"] == 15432
    assert set(config["redactions"]) == {
        FAKE_PASSWORD,
        quote(FAKE_PASSWORD, safe=""),
        env["DATABASE_URL"],
    }
    if kind in {"postgresql", "pgvector"}:
        assert env["PGPASSWORD"] == FAKE_PASSWORD and env["PGSSLMODE"] == "disable"
        assert env["PGSERVICEFILE"] == "/dev/null"
    elif kind == "mysql":
        assert env["MYSQL_PWD"] == FAKE_PASSWORD
    else:
        assert env["QDRANT_API_KEY"] == FAKE_PASSWORD
        assert env["QDRANT_URL"] == "http://[::1]:15432"


@pytest.mark.parametrize("tls,expected", [(True, "verify-full"), (False, "disable")])
def test_postgres_url_honors_tls_for_drivers_that_only_read_database_url(profiles, tls, expected):
    configure(profiles, tls=tls)
    env = profiles.database.job_configuration("project")["env"]
    assert parse_qs(urlsplit(env["DATABASE_URL"]).query).get("sslmode") == [expected]


@pytest.mark.parametrize("password", ["has\x00nul", "x" * 4097, None, 123])
def test_invalid_keychain_secret_is_content_free(profiles, password):
    configure(profiles)
    profiles.database.secret_reader = lambda _: password
    with pytest.raises(SourceError, match="INVALID_DATABASE_CREDENTIAL") as error:
        profiles.database.job_configuration("project")
    assert "has" not in str(error.value)
    assert str(error.value) == "INVALID_DATABASE_CREDENTIAL"


def test_profile_identity_change_requires_local_reconfiguration(profiles):
    configure(profiles)
    replacement = profiles.tmp / "replacement"
    replacement.mkdir()
    profiles.sources["project"] = SourceAccess(replacement)
    with pytest.raises(SourceError, match="DATABASE_SOURCE_CHANGED"):
        profiles.database.status("project")
    with pytest.raises(SourceError, match="DATABASE_SOURCE_CHANGED"):
        profiles.database.job_configuration("project")
    assert not profiles.seen


def test_restart_retains_metadata_without_caching_secret(profiles):
    configure(profiles)
    original = profiles.database.job_configuration("project")
    next_password = "different-fixture-secret"
    restarted = DatabaseProfiles(
        profiles.database.state.root,
        profiles.sources.__getitem__,
        secret_reader=lambda _: next_password,
        scope_enforcement=False,
    )
    assert restarted.status("project")["configured"]
    fresh = restarted.job_configuration("project")
    assert fresh["profile_digest"] == original["profile_digest"]
    assert fresh["env"]["PGPASSWORD"] == next_password
    assert FAKE_PASSWORD not in json.dumps(fresh)


def test_unconfigured_project_cannot_inject_database_env(profiles):
    with pytest.raises(SourceError, match="DATABASE_NOT_CONFIGURED"):
        profiles.database.job_configuration("project")
    assert not profiles.seen


def test_native_job_config_does_not_persist_keychain_password(profiles, monkeypatch):
    configure(profiles)
    config = profiles.database.job_configuration("project")
    disk_root = profiles.tmp / "fixture-disk"
    disk_root.mkdir(mode=0o700)
    workspace = disk_root / "workspace"
    workspace.mkdir()
    disk = SimpleNamespace(
        mount=disk_root, image=profiles.tmp / "disk.fixture", verify=lambda: None
    )
    sandbox = NativeSandbox(profiles.tmp / "sandbox-state", [])
    sandbox.tools = {"node": sys.executable, "python3": sys.executable}
    monkeypatch.setattr(sandbox, "available", lambda: True)
    sandbox.prepare("job-fixture", disk, workspace, [sys.executable, "a.py"], database=config)
    persisted = (sandbox.state.root / "job-fixture.json").read_text()
    assert FAKE_PASSWORD not in persisted
    assert quote(FAKE_PASSWORD, safe="") not in persisted
    assert config["env"]["DATABASE_URL"] not in persisted


def proof_for(parts, **changes):
    """Synthetic trusted-helper result; never a claim about a live DB."""
    proof = {
        "kind": "postgresql",
        "authenticated": True,
        "password_enforced": True,
        "role_check": "passed",
        "vector_extension": "not_verified",
    }
    proof.update(changes)
    return proof


def record_proof(parts, proof=None, *, job_id="job-" + "f" * 32):
    profile = parts.database.profile("project")
    return parts.database.record_proof(
        "project", parts.database.fingerprint(profile), job_id, proof or proof_for(parts)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "mysql"},
        {"authenticated": False},
        {"authenticated": "true"},
        {"password_enforced": False},
        {"password_enforced": "true"},
        {"role_check": "superuser"},
    ],
)
def test_invalid_synthetic_helper_proof_does_not_mark_database_verified(profiles, change):
    configure(profiles)
    with pytest.raises(SourceError, match="DATABASE_PROOF_INVALID"):
        record_proof(profiles, proof_for(profiles, **change))
    assert profiles.database.status("project")["authenticated_connection"] == "not_verified"
    assert profiles.database.verifications == {}
    assert not profiles.seen


@pytest.mark.parametrize("proof", [None, [], "fixture-secret-proof"])
def test_malformed_helper_proof_is_content_free_error(profiles, proof):
    configure(profiles)
    fingerprint = profiles.database.fingerprint(profiles.database.profile("project"))
    with pytest.raises(SourceError, match="DATABASE_PROOF_INVALID") as error:
        profiles.database.record_proof("project", fingerprint, "job-" + "a" * 32, proof)
    assert "fixture-secret" not in str(error.value)


def test_synthetic_proof_expires_after_twenty_four_hours_and_future_time_is_invalid(
    profiles, monkeypatch
):
    now = [100_000.0]
    monkeypatch.setattr(implementation.time, "time", lambda: now[0])
    configure(profiles)
    record_proof(profiles)
    assert profiles.database.status("project")["job_authenticated_connection"] == "verified_for_job"
    now[0] += 86400
    assert profiles.database.status("project")["authenticated_connection"] == "not_verified"
    now[0] = 100_000.0
    profiles.database.verifications["project"]["verified_at"] = now[0] + 60
    assert profiles.database.status("project")["authenticated_connection"] == "not_verified"
    assert not profiles.seen


def test_replacing_local_profile_invalidates_old_job_proof(profiles):
    configure(profiles)
    fingerprint = profiles.database.fingerprint(profiles.database.profile("project"))
    record_proof(profiles)
    configure(profiles, port=15433)
    profiles.database.record_proof("project", fingerprint, "job-" + "b" * 32, proof_for(profiles))
    assert profiles.database.status("project")["authenticated_connection"] == "not_verified"
    assert profiles.database.verifications == {}


@pytest.mark.parametrize("field", ["vector_extension", "job_id"])
def test_proof_metadata_cannot_persist_or_echo_arbitrary_secret_fields(profiles, field):
    configure(profiles)
    with pytest.raises(SourceError, match="DATABASE_PROOF_INVALID") as error:
        if field == "job_id":
            record_proof(profiles, job_id=FAKE_PASSWORD)
        else:
            record_proof(profiles, proof_for(profiles, vector_extension=FAKE_PASSWORD))
    assert FAKE_PASSWORD not in str(error.value)
    assert FAKE_PASSWORD not in (profiles.database.state.root / "profiles.json").read_text()


def test_corrupt_stored_verification_never_becomes_authenticated_or_leaks_secret(profiles):
    configure(profiles)
    profiles.database.verifications["project"] = {
        "profile_fingerprint": profiles.database.fingerprint(profiles.database.profile("project")),
        "verified_at": "fixture-secret-corrupt",
        "authenticated_connection": "verified_for_job",
    }
    try:
        status = profiles.database.status("project")
    except SourceError as error:
        assert "fixture-secret" not in str(error)
    else:
        assert status["authenticated_connection"] == "not_verified"
        assert "fixture-secret" not in json.dumps(status)


def test_failed_connection_save_preserves_previous_profile_and_proof(profiles, monkeypatch):
    configure(profiles)
    record_proof(profiles)
    before = (profiles.database.state.root / "profiles.json").read_bytes()

    def refuse(*args):
        raise SourceError("CONTROL_STATE_FAILED")

    monkeypatch.setattr(implementation, "write_state", refuse)
    with pytest.raises(SourceError, match="CONTROL_STATE_FAILED"):
        configure(profiles, port=15433)
    status = profiles.database.status("project")
    assert status["port"] == 15432
    assert status["job_authenticated_connection"] == "verified_for_job"
    assert (profiles.database.state.root / "profiles.json").read_bytes() == before


def test_failed_proof_save_does_not_report_unsaved_authentication(profiles, monkeypatch):
    configure(profiles)
    before = (profiles.database.state.root / "profiles.json").read_bytes()

    def refuse(*args):
        raise SourceError("CONTROL_STATE_FAILED")

    monkeypatch.setattr(implementation, "write_state", refuse)
    with pytest.raises(SourceError, match="CONTROL_STATE_FAILED"):
        record_proof(profiles)
    assert profiles.database.status("project")["authenticated_connection"] == "not_verified"
    assert not profiles.database.verifications
    assert (profiles.database.state.root / "profiles.json").read_bytes() == before


def run_database_adapter(payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the actual database adapter")
    adapter = Path(implementation.__file__).parent / "resources/sandbox/database-probe.mjs"
    script = """
        import {readFileSync} from 'node:fs';
        const adapter = await import(process.argv[1]);
        const p = JSON.parse(readFileSync(0, 'utf8'));
        try {
          const result = p.operation === 'grants'
            ? adapter.verifyMySqlGrants(p.text, p.expected)
            : adapter.mysqlProjectArgs(p.config, p.env, p.args);
          process.stdout.write(JSON.stringify({accepted: true, result}));
        } catch {process.stdout.write(JSON.stringify({accepted: false}));}
    """
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, adapter.as_uri()],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "unexpected", [None, {"PATH": "/fixture"}, {"SPRING_DATASOURCE_PASSWORD": 42}]
)
def test_actual_native_helper_accepts_private_spring_payload_and_rejects_unapproved_env(unexpected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the actual native helper")
    helper = Path(implementation.__file__).parent / "resources/sandbox/helper.mjs"
    payload = {
        "env": {
            "COLINK_DATABASE_NAME": "ordinary_store",
            "COLINK_DATABASE_KIND": "postgresql",
            "COLINK_DB_PASSWORD": FAKE_PASSWORD,
            "SPRING_DATASOURCE_URL": "jdbc:postgresql://127.0.0.1:33333/ordinary_store?sslmode=disable",
            "SPRING_DATASOURCE_USERNAME": "colink_child",
            "SPRING_DATASOURCE_PASSWORD": FAKE_PASSWORD,
        },
        "redactions": [FAKE_PASSWORD],
    }
    if unexpected is not None:
        payload["env"].update(unexpected)
    script = """
      import {readFileSync} from 'node:fs';
      const helper = await import(process.argv[1]);
      try {helper.validateDatabasePayload(JSON.parse(readFileSync(0,'utf8')));
        process.stdout.write('accepted');}
      catch {process.stdout.write('rejected');}
    """
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, helper.as_uri()],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert result.stdout == ("accepted" if unexpected is None else "rejected")
    assert FAKE_PASSWORD not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "name,mode,qualified,accepted",
    [
        ("store", "0", "store", True),
        ("normal_db", "0", r"normal\_db", True),
        ("normal_db", "0", "normal_db", False),
        ("normal_db", "1", "normal_db", True),
        ("my store", "0", "my store", True),
        ("my-store", "1", "another", False),
    ],
)
def test_actual_mysql_grants_accept_normal_database_names_with_exact_scope(
    name, mode, qualified, accepted
):
    result = run_database_adapter(
        {
            "operation": "grants",
            "expected": name,
            "text": mode + "\nGRANT SELECT,INSERT ON `" + qualified + "`.* TO `role`@`127.0.0.1`",
        }
    )
    assert result["accepted"] is accepted


@pytest.mark.parametrize(
    "mode,grant,accepted",
    [
        ("0", r"GRANT SELECT ON `colink\_fixture`.* TO `role`@`127.0.0.1`", True),
        ("0", "GRANT SELECT ON `colink_fixture`.* TO `role`@`127.0.0.1`", False),
        ("1", "GRANT SELECT ON `colink_fixture`.* TO `role`@`127.0.0.1`", True),
        ("1", r"GRANT SELECT ON `colink\_fixture`.* TO `role`@`127.0.0.1`", False),
        ("0", "GRANT SELECT ON `colink_fixture`.`records` TO `role`@`127.0.0.1`", True),
        ("0", "GRANT EXECUTE ON PROCEDURE `colink_fixture`.`f` TO `role`@`127.0.0.1`", True),
        ("0", r"GRANT SELECT ON `colink\_%`.* TO `role`@`127.0.0.1`", False),
        ("0", "GRANT SELECT ON *.* TO `role`@`127.0.0.1`", False),
        ("1", "GRANT CREATE USER ON *.* TO `role`@`127.0.0.1`", False),
        ("1", "GRANT `other`@`localhost` TO `role`@`127.0.0.1`", False),
        ("1", "GRANT USAGE ON *.* TO `role`@`127.0.0.1` WITH GRANT OPTION", False),
        ("1", "GRANT SELECT ON `colink_fixture`.* TO `role`@`127.0.0.1` WITH GRANT OPTION", False),
        ("1", "GRANT SELECT ON `colink_other`.* TO `role`@`127.0.0.1`", False),
        ("missing", "GRANT SELECT ON `colink_fixture`.* TO `role`@`127.0.0.1`", False),
    ],
)
def test_actual_mysql_grant_parser_checks_server_wildcard_mode(mode, grant, accepted):
    result = run_database_adapter(
        {"operation": "grants", "text": mode + "\n" + grant, "expected": "colink_fixture"}
    )
    assert result["accepted"] is accepted


@pytest.mark.parametrize(
    "args,accepted",
    [
        (["-e", "SELECT 1", "--batch", "--raw"], True),
        (["--execute=SELECT 1", "--skip-column-names", "--connect-timeout=2"], True),
        (["-e", "-- SQL comment followed by project query"], True),
        (["--host=127.0.0.2"], False),
        (["--database=colink_other"], False),
        (["--pas=fixture-only-secret"], False),
        (["--def=other"], False),
        (["-uroot"], False),
        (["--execute"], False),
        (["colink_other"], False),
    ],
)
def test_actual_mysql_command_preserves_bound_connection_and_cold_auth(args, accepted):
    env = {
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_TCP_PORT": "43123",
        "COLINK_DATABASE_NAME": "colink_fixture",
    }
    config = {
        "databaseClient": "/fixture/mysql",
        "databaseUser": "fixture_role",
        "databaseTLS": False,
    }
    result = run_database_adapter({"operation": "argv", "env": env, "config": config, "args": args})
    assert result["accepted"] is accepted
    if accepted:
        assert result["result"][:4] == [
            "/fixture/mysql",
            "--no-defaults",
            "--no-login-paths",
            "--protocol=TCP",
        ]
        assert "--get-server-public-key" in result["result"]
        assert "--user=fixture_role" in result["result"]
        assert "--database=colink_fixture" in result["result"]
        assert result["result"][-len(args) :] == args

"""Read-only discovery evidence, clean probes and controlled venv specifications."""

import json
import sys
import time
from pathlib import Path

import pytest

from code_context import execution_environment as environment
from code_context.source_access import SourceError


def fake_inventory(monkeypatch, *, present=environment.TOOLS, configured=None, ensurepip=True):
    probes = []
    monkeypatch.setattr(
        environment.shutil, "which", lambda name: f"/installed/{name}" if name in present else None
    )
    monkeypatch.setattr(
        environment, "_trusted", lambda path, _name: (path, None) if path else (None, "not_found")
    )
    monkeypatch.setattr(environment, "_fallback", lambda _name: None)
    monkeypatch.setattr(
        environment,
        "_package_metadata",
        lambda _runtime: {"pgvector": {"installed_metadata": True, "import_verified": False}},
    )

    def probe(argv, clean):
        probes.append((argv, clean))
        if "-c" in argv:
            output = json.dumps(
                {
                    "version": [3, 12, 1],
                    "executable": argv[0],
                    "prefix": "/installed",
                    "base_prefix": "/installed",
                    "purelib": "/installed/lib/site-packages",
                    "platlib": "/installed/lib/site-packages",
                    "venv_supported": True,
                    "ensurepip_supported": ensurepip,
                }
            )
        elif argv[0].endswith("java"):
            output = 'openjdk version "21.0.1"\n'
        else:
            output = "1.2.3\n"
        return {"state": "ok", "exit_code": 0, "output": output}

    monkeypatch.setattr(environment, "_probe", probe)
    return environment.inventory(configured), probes


def test_fixed_versions_do_not_inherit_secrets_or_read_user_defaults(monkeypatch):
    monkeypatch.setenv("EXAMPLE_PRIVATE_VALUE", "synthetic-test-only")
    result, probes = fake_inventory(monkeypatch)
    assert result["tools"]["java"]["version"] == "21.0.1"
    assert result["python"]["venv_supported"]
    assert len(probes) == len(environment.TOOLS)
    for argv, clean in probes:
        assert "EXAMPLE_PRIVATE_VALUE" not in clean
        assert clean["HOME"] == "/var/empty" and clean["MAVEN_SKIP_RC"] == "1"
        assert clean["NPM_CONFIG_USERCONFIG"] == "/dev/null"
        assert clean["NPM_CONFIG_GLOBALCONFIG"] != "/dev/null"
        assert "shell" not in argv and "--eval" not in argv
        if Path(argv[0]).name in {"mysql", "mysqld"}:
            assert argv[1:] == ["--no-defaults", "--version"]
        if Path(argv[0]).name == "python3":
            assert argv[1:5] == ["-I", "-B", "-S", "-c"]
    assert not result["databases"]["connection_verified"]
    assert not result["vector"]["extension_verified"]


def test_inventory_does_not_claim_binary_or_package_as_database_proof(monkeypatch):
    result, _ = fake_inventory(monkeypatch)
    assert result["databases"]["mysql_server_binary_available"]
    assert result["databases"]["postgresql_client_available"]
    assert not result["databases"]["crud_verified"]
    assert not result["databases"]["migration_verified"]
    assert result["python"]["modules"]["pgvector"]["installed_metadata"]
    assert not result["vector"]["extension_verified"] and not result["vector"]["embedding_verified"]


def test_missing_tools_and_binding_mismatch_are_explicit(monkeypatch):
    result, probes = fake_inventory(
        monkeypatch, present={"python3", "mysql"}, configured={"python3": "/installed/python3"}
    )
    assert not result["tools"]["node"]["available"]
    assert result["tools"]["node"]["probe_state"] == "not_found"
    assert {Path(argv[0]).name for argv, _ in probes} == {"python3", "mysql"}
    assert any(item.get("tool") == "mysql" for item in result["runtime_mismatches"])


@pytest.mark.parametrize("path,name", [("/usr/bin/java", "java"), ("/usr/bin/python3", "python3")])
def test_apple_developer_launchers_never_run(path, name, monkeypatch):
    monkeypatch.setattr(environment, "_probe", lambda *_args: pytest.fail("launcher executed"))
    assert environment._trusted(path, name) == (None, "developer_launcher_skipped")


def test_project_runtime_script_never_runs(tmp_path):
    candidate = tmp_path / "python3"
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(0o700)
    assert environment._trusted(str(candidate), "python3")[0] is None


def test_native_runtime_wrapper_is_skipped_even_in_trusted_location(tmp_path, monkeypatch):
    candidate = tmp_path / "python3"
    candidate.write_text("#!/bin/sh\nexit 0\n")
    candidate.chmod(0o700)
    monkeypatch.setattr(environment, "_roots", lambda: (tmp_path,))
    assert environment._trusted(str(candidate), "python3") == (None, "runtime_wrapper_skipped")


def test_venv_plan_is_non_mutating_and_uses_verified_runtime(monkeypatch, tmp_path):
    result, _ = fake_inventory(monkeypatch)
    monkeypatch.chdir(tmp_path)
    plan = environment.venv_plan(result)
    assert plan["command"] == ["python3", "-I", "-B", "-S", "-m", "venv", "{environment}"]
    assert plan["network"] == "none" and plan["operation"] == "generate"
    assert plan["requires_execution_plan"] and plan["requires_rehearsal"]
    assert list(tmp_path.iterdir()) == []


def test_venv_without_ensurepip_reports_missing_pip_bootstrap(monkeypatch):
    result, _ = fake_inventory(monkeypatch, ensurepip=False)
    plan = environment.venv_plan(result)
    assert "--without-pip" in plan["command"]
    assert not plan["pip_bootstrap_available"]


@pytest.mark.parametrize(
    "path", ["/tmp/venv", "../venv", ".", "..", "new/subdir", "new; touch data"]
)
def test_venv_destination_is_one_bounded_relative_name(monkeypatch, path):
    result, _ = fake_inventory(monkeypatch)
    with pytest.raises(SourceError, match="INVALID_VENV_PLAN"):
        environment.venv_plan(result, path)


@pytest.mark.parametrize(
    "configured",
    [
        {"unknown": "/usr/bin/false"},
        {"python3": None},
        {"python3": ""},
        {"python3": "relative"},
        [],
    ],
)
def test_tool_path_configuration_is_exact(configured):
    with pytest.raises(SourceError, match="INVALID_TOOLCHAIN_CONFIGURATION"):
        environment.inventory(configured)


def test_real_probe_output_is_bounded_and_never_exposes_sensitive_output():
    clean = environment._clean_environment({})
    result = environment._probe(
        [sys.executable, "-I", "-B", "-S", "-c", "print('a'*100000)"], clean
    )
    assert result["state"] == "ok" and len(result["output"].encode()) == environment.PROBE_BYTES
    redacted = environment._probe(
        [sys.executable, "-I", "-B", "-S", "-c", "print('sk-proj-'+'x'*24)"], clean
    )
    assert redacted["state"] == "unsafe_output" and redacted["output"] == ""


def test_probe_timeout_stops_own_process_within_bound(monkeypatch):
    monkeypatch.setattr(environment, "PROBE_SECONDS", 0.15)
    started = time.monotonic()
    result = environment._probe(
        [sys.executable, "-I", "-B", "-S", "-c", "import time;time.sleep(30)"],
        environment._clean_environment({}),
    )
    assert result["state"] == "timeout" and time.monotonic() - started < 2
    assert result["exit_code"] is not None


def test_package_metadata_is_not_imported_and_symlinks_are_ignored(tmp_path, monkeypatch):
    packages = tmp_path / "site-packages"
    packages.mkdir()
    marker = tmp_path / "import-would-have-run"
    (packages / "pgvector.py").write_text(f"open({str(marker)!r},'w').write('executed')\n")
    metadata = packages / "pgvector-0.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: pgvector\nVersion: 0.1\n")
    bogus = packages / "redis-1.0.dist-info"
    bogus.symlink_to(metadata, target_is_directory=True)
    monkeypatch.setattr(environment, "_roots", lambda: (tmp_path,))
    result = environment._package_metadata({"purelib": str(packages), "platlib": str(packages)})
    assert result["pgvector"]["installed_metadata"]
    assert not result["pgvector"]["import_verified"] and not marker.exists()
    assert not result["redis"]["installed_metadata"]


def test_failed_version_probe_cannot_claim_available_runtime(monkeypatch):
    result, _ = fake_inventory(monkeypatch)
    monkeypatch.setattr(
        environment,
        "_probe",
        lambda *_args: {"state": "timeout", "exit_code": -9, "output": "1.2.3"},
    )
    timed = environment.inventory()
    assert not timed["tools"]["python3"]["available"]
    assert timed["tools"]["python3"]["probe_state"] == "timeout"
    assert not timed["python"]["venv_supported"]
    with pytest.raises(SourceError, match="PYTHON_VENV_UNAVAILABLE"):
        environment.venv_plan(timed)
    assert result["tools"]["python3"]["available"]


def test_actual_trusted_python_inventory_uses_isolated_standard_library():
    path, reason = environment._trusted(str(Path(sys.executable).resolve()), "python3")
    if path is None:
        pytest.skip("test runtime not inside a trusted installation: " + reason)
    result = environment.inventory({"python3": path})
    assert result["tools"]["python3"]["available"]
    assert result["tools"]["python3"]["version"] == ".".join(map(str, sys.version_info[:3]))
    assert not result["databases"]["connection_verified"]

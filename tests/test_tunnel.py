import json
import os
import shlex
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_context.client import SyncError
from code_context.tunnel import (
    launch_tunnel,
    load_profile,
    prepare_profile,
    read_runtime_key,
    tunnel_status,
)

TUNNEL_ID = "tunnel_" + "0" * 32
FAKE_KEY = "sk-" + "k" * 32


def private(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def profile(tmp_path):
    root = tmp_path.resolve() / "sample source 中文"
    root.mkdir()
    (root / "a.py").write_text("value = 1\n", encoding="utf-8")
    path = tmp_path.resolve() / "tunnel" / "profile.yaml"
    result = prepare_profile(root, "sample", tmp_path / "data", TUNNEL_ID, path)
    assert not result["remote_connection_started"]
    return path


def test_profile_is_private_no_key_and_retains_venv_python(profile):
    config, root, project, data = load_profile(profile)
    assert project == "sample" and data.is_absolute() and root.is_absolute()
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert FAKE_KEY not in profile.read_text()
    assert config["health"]["listen_addr"] == "127.0.0.1:0"
    args = shlex.split(config["mcp"]["commands"][0]["command"])
    assert args[0] == str(Path(sys.executable).absolute())
    assert args[5] == str(root)
    assert not data.exists()


def test_profile_refuses_overwrite(profile):
    _, root, project, data = load_profile(profile)
    before = profile.read_bytes()
    with pytest.raises(SyncError, match="already exists"):
        prepare_profile(root, project, data, TUNNEL_ID, profile)
    assert profile.read_bytes() == before


def test_workspace_profile_is_explicit_and_keeps_exact_scope(profile):
    config, root, _, data = load_profile(profile)
    live = profile.parent / "workspace.yaml"
    prepare_profile(
        root,
        "workspace",
        data.parent / "live",
        config["control_plane"]["tunnel_id"],
        live,
        mode="workspace",
    )
    accepted, accepted_root, project, _ = load_profile(live)
    assert accepted_root == root and project == "workspace"
    assert shlex.split(accepted["mcp"]["commands"][0]["command"])[3] == "workspace"
    assert tunnel_status(live)["source_mode"] == "live"
    assert not tunnel_status(live)["workspace_status"]["write_enabled"]
    assert not (data.parent / "live").exists()
    accepted["mcp"]["commands"][0]["command"] += " --allow-write"
    private(live, json.dumps(accepted))
    with pytest.raises(SyncError, match="expanded-scope"):
        load_profile(live)


def test_profile_requires_official_yaml_extension(profile, tmp_path):
    _, root, project, data = load_profile(profile)
    wrong_path = tmp_path / "profile.json"
    with pytest.raises(SyncError, match=".yaml extension"):
        prepare_profile(root, project, data, TUNNEL_ID, wrong_path)
    assert not wrong_path.exists()
    private(wrong_path, profile.read_text())
    with pytest.raises(SyncError, match=".yaml extension"):
        load_profile(wrong_path)


@pytest.mark.parametrize(
    "mutation", ["foreign_host", "remote_ui", "extra_command", "extra_target", "shell"]
)
def test_profile_rejects_expanded_scope(profile, mutation):
    config = json.loads(profile.read_text())
    if mutation == "foreign_host":
        config["control_plane"]["base_url"] = "https://untrusted.example"
    elif mutation == "remote_ui":
        config["health"]["listen_addr"] = "0.0.0.0:8080"
    elif mutation == "extra_command":
        config["mcp"]["commands"].append({"channel": "other", "command": "unused"})
    elif mutation == "extra_target":
        config["harpoon"] = {"targets": ["http://127.0.0.1:1"]}
    else:
        config["mcp"]["commands"][0]["command"] += " ; unused"
    private(profile, json.dumps(config))
    with pytest.raises(SyncError, match="expanded-scope"):
        load_profile(profile)


@pytest.mark.parametrize("line", [f"OPENAI_API_KEY={FAKE_KEY}\n", f'OPENAI_API_KEY="{FAKE_KEY}"\n'])
def test_key_reader_only_reads_assignment(tmp_path, line):
    env = private(tmp_path / ".env.local", "# comment\nOTHER=ignored\n" + line)
    assert read_runtime_key(env) == FAKE_KEY


@pytest.mark.parametrize(
    "text",
    [
        "OPENAI_API_KEY=$(unused)\n",
        "OPENAI_API_KEY=\n",
        "OTHER=value\n",
        f"OPENAI_API_KEY={FAKE_KEY}\nOPENAI_API_KEY={FAKE_KEY}\n",
        f"OPENAI_API_KEY={FAKE_KEY} ; unused\n",
    ],
)
def test_key_reader_rejects_shell_and_duplicate_values_without_echo(tmp_path, text):
    env = private(tmp_path / ".env.local", text)
    with pytest.raises(SyncError) as caught:
        read_runtime_key(env)
    assert FAKE_KEY not in str(caught.value)


def test_private_key_file_permissions_links_and_size(tmp_path):
    env = private(tmp_path / ".env.local", f"OPENAI_API_KEY={FAKE_KEY}\n")
    env.chmod(0o644)
    with pytest.raises(SyncError, match="private"):
        read_runtime_key(env)
    env.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(env)
    with pytest.raises(SyncError):
        read_runtime_key(link)
    hard = tmp_path / "hard"
    os.link(env, hard)
    with pytest.raises(SyncError):
        read_runtime_key(env)
    large = private(tmp_path / "large", "x" * 65537)
    with pytest.raises(SyncError):
        read_runtime_key(large)


def test_doctor_key_only_in_env_and_unsafe_env_removed(profile, tmp_path, monkeypatch, capsys):
    import code_context.tunnel as module

    env_file = private(tmp_path / ".env.local", f"OPENAI_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("CONTROL_PLANE_BASE_URL", "https://untrusted.example")
    monkeypatch.setenv("LOG_HTTP_RAW_UNSAFE", "true")
    monkeypatch.setenv("MCP_COMMAND", "unused")
    monkeypatch.setenv("CLOUDFLARED_MANAGED", "true")
    monkeypatch.setattr(module, "_client_path", lambda _: "/official/tunnel-client")

    def invoke(args, **kwargs):
        assert FAKE_KEY not in " ".join(args)
        child = kwargs["env"]
        assert child["CONTROL_PLANE_API_KEY"] == FAKE_KEY
        assert child["PYTHONDONTWRITEBYTECODE"] == "1"
        assert "OPENAI_API_KEY" not in child
        assert (
            not {
                "CONTROL_PLANE_BASE_URL",
                "LOG_HTTP_RAW_UNSAFE",
                "MCP_COMMAND",
                "CLOUDFLARED_MANAGED",
            }
            & child.keys()
        )
        assert "--allow-remote-ui=false" in args
        assert "--log.http-raw-unsafe=false" in args
        return SimpleNamespace(stdout=f"diagnostic {FAKE_KEY}\n", stderr="", returncode=0)

    monkeypatch.setattr(module.subprocess, "run", invoke)
    assert launch_tunnel("doctor", profile, env_file, "client") == 0
    output = capsys.readouterr().out
    assert FAKE_KEY not in output and "[redacted]" in output


def test_no_profile_key_read_when_scope_invalid(profile, tmp_path, monkeypatch):
    import code_context.tunnel as module

    config = json.loads(profile.read_text())
    config["control_plane"]["base_url"] = "https://untrusted.example"
    private(profile, json.dumps(config))
    monkeypatch.setattr(module, "read_runtime_key", lambda *_: pytest.fail("key must not be read"))
    with pytest.raises(SyncError):
        launch_tunnel("run", profile, tmp_path / ".env.local", "client")


def test_tunnel_status_missing_health_does_not_claim_web_verification(profile):
    result = tunnel_status(profile)
    assert not result["healthy"] and not result["ready"]
    assert not result["chatgpt_web_verified"]
    assert not result["local_mirror"]["running"]


def test_tunnel_health_probe_cannot_be_redirected_to_external_url(profile, monkeypatch):
    import code_context.tunnel as module

    config = json.loads(profile.read_text())
    Path(config["health"]["url_file"]).write_text("https://untrusted.example", encoding="utf-8")
    monkeypatch.setattr(module.httpx, "Client", lambda *_, **__: pytest.fail("no external probe"))
    with pytest.raises(SyncError, match="loopback"):
        tunnel_status(profile)

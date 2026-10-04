import json
import stat

import pytest

from code_context.client import SyncError
from code_context.onboarding import configure_desktop
from code_context.tunnel import load_profile, read_runtime_key

FAKE_KEY = "sk-" + "z" * 32
FAKE_TUNNEL = "tunnel_" + "0" * 32


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace/examples/sample_project"
    root.mkdir(parents=True)
    (root / "main.py").write_text("sample = True\n")
    return root.parent.parent


def payload(**changes):
    return json.dumps({"tunnel_id": FAKE_TUNNEL, "api_key": FAKE_KEY} | changes)


def test_first_setup_is_private_idle_and_does_not_scan(workspace, monkeypatch):
    monkeypatch.setattr(
        "code_context.scanner.Scanner.scan", lambda *_: pytest.fail("must not scan")
    )
    result = configure_desktop(workspace, payload())
    assert result == {
        "configured": True,
        "remote_connection_started": False,
        "source_collected": False,
    }
    key_file = workspace / ".env.local"
    profile = workspace / ".code-context/tunnel/profile.yaml"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert read_runtime_key(key_file) == FAKE_KEY
    config, root, project, data = load_profile(profile)
    assert root == workspace / "examples/sample_project" and project == "sample"
    assert config["control_plane"]["tunnel_id"] == FAKE_TUNNEL
    assert FAKE_KEY not in profile.read_text() and FAKE_KEY not in str(result)
    assert not data.exists()


def test_existing_settings_are_not_overwritten(workspace):
    configure_desktop(workspace, payload())
    before = (workspace / ".env.local").read_bytes()
    with pytest.raises(SyncError, match="already exist"):
        configure_desktop(workspace, payload(api_key="sk-" + "y" * 32))
    assert (workspace / ".env.local").read_bytes() == before


@pytest.mark.parametrize("entry", [".env.local", ".code-context/tunnel/profile.yaml"])
def test_dangling_settings_symlink_is_rejected(workspace, entry):
    target = workspace / entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(workspace / "missing")
    with pytest.raises(SyncError, match="already exist"):
        configure_desktop(workspace, payload())
    assert target.is_symlink() and not (workspace / "missing").exists()


@pytest.mark.parametrize(
    "text", ["[]", "null", "not json", payload(api_key="bad-secret"), payload(tunnel_id="bad-id")]
)
def test_invalid_setup_has_no_writes_or_secret_echo(workspace, text):
    with pytest.raises(SyncError) as caught:
        configure_desktop(workspace, text)
    assert FAKE_KEY not in str(caught.value) and "bad-secret" not in str(caught.value)
    assert not (workspace / ".env.local").exists()
    assert not (workspace / ".code-context").exists()

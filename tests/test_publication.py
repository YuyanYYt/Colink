import hashlib
import os
import plistlib
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

GUARD = runpy.run_path(str(Path(__file__).parents[1] / "scripts/check-publication.py"))["inspect"]


def test_publication_guard_does_not_echo_secret():
    secret = b"sk-" + b"q" * 40
    result = GUARD("example.py", secret)
    assert result == ["literal-api-key"]
    assert secret.decode() not in str(result)


def test_publication_guard_excludes_user_data_and_fake_ids():
    assert GUARD(".code-context/tunnel/profile.yaml", b"{}") == ["private-or-generated-file"]
    assert GUARD(".env.example", b"OPENAI_API_KEY=\n") == []
    assert GUARD("test.py", b"tunnel_" + b"0" * 32) == []
    assert GUARD("example.json", b"tunnel_" + b"a" * 32) == ["personal-tunnel-id"]


def test_installer_has_explicit_apply_no_overwrite_and_no_security_bypass():
    source = (Path(__file__).parents[1] / "scripts/install-macos.sh").read_text()
    assert "colink_apply=false" in source and "--apply" in source
    assert "Checksum mismatch" in source and "already exists" in source
    assert "codesign --verify --deep --strict" in source
    for forbidden in ("rm -", "xattr -", "spctl --master-disable", "launchctl "):
        assert forbidden not in source


@pytest.mark.parametrize(
    "name,bundle_id,accepted",
    [
        ("CoLink", "local.codeconnect.menubar", True),
        ("Colink", "local.codeconnect.menubar", True),
        ("Unrelated", "local.codeconnect.menubar", False),
        ("CoLink", "local.unrelated.application", False),
    ],
)
def test_installer_brand_identity_compatibility(tmp_path, name, bundle_id, accepted):
    if sys.platform != "darwin" or not shutil.which("plutil") or not shutil.which("bash"):
        pytest.skip("native plist preflight tools unavailable")
    source = (Path(__file__).parents[1] / "scripts/install-macos.sh").read_text()
    checks = source[source.index("colink_name=$(") : source.index('mkdir -p "$colink_destination"')]
    info = tmp_path / "Colink.app/Contents/Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({"CFBundleName": name, "CFBundleIdentifier": bundle_id}))
    result = subprocess.run(
        ["bash", "-e", "-c", checks],
        env={**os.environ, "colink_stage": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == accepted
    if not accepted:
        assert "Unexpected application identity" in result.stderr


def run_installer_dry_run(tmp_path, members, *, checksum=None):
    """Exercise the real script's preflight, with a simulated supported OS.

    This neither installs an app nor substitutes for macOS package validation.
    """
    if not all(shutil.which(tool) for tool in ("bash", "shasum", "unzip", "zipinfo")):
        pytest.skip("ZIP preflight tools unavailable")
    commands = tmp_path / "commands"
    commands.mkdir()
    for name, source in {
        "uname": '#!/bin/bash\nif [ "$1" = -s ]; then echo Darwin; else echo arm64; fi\n',
        "sw_vers": "#!/bin/bash\necho 14.0\n",
    }.items():
        command = commands / name
        command.write_text(source)
        command.chmod(0o755)
    archive = tmp_path / "Colink.zip"
    with ZipFile(archive, "w") as stream:
        for name in members:
            stream.writestr(name, "" if name.endswith("/") else "sample")
    expected = checksum or hashlib.sha256(archive.read_bytes()).hexdigest()
    destination = tmp_path / "Applications"
    result = subprocess.run(
        [
            "bash",
            str(Path(__file__).parents[1] / "scripts/install-macos.sh"),
            "--archive",
            str(archive),
            "--sha256",
            expected,
            "--destination",
            str(destination),
        ],
        env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert not destination.exists()
    assert not list(tmp_path.glob("colink-install.*"))
    return result


def test_installer_accepts_normal_zip_directory_terminators_without_installing(tmp_path):
    result = run_installer_dry_run(
        tmp_path,
        ["Colink.app/", "Colink.app/Contents/", "Colink.app/Contents/Info.plist"],
    )
    assert result.returncode == 0, result.stderr
    assert "Dry run only" in result.stdout
    assert "Retained staging" not in result.stdout


@pytest.mark.parametrize(
    "member",
    [
        "Colink.app/../outside",
        "Colink.app/./value",
        "Colink.app//value",
        "Colink.app//",
        "/Colink.app/",
        "other.app/",
    ],
)
def test_installer_rejects_unsafe_archive_members_without_extracting(tmp_path, member):
    result = run_installer_dry_run(tmp_path, ["Colink.app/", member])
    assert result.returncode != 0
    assert "archive" in result.stderr


def test_installer_rejects_wrong_checksum_before_installing(tmp_path):
    result = run_installer_dry_run(tmp_path, ["Colink.app/"], checksum="0" * 64)
    assert result.returncode != 0
    assert "Checksum mismatch" in result.stderr


def test_upstream_exception_is_not_a_directory_wide_secret_waiver():
    name = "Contents/Resources/tunnel-client/tunnel-client"
    assert GUARD(name, b"sk-" + b"q" * 40, bundle=True) == ["literal-api-key"]
    assert GUARD(name, b"tunnel_" + b"a" * 32, bundle=True) == ["personal-tunnel-id"]
    assert GUARD("Contents/Resources/example.py", b"tunnel_" + b"a" * 32, bundle=True) == [
        "personal-tunnel-id"
    ]

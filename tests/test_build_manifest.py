"""Build provenance binds source bytes without importing a workspace or assuming Git identity."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import code_context.build_manifest as provenance
from code_context.build_manifest import (
    BuildManifestError,
    create_build_manifest,
    verify_build_manifest,
)

INFO = {
    "CFBundleIdentifier": "local.colink.fixture",
    "CFBundleShortVersionString": "0.5.0",
    "CFBundleVersion": "34",
    "CoLinkVersion": "0.5.0b7",
}


def seed(workspace):
    source = workspace / "src/code_context"
    source.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text('[project]\nversion="0.5.0b7"\n')
    (workspace / "uv.lock").write_text("version = 1\n")
    files = {
        "__init__.py": '__version__ = "0.5.0b7"\n',
        "service.py": "def service(value): return value + 1\n",
        "intelligence_models.py": (
            'PARSER_VERSION = f"facts-py{sys.version_info.major}.{sys.version_info.minor}"\n'
        ),
        "intelligence_index.py": 'INDEX_VERSION = f"{PARSER_VERSION}-bindings"\n',
        "live_index.py": '_INDEX_VERSION = f"live-{INDEX_VERSION}"\n',
        "language_frontends.py": (
            'SCRIPT_LANGUAGES = frozenset({"typescript", "javascript"})\n'
            'STRUCTURE_LANGUAGES = frozenset({"python", "java", *SCRIPT_LANGUAGES})\n'
            'VALUE_FLOW_LANGUAGES = frozenset({"python", "java"})\n'
        ),
    }
    for name, content in files.items():
        (source / name).write_text(content)
    return workspace


@pytest.fixture
def workspace(tmp_path):
    return seed(tmp_path / "source-workspace")


def git(workspace, *arguments):
    return subprocess.check_output(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "user.name=CoLink fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-C",
            str(workspace),
            *arguments,
        ],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


def test_manifest_records_versions_capabilities_and_detects_changed_missing_added_code(workspace):
    manifest = create_build_manifest(workspace, bundle_info=INFO)
    source = workspace / "src/code_context"
    assert manifest["versions"]["project"] == manifest["versions"]["package"] == "0.5.0b7"
    assert manifest["versions"]["parser"].startswith("facts-py")
    assert manifest["versions"]["live_index"].endswith("-bindings")
    assert manifest["capabilities"] == {
        "structure_languages": ["java", "javascript", "python", "typescript"],
        "value_flow_languages": ["java", "python"],
        "production_backend": "python",
        "rust_enabled": False,
    }
    assert manifest["bundle"]["build"] == "34"
    assert verify_build_manifest(manifest, source, bundle_info=INFO)["matches"]
    (source / "service.py").write_text("def service(value): return value + 2\n")
    (source / "live_index.py").unlink()
    (source / "added.py").write_text("NEW = True\n")
    checked = verify_build_manifest(manifest, source, bundle_info=INFO)
    assert not checked["matches"]
    assert checked["changed"] == ["service.py"]
    assert checked["missing"] == ["live_index.py"]
    assert checked["extra"] == ["added.py"]
    assert not verify_build_manifest(
        manifest, source, bundle_info={**INFO, "CFBundleVersion": "35"}
    )["bundle_matches"]


def test_bundled_inventory_is_actual_copy_instead_of_workspace_assumption(workspace, tmp_path):
    bundled = tmp_path / "Resources/backend/src/code_context"
    shutil.copytree(workspace / "src/code_context", bundled)
    (workspace / "src/code_context/service.py").write_text("def service(value): return value + 3\n")
    manifest = create_build_manifest(workspace, bundled_source=bundled, bundle_info=INFO)
    assert manifest["source"]["kind"] == "bundled"
    assert manifest["source"]["matches_workspace"] is False
    assert verify_build_manifest(manifest, bundled)["matches"]
    assert not verify_build_manifest(manifest, workspace / "src/code_context")["matches"]


def test_source_is_never_imported_and_private_state_is_not_part_of_inventory(workspace, tmp_path):
    source = workspace / "src/code_context"
    marker = tmp_path / "must-not-execute"
    (source / "__init__.py").write_text(
        '__version__="0.5.0b7"\n'
        + "from pathlib import Path\n"
        + f"Path({str(marker)!r}).write_text('executed')\n"
    )
    (source / ".env").write_text("unread-fixture-secret")
    (source / "runtime.sqlite3").write_bytes(b"not a source file")
    (source / "node_modules").mkdir()
    (source / "node_modules/not_owned.py").write_text("raise RuntimeError('never read')\n")
    manifest = create_build_manifest(workspace)
    assert not marker.exists()
    encoded = json.dumps(manifest)
    assert str(workspace) not in encoded
    assert "unread-fixture-secret" not in encoded and "runtime.sqlite3" not in encoded
    assert "not_owned.py" not in encoded


@pytest.mark.parametrize(
    "info,project_version",
    [
        (INFO, "0.6.0"),
        ({**INFO, "CoLinkVersion": "0.4.0"}, "0.5.0b7"),
        ({**INFO, "CFBundleShortVersionString": "9.9.9"}, "0.5.0b7"),
    ],
)
def test_project_package_and_bundle_versions_must_agree(workspace, info, project_version):
    (workspace / "pyproject.toml").write_text(f'[project]\nversion="{project_version}"\n')
    with pytest.raises(BuildManifestError, match="BUILD_MANIFEST_VERSION_MISMATCH"):
        create_build_manifest(workspace, bundle_info=info)


def test_git_commit_and_dirty_are_bound_to_exact_repository_build_inputs(workspace):
    git(workspace, "init", "--template=")
    git(workspace, "add", "--", "src", "pyproject.toml", "uv.lock")
    git(workspace, "commit", "-m", "source fixture")
    commit = git(workspace, "rev-parse", "HEAD")
    assert create_build_manifest(workspace)["source"]["git"] == {
        "commit": commit,
        "dirty": False,
        "dirty_scope": "build_inputs",
    }
    service = workspace / "src/code_context/service.py"
    original = service.read_bytes()
    service.write_text("def changed(): pass\n")
    git(workspace, "add", "--", "src/code_context/service.py")
    service.write_bytes(original)
    assert create_build_manifest(workspace)["source"]["git"]["dirty"] is True
    nested = seed(workspace / "nested-copy")
    assert create_build_manifest(nested)["source"]["git"] == {
        "commit": "unknown",
        "dirty": None,
        "dirty_scope": "build_inputs",
    }


def test_manifest_rejects_unsafe_source_and_tampered_inventory(workspace, tmp_path):
    source = workspace / "src/code_context"
    manifest = create_build_manifest(workspace)
    manifest["source"]["files"][0]["path"] = "../../outside.py"
    with pytest.raises(BuildManifestError, match="BUILD_MANIFEST_INVALID"):
        verify_build_manifest(manifest, source)
    outside = tmp_path / "outside.py"
    outside.write_text("PRIVATE = True\n")
    (source / "linked.py").symlink_to(outside)
    with pytest.raises(BuildManifestError, match="BUILD_MANIFEST_SOURCE_INVALID"):
        create_build_manifest(workspace)


def test_native_builder_writes_provenance_before_signing(workspace, tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "macos/build.py"
    spec = importlib.util.spec_from_file_location("manifest_native_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assets = workspace / "macos/assets"
    assets.mkdir(parents=True)
    for name in ("logo.svg", "menubar.svg"):
        (assets / name).write_text("<svg/>")
    client = tmp_path / "tunnel-client"
    client.write_bytes(b"fixture-only")
    output = tmp_path / "CoLink.app"
    calls = []

    def command(arguments, **options):
        calls.append(arguments)
        if arguments[0] == "codesign":
            manifest = json.loads(
                (output / "Contents/Resources/colink-build-manifest.json").read_text()
            )
            assert manifest["source"]["kind"] == "workspace"
            assert manifest["bundle"]["build"] == "34"
            assert verify_build_manifest(manifest, workspace / "src/code_context")["matches"]

    monkeypatch.setattr(module.subprocess, "run", command)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fixture/uv")
    monkeypatch.setattr(provenance, "_git", lambda *args: {"commit": "unknown", "dirty": None})
    module.build(workspace, output, "node", "sharp", client=client)
    assert calls[-2][:4] == ["codesign", "--force", "--sign", "-"]
    assert calls[-1][:3] == ["codesign", "--verify", "--strict"]

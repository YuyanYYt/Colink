"""Offline packaging guards: fake native tools, real wheel metadata and file copies."""

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import plistlib
import runpy
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("colink_packager", WORKSPACE / "macos/package.py")
assert SPEC is not None and SPEC.loader is not None
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)
MACHO = bytes.fromhex("cffaedfe") + b"synthetic-native-test-only"


def write(path: Path, content: str | bytes = "test-only") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def wheel(site: Path, name="alpha", version="1.0", requirements=(), members=None):
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    entries = members or {f"{name.replace('-', '_')}/__init__.py": "", "tests/ignored.py": ""}
    entries = {**entries, f"{info.name}/licenses/LICENSE": "Test-only license"}
    for relative, content in entries.items():
        # The RECORD can list wheel console scripts outside site-packages, but
        # a fixture must never materialize those paths outside its test root.
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            continue
        write(site / relative, content)
    write(
        info / "METADATA",
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {value}\n" for value in requirements),
    )
    write(info / "WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\n")
    inventory = [*entries, f"{info.name}/METADATA", f"{info.name}/WHEEL", f"{info.name}/RECORD"]
    write(info / "RECORD", "".join(f"{relative},,\n" for relative in inventory))
    return importlib.metadata.PathDistribution(info)


def metadata(workspace: Path, dependencies=('"alpha>=1"',), versions=None):
    versions = versions or {"colink-mcp": "0.4.0", "alpha": "1.0"}
    write(
        workspace / "pyproject.toml",
        '[project]\nname="colink-mcp"\nversion="0.4.0"\ndependencies=['
        + ",".join(dependencies)
        + "]\n",
    )
    write(
        workspace / "uv.lock",
        'version=1\nrequires-python=">=3.11"\n'
        + "".join(
            f'[[package]]\nname="{name}"\nversion="{version}"\n'
            for name, version in versions.items()
        ),
    )


@pytest.fixture
def inputs(tmp_path):
    workspace = tmp_path / "workspace"
    metadata(workspace)
    write(workspace / "LICENSE", "MIT test fixture")
    write(workspace / "THIRD_PARTY_NOTICES.md", "Third-party test fixture")
    write(workspace / "src/code_context/__init__.py", '__version__="0.4.0"\n')
    write(workspace / "src/code_context/__main__.py", "")
    write(workspace / "src/code_context/cli.py", "def main(): pass\n")
    write(workspace / "src/code_context/__pycache__/cli.pyc", b"excluded")
    write(workspace / "src/code_context/.env.fixture", "excluded fixture")
    write(workspace / ".code-context/server/mirror.sqlite3", "excluded fixture")
    write(workspace / "examples/sample_project/main.py", "print('sample')\n")
    write(workspace / "examples/sample_project/models.py", "class Model: pass\n")
    write(workspace / "examples/sample_project/private.txt", "excluded fixture")
    python = tmp_path / "python"
    write(python / "bin/python3.11", MACHO).chmod(0o755)
    for name in ("python", "python3"):
        (python / "bin" / name).symlink_to("python3.11")
    write(python / "bin/pip3", "excluded fixture")
    write(python / "lib/libpython3.11.dylib", MACHO)
    for path in ("LICENSE.txt", "encodings/__init__.py", "ssl.py", "sqlite3/__init__.py"):
        write(python / "lib/python3.11" / path, "runtime fixture")
    for path in (
        "__pycache__/ssl.pyc",
        "test/test_ssl.py",
        "tkinter/__init__.py",
        "ensurepip/__init__.py",
        "site-packages/pip/__init__.py",
        "config-3.11/Makefile",
    ):
        write(python / "lib/python3.11" / path, "excluded fixture")
    for name in ("_dbm.so", "_testclinic.so", "_tkinter.so"):
        write(python / "lib/python3.11/lib-dynload" / name, MACHO)
    site = tmp_path / "site-packages"
    distribution = wheel(site)
    wheel(site, "pytest", "9.1.1")
    wheel(site, "ruff", "0.16.10")
    write(site / "_editable_impl_colink_mcp.pth", "excluded fixture")
    client = tmp_path / "client"
    for name in ("tunnel-client", "cloudflared"):
        write(client / name, MACHO).chmod(0o755)
    for name in ("LICENSE", "NOTICE", "tunnel-client-v0.0.15-darwin-arm64-licenses.txt"):
        write(client / name, "official-license fixture")
    write(client / "tunnel-client-v0.0.15-darwin-arm64.spdx.json", '{"spdxVersion":"SPDX-2.3"}')
    write(client / "cloudflared-manifest.json", '{"upstream_version":"test-only"}')
    write(client / ".env.fixture", "excluded fixture")
    return {
        "workspace": workspace,
        "python": python,
        "site": site,
        "client": client,
        "dist": distribution,
    }


def test_runtime_closure_excludes_dev_and_handles_extras_and_markers(inputs):
    workspace, site = inputs["workspace"], inputs["site"]
    metadata(
        workspace, ('"alpha[crypto]>=1"',), {"colink-mcp": "0.4.0", "alpha": "1.0", "beta": "2.0"}
    )
    wheel(
        site, requirements=('beta>=2; extra == "crypto"', 'windows-only; sys_platform == "win32"')
    )
    wheel(site, "beta", "2.0")
    selected = PACKAGER.runtime_distributions(workspace, site)
    assert [(dist.metadata["Name"], dist.version) for dist in selected] == [
        ("alpha", "1.0"),
        ("beta", "2.0"),
    ]


@pytest.mark.parametrize("failure", ["missing", "wrong-version", "wrong-project-version"])
def test_lock_or_missing_dependency_fails_closed(inputs, failure):
    workspace = inputs["workspace"]
    versions = {"colink-mcp": "0.4.0", "alpha": "1.0"}
    dependencies = ('"alpha>=1"',)
    if failure == "missing":
        dependencies = ('"missing>=1"',)
    elif failure == "wrong-version":
        versions["alpha"] = "2.0"
    else:
        versions["colink-mcp"] = "0.3.3"
    metadata(workspace, dependencies, versions)
    with pytest.raises(PACKAGER.PackageError):
        PACKAGER.runtime_distributions(workspace, inputs["site"])


def test_resources_are_whitelisted_licensed_and_use_relative_pth(inputs, tmp_path):
    resources = tmp_path / "app/Contents/Resources"
    resources.mkdir(parents=True)
    PACKAGER.bundle_resources(
        inputs["workspace"],
        resources,
        inputs["python"],
        inputs["site"],
        inputs["client"],
        [inputs["dist"]],
    )
    assert json.loads((resources / "runtime.json").read_text()) == PACKAGER.RUNTIME
    assert "workspace" not in PACKAGER.RUNTIME and "uv" not in PACKAGER.RUNTIME
    assert "dataName" not in PACKAGER.RUNTIME
    assert (
        resources / "python/lib/python3.11/site-packages/colink-runtime.pth"
    ).read_text() == "../../../../backend/src\n../../../../vendor\n"
    bundled_site = resources / "python/lib/python3.11/site-packages"
    assert sorted(path.name for path in bundled_site.iterdir()) == ["colink-runtime.pth"]
    for line in PACKAGER.RUNTIME_PTH.splitlines():
        assert not Path(line).is_absolute()
        assert (bundled_site / line).resolve().is_relative_to(resources.resolve())
        assert (bundled_site / line).is_dir()
    members = [path.relative_to(resources).as_posix() for path in resources.rglob("*")]
    for forbidden in (
        ".env",
        "__pycache__",
        "pytest",
        "ruff",
        "_editable",
        "sqlite3.db",
        ".code-context",
        "pip3",
        "_testclinic",
        "_tkinter",
        "Makefile",
        "private.txt",
    ):
        assert not any(forbidden in member for member in members)
    assert (resources / "vendor/alpha-1.0.dist-info/licenses/LICENSE").is_file()
    assert (resources / "python/lib/python3.11/LICENSE.txt").is_file()
    assert (resources / "licenses/LICENSE").is_file()
    assert (resources / "licenses/THIRD_PARTY_NOTICES.md").is_file()
    assert (resources / "tunnel-client/LICENSE").is_file()
    assert (resources / "tunnel-client/NOTICE").is_file()
    assert any("spdx.json" in member for member in members)
    assert any("-licenses.txt" in member for member in members)
    assert (resources / "python/bin/python3.11").stat().st_mode & 0o111
    PACKAGER.validate_links(resources)


def test_python_extra_root_and_share_notices_are_kept_but_pip_notices_are_excluded(
    inputs, tmp_path
):
    home = inputs["python"]
    extras = (
        "NOTICE",
        "share/doc/python/COPYRIGHT",
        "share/licenses/openssl/COPYING",
        "licenses/zlib.txt",
    )
    for path in extras:
        write(home / path, "native-license fixture")
    write(home / "lib/python3.11/site-packages/pip/LICENSE", "excluded pip license fixture")
    destination = tmp_path / "bundled-python"
    PACKAGER.copy_python(home, destination)
    for path in extras:
        assert (destination / path).read_text() == "native-license fixture"
    assert not (destination / "lib/python3.11/site-packages/pip").exists()


@pytest.mark.parametrize(
    "filename", ["_sysconfigdata__darwin_darwin.py", "_sysconfigdata__other.py"]
)
def test_sysconfigdata_has_no_source_prefix_and_resolves_after_relocation_without_type_changes(
    inputs, tmp_path, filename
):
    home = inputs["python"]
    original = {
        "prefix": str(home),
        "exec_prefix": str(home),
        "LIBDIR": f"{home}/lib",
        "CFLAGS": f"-I{home}/include -L{home}/lib",
        "SYSTEM_PATH": "/usr/lib/unchanged",
        "INTEGER": 20,
        "BOOLEAN": False,
        "NONE": None,
        "FLOAT": 0.5,
        "BYTES": b"unchanged",
        "LIST": [1, "unchanged"],
        "TUPLE": (1, "unchanged"),
    }
    source = write(
        home / "lib/python3.11" / filename,
        f"build_time_vars = {original!r}\nother_field = 'unchanged'\n",
    )
    before = source.read_bytes()
    bundled = tmp_path / "first/Colink.app/Contents/Resources/python"
    PACKAGER.copy_python(home, bundled)
    copied = bundled / "lib/python3.11" / filename
    assert str(home) not in copied.read_text()
    assert PACKAGER.PYTHON_PREFIX_PLACEHOLDER in copied.read_text()
    assert runpy.run_path(str(copied))["build_time_vars"]["prefix"] == str(bundled)
    moved = tmp_path / "relocated/Colink.app/Contents/Resources/python"
    moved.parent.mkdir(parents=True)
    bundled.rename(moved)
    loaded = runpy.run_path(str(moved / "lib/python3.11" / filename))
    expected = {
        key: value.replace(str(home), str(moved)) if isinstance(value, str) else value
        for key, value in original.items()
    }
    assert loaded["build_time_vars"] == expected
    assert loaded["build_time_vars"]["prefix"] == str(moved)
    assert all(
        type(loaded["build_time_vars"][key]) is type(value) for key, value in original.items()
    )
    assert loaded["other_field"] == "unchanged"
    assert source.read_bytes() == before


def test_sysconfigdata_scrubbing_does_not_execute_nonliteral_build_metadata(inputs, tmp_path):
    marker = tmp_path / "must-not-be-written"
    path = write(
        tmp_path / "_sysconfigdata__fixture.py",
        f"build_time_vars = {{'prefix': __import__('pathlib').Path({str(marker)!r}).touch()}}\n",
    )
    with pytest.raises(PACKAGER.PackageError, match="literal data"):
        PACKAGER.relocate_sysconfigdata(path, inputs["python"])
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["absolute", "escape", "broken"])
def test_source_links_fail_closed_without_copying_outside(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    outside = write(tmp_path / "outside.txt", "not-to-copy")
    target = {"absolute": str(outside), "escape": "../outside.txt", "broken": "missing.txt"}[kind]
    (source / "linked.py").symlink_to(target)
    with pytest.raises(PACKAGER.PackageError):
        PACKAGER.copy_tree(source, tmp_path / "destination")
    assert not (tmp_path / "destination/linked.py").exists()


def test_copy_file_rejects_a_symlinked_parent_outside_component(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = write(tmp_path / "outside/file.py", "not-to-copy")
    (root / "alias").symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(PACKAGER.PackageError):
        PACKAGER.copy_file(root / "alias/file.py", tmp_path / "copy.py", root)
    assert not (tmp_path / "copy.py").exists()


def test_relative_internal_links_are_preserved(tmp_path):
    source = tmp_path / "source"
    write(source / "real.py", "fixture")
    (source / "alias.py").symlink_to("real.py")
    destination = tmp_path / "destination"
    PACKAGER.copy_tree(source, destination)
    PACKAGER.validate_links(destination)
    assert os.readlink(destination / "alias.py") == "real.py"


@pytest.mark.parametrize("isolated", [False, True])
def test_fixed_pth_really_imports_from_relative_paths_without_python_env(
    inputs, tmp_path, isolated
):
    # Exercise CPython's actual .pth reader, without copying the real runtime,
    # compiling an app or pretending this is a full bundled-Python smoke test.
    resources = tmp_path / "relocated/Contents/Resources"
    site = resources / "python/lib/python3.11/site-packages"
    write(site / "colink-runtime.pth", PACKAGER.RUNTIME_PTH)
    PACKAGER.copy_tree(
        inputs["workspace"] / "src/code_context",
        resources / "backend/src/code_context",
        python_only=True,
    )
    write(resources / "vendor/alpha/__init__.py", "")
    cwd = tmp_path / "independent-working-directory"
    cwd.mkdir()
    probe = (
        "import sys,site,pathlib; site.addsitedir(sys.argv[1]); "
        "import code_context.cli,alpha; "
        "root=pathlib.Path(sys.argv[2]).resolve(); "
        f"assert bool(sys.flags.isolated) == {isolated}; assert sys.dont_write_bytecode; "
        "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(root) "
        "for m in (code_context.cli,alpha)); print('relative-pth-ok')"
    )
    result = subprocess.run(
        [
            sys.executable,
            *(["-I", "-B"] if isolated else []),
            "-S",
            "-c",
            probe,
            str(site),
            str(resources),
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            **({} if isolated else {"PYTHONDONTWRITEBYTECODE": "1"}),
        },
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "relative-pth-ok"


@pytest.mark.parametrize("kind", ["absolute-internal", "escape", "broken"])
def test_final_app_link_validator_checks_all_links(tmp_path, kind):
    root = tmp_path / "app"
    inside = write(root / "inside.txt")
    outside = write(tmp_path / "outside.txt")
    target = {
        "absolute-internal": str(inside),
        "escape": "../outside.txt",
        "broken": "missing.txt",
    }[kind]
    (root / "alias").symlink_to(target)
    assert outside.exists()
    with pytest.raises(PACKAGER.PackageError):
        PACKAGER.validate_links(root)


def test_wheel_inventory_does_not_copy_console_scripts_or_direct_urls(inputs, tmp_path):
    site = inputs["site"]
    dist = wheel(
        site,
        members={
            "alpha/__init__.py": "",
            "../../../bin/alpha": "excluded",
            "alpha-1.0.dist-info/direct_url.json": "excluded",
            "alpha/__pycache__/a.pyc": b"excluded",
        },
    )
    destination = tmp_path / "vendor"
    PACKAGER.copy_vendor([dist], site, destination)
    assert not (destination / "alpha-1.0.dist-info/direct_url.json").exists()
    assert not (destination / "alpha/__pycache__").exists()
    assert sorted(path.name for path in destination.iterdir()) == ["alpha", "alpha-1.0.dist-info"]


def test_dependency_pth_is_rejected_but_project_runtime_pth_is_generated(inputs, tmp_path):
    dist = wheel(inputs["site"], members={"alpha.pth": "excluded fixture"})
    with pytest.raises(PACKAGER.PackageError, match="pth"):
        PACKAGER.copy_vendor([dist], inputs["site"], tmp_path / "vendor")


@pytest.mark.parametrize("name", ["pytest", "ruff"])
def test_dev_tools_cannot_be_explicitly_bundled(inputs, tmp_path, name):
    dist = wheel(inputs["site"], name=name)
    with pytest.raises(PACKAGER.PackageError, match="Development"):
        PACKAGER.copy_vendor([dist], inputs["site"], tmp_path / "vendor")


@pytest.mark.parametrize("missing", ["LICENSE", "NOTICE", "cloudflared", "spdx", "licenses"])
def test_official_client_requires_binaries_licenses_and_sbom(inputs, tmp_path, missing):
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    for path in inputs["client"].iterdir():
        if (
            path.name == missing
            or (missing == "spdx" and "spdx" in path.name)
            or (missing == "licenses" and "-licenses.txt" in path.name)
        ):
            continue
        write(incomplete / path.name, path.read_bytes())
    with pytest.raises(PACKAGER.PackageError, match="Official"):
        PACKAGER.client_inventory(incomplete)


def fake_tools(
    monkeypatch, *, fail_import=False, arch="arm64", dependency="/usr/lib/libSystem.B.dylib"
):
    commands = []

    def run(arguments, **kwargs):
        commands.append((arguments, kwargs))
        name = Path(arguments[0]).name
        if name == "lipo":
            return arch
        if name == "otool":
            if arguments[1] == "-l":
                return "Load command 1\n      cmd LC_BUILD_VERSION\n platform 1\n    minos 14.0\n"
            return f"binary:\n\t{dependency} (compatibility version 1.0.0)"
        if name == "install_name_tool":
            assert arguments[1:3] == ["-id", "@rpath/libpython3.11.dylib"]
            assert Path(arguments[-1]).is_file()
            assert Path(arguments[-1]).read_bytes() == MACHO
        if name == "python3.11":
            assert "PYTHONHOME" not in kwargs["env"] and "PYTHONPATH" not in kwargs["env"]
            if "-I" in arguments:
                assert "-B" in arguments
            else:
                assert "-B" not in arguments and kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
            assert "relocation-check" in arguments[0]
            if "--version" in arguments:
                return "0.4.0" if "-m" in arguments else "Python 3.11.15"
            if fail_import:
                raise PACKAGER.PackageError("synthetic import failure")
            assert "code_context.cli,mcp,httpx" in arguments[-1]
            assert Path(kwargs["cwd"]).name == "relocation-check"
            return "Bundled imports OK"
        if name == "ditto":
            write(Path(arguments[-1]), b"synthetic-zip-for-orchestration-test")
        if name == "hdiutil" and arguments[1] == "create":
            write(Path(arguments[-1]), b"synthetic-dmg-for-orchestration-test")
        return ""

    monkeypatch.setattr(PACKAGER, "run_tool", run)
    return commands


def test_native_components_sign_inside_out_and_verify_strict(tmp_path, monkeypatch):
    app = tmp_path / "Colink.app"
    native = write(app / "Contents/Resources/vendor/native.so", MACHO)
    write(app / "Contents/Resources/readme.txt", "data")
    commands = fake_tools(monkeypatch)
    PACKAGER.sign_app(app)
    signed = [
        args[-1] for args, _ in commands if args[:4] == ["codesign", "--force", "--sign", "-"]
    ]
    assert signed == [str(native), str(app)]
    assert commands[-1][0] == ["codesign", "--verify", "--deep", "--strict", str(app)]


@pytest.mark.parametrize("failure", ["wrong-arch", "external-link"])
def test_nonportable_native_binaries_fail_closed(tmp_path, monkeypatch, failure):
    app = tmp_path / "Colink.app"
    write(app / "Contents/MacOS/CodeConnect", MACHO)
    fake_tools(
        monkeypatch,
        arch="x86_64" if failure == "wrong-arch" else "arm64",
        dependency="/private/build/lib.dylib"
        if failure == "external-link"
        else "/usr/lib/libSystem.B.dylib",
    )
    with pytest.raises(PACKAGER.PackageError):
        PACKAGER.sign_app(app)


@pytest.mark.parametrize("failure", ["too-new-os", "absolute-rpath"])
def test_incompatible_macho_load_commands_fail_closed(tmp_path, monkeypatch, failure):
    app = tmp_path / "Colink.app"
    write(app / "Contents/MacOS/CodeConnect", MACHO)
    fake_tools(monkeypatch)
    tool = PACKAGER.run_tool

    def run(arguments, **kwargs):
        if arguments[:2] == ["otool", "-l"]:
            if failure == "too-new-os":
                return "Load command 1\n  cmd LC_BUILD_VERSION\n  platform 1\n  minos 15.0\n"
            return "Load command 1\n  cmd LC_RPATH\n  path /private/build/lib (offset 12)\n"
        return tool(arguments, **kwargs)

    monkeypatch.setattr(PACKAGER, "run_tool", run)
    with pytest.raises(PACKAGER.PackageError):
        PACKAGER.sign_app(app)


@pytest.mark.parametrize("fail", [False, True])
def test_relocation_uses_isolated_python_without_python_env_and_preserves_app(
    tmp_path, monkeypatch, fail
):
    app = tmp_path / "image-root/Colink.app"
    write(app / "Contents/Resources/python/bin/python3.11", MACHO)
    write(app / "Contents/Info.plist", plistlib.dumps({"CFBundleShortVersionString": "0.4.0"}))
    commands = fake_tools(monkeypatch, fail_import=fail)
    monkeypatch.setenv("PYTHONHOME", "synthetic-ignored-home")
    monkeypatch.setenv("PYTHONPATH", "synthetic-ignored-path")
    if fail:
        with pytest.raises(PACKAGER.PackageError, match="synthetic"):
            PACKAGER.verify_relocation(app, tmp_path)
    else:
        PACKAGER.verify_relocation(app, tmp_path)
        python_calls = [
            (args, kwargs) for args, kwargs in commands if Path(args[0]).name == "python3.11"
        ]
        assert len(python_calls) == 4
        assert python_calls[2][0][1:] == ["-m", "code_context", "--version"]
        assert python_calls[3][0][1] == "-c"
        assert python_calls[2][1]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        assert commands[-1][0][:4] == ["codesign", "--verify", "--deep", "--strict"]
    assert app.exists() and (tmp_path / "relocation-check").is_dir()
    assert not (tmp_path / "relocation-check/Colink.app").exists()


def test_package_orchestration_creates_private_path_free_report_and_both_archives(
    inputs, tmp_path, monkeypatch
):
    output = tmp_path / "release"
    calls = []

    def build(workspace, app, node, sharp, client, bundle_id):
        calls.append((workspace, node, sharp, client, bundle_id))
        write(app / "Contents/MacOS/CodeConnect", MACHO).chmod(0o755)
        (app / "Contents/Resources").mkdir()
        (app / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleShortVersionString": "0.4.0", "LSUIElement": True})
        )
        return app

    monkeypatch.setattr(PACKAGER, "native_build", build)
    monkeypatch.setattr(PACKAGER.sys, "platform", "darwin")
    monkeypatch.setattr(PACKAGER.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(PACKAGER.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    commands = fake_tools(monkeypatch)
    report = PACKAGER.package(
        inputs["workspace"],
        output,
        "node",
        "sharp",
        client=inputs["client"] / "tunnel-client",
        python_home=inputs["python"],
        site_packages=inputs["site"],
    )
    assert calls == [
        (
            inputs["workspace"],
            "node",
            "sharp",
            inputs["client"] / "tunnel-client",
            "local.codeconnect.menubar",
        )
    ]
    assert report["version"] == "0.4.0"
    assert report["platform"] == {"os": "macos", "architecture": "arm64", "minimum_version": "14.0"}
    text = (output / "package-report.json").read_text()
    assert json.loads(text) == report and str(tmp_path) not in text
    assert set(report["artifacts"]) == {"Colink-macos-arm64.zip", "Colink-macos-arm64.dmg"}
    for name, summary in report["artifacts"].items():
        assert summary["sha256"] == hashlib.sha256((output / name).read_bytes()).hexdigest()
        assert summary["size_bytes"] == (output / name).stat().st_size
        assert f"{summary['sha256']}  {name}\n" in (output / "SHA256SUMS").read_text()
    assert os.readlink(output / "image-root/Applications") == "/Applications"
    app = output / "image-root/Colink.app"
    PACKAGER.validate_links(app)
    install_name = [
        "install_name_tool",
        "-id",
        "@rpath/libpython3.11.dylib",
        str(app / "Contents/Resources/python/lib/libpython3.11.dylib"),
    ]
    argv = [args for args, _ in commands]
    assert [args for args in argv if args[0] == "install_name_tool"] == [install_name]
    assert argv.index(install_name) < next(
        index for index, args in enumerate(argv) if args[0] == "codesign"
    )
    assert (inputs["python"] / "lib/libpython3.11.dylib").read_bytes() == MACHO
    assert [
        "ditto",
        "-c",
        "-k",
        "--norsrc",
        "--noextattr",
        "--keepParent",
        str(app),
        str(output / "Colink-macos-arm64.zip"),
    ] in [args for args, _ in commands]
    assert any(args[:2] == ["hdiutil", "create"] and "UDZO" in args for args, _ in commands)
    assert ["hdiutil", "verify", str(output / "Colink-macos-arm64.dmg")] in [
        args for args, _ in commands
    ]
    assert not any(
        Path(args[0]).name in {"tunnel-client", "cloudflared", "uv", "git", "gh"}
        for args, _ in commands
    )
    with (app / "Contents/Info.plist").open("rb") as stream:
        assert plistlib.load(stream)["LSUIElement"] is True


def test_missing_install_name_tool_fails_before_build_or_output_creation(
    inputs, tmp_path, monkeypatch
):
    output = tmp_path / "release"
    monkeypatch.setattr(PACKAGER.sys, "platform", "darwin")
    monkeypatch.setattr(PACKAGER.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        PACKAGER.shutil,
        "which",
        lambda tool: None if tool == "install_name_tool" else f"/usr/bin/{tool}",
    )
    monkeypatch.setattr(PACKAGER, "native_build", lambda *args: pytest.fail("must not build"))
    with pytest.raises(PACKAGER.PackageError, match="install_name_tool"):
        PACKAGER.package(
            inputs["workspace"],
            output,
            "node",
            "sharp",
            client=inputs["client"],
            python_home=inputs["python"],
            site_packages=inputs["site"],
        )
    assert not output.exists()


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("ditto"), reason="macOS ditto only"
)
def test_real_ditto_zip_has_one_root_and_keeps_modes_and_symlinks_without_macosx(tmp_path):
    # Only a tiny synthetic fixture is archived, not the actual app or runtime.
    app = tmp_path / "Colink.app"
    executable = write(app / "Contents/MacOS/fixture", b"synthetic executable fixture")
    executable.chmod(0o755)
    (executable.parent / "alias").symlink_to("fixture")
    archive = tmp_path / "fixture.zip"
    PACKAGER.run_tool(
        [
            "ditto",
            "-c",
            "-k",
            "--norsrc",
            "--noextattr",
            "--keepParent",
            str(app),
            str(archive),
        ]
    )
    with zipfile.ZipFile(archive) as zipped:
        assert all(name.startswith("Colink.app/") for name in zipped.namelist())
        assert not any("__MACOSX" in name for name in zipped.namelist())
        entry = zipped.getinfo("Colink.app/Contents/MacOS/fixture")
        alias = zipped.getinfo("Colink.app/Contents/MacOS/alias")
        assert stat.S_IMODE(entry.external_attr >> 16) == 0o755
        assert stat.S_ISLNK(alias.external_attr >> 16)
        assert zipped.read(alias) == b"fixture"


@pytest.mark.parametrize("existing", ["directory", "file", "symlink", "broken-symlink"])
def test_existing_outputs_are_never_overwritten_before_any_build(tmp_path, monkeypatch, existing):
    output = tmp_path / "existing"
    if existing == "directory":
        output.mkdir()
        write(output / "preserved.txt", "user-owned")
    elif existing == "file":
        write(output, "user-owned")
    else:
        target = tmp_path / "target"
        if existing == "symlink":
            target.mkdir()
        output.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(PACKAGER, "native_build", lambda *args: pytest.fail("must not build"))
    with pytest.raises(PACKAGER.PackageError, match="Output already exists"):
        PACKAGER.package(tmp_path, output, "node", "sharp")
    assert output.exists() or output.is_symlink()
    if existing == "directory":
        assert (output / "preserved.txt").read_text() == "user-owned"


def test_native_builder_wrapper_uses_public_interface_and_generic_chat_url(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        PACKAGER.runpy,
        "run_path",
        lambda _: {"build": lambda *args, **kwargs: calls.append((args, kwargs)) or args[1]},
    )
    app = tmp_path / "Colink.app"
    client = tmp_path / "tunnel-client"
    assert PACKAGER.native_build(tmp_path, app, "node", "sharp", client, "test.colink") == app
    assert calls == [
        (
            (tmp_path, app, "node", "sharp"),
            {
                "client": client,
                "chat_url": "https://chatgpt.com/plugins",
                "bundle_id": "test.colink",
            },
        )
    ]


def test_live_runtime_configuration_is_generic_unless_explicitly_private(tmp_path):
    generic = PACKAGER.runtime_configuration(tmp_path, "live", False)
    assert generic == {**PACKAGER.RUNTIME, "sourceMode": "live"}
    assert str(tmp_path) not in json.dumps(generic)
    private = PACKAGER.runtime_configuration(tmp_path, "live", True)
    assert private["runtimeWorkspace"] == str(tmp_path.resolve())
    assert private["sampleRoot"] == str(tmp_path.resolve() / "examples/sample_project")
    assert private["python"] == PACKAGER.RUNTIME["python"]
    assert "sourceMode" not in PACKAGER.RUNTIME and "runtimeWorkspace" not in PACKAGER.RUNTIME


def test_private_acceptance_runtime_can_isolate_state_without_changing_sample(tmp_path):
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    private = PACKAGER.runtime_configuration(tmp_path, "live", True, state)
    assert private["runtimeWorkspace"] == str(state)
    assert private["sampleRoot"] == str(tmp_path / "examples/sample_project")
    assert "runtimeWorkspace" not in PACKAGER.RUNTIME


@pytest.mark.parametrize("kind", ["public", "outside", "symlink", "shared", "missing"])
def test_acceptance_runtime_override_rejects_invalid_boundaries(tmp_path, kind):
    workspace = tmp_path / "source"
    workspace.mkdir()
    state = workspace / "private-state"
    if kind == "outside":
        state = tmp_path / "outside"
        state.mkdir(mode=0o700)
    elif kind == "symlink":
        real = workspace / "real"
        real.mkdir(mode=0o700)
        state.symlink_to(real, target_is_directory=True)
    elif kind != "missing":
        state.mkdir(mode=0o755 if kind == "shared" else 0o700)
    with pytest.raises(PACKAGER.PackageError):
        PACKAGER.runtime_configuration(workspace, "live", kind != "public", state)


@pytest.mark.parametrize("mode,portable", [("unknown", False), ("mirror", True)])
def test_invalid_runtime_mode_is_rejected_before_creating_resources(tmp_path, mode, portable):
    resources = tmp_path / "not-created"
    with pytest.raises(PACKAGER.PackageError):
        PACKAGER.bundle_resources(
            tmp_path,
            resources,
            tmp_path,
            tmp_path,
            tmp_path,
            [],
            source_mode=mode,
            portable_runtime=portable,
        )
    assert not resources.exists()


def test_private_package_rejects_output_outside_workspace_before_build(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "outside"
    monkeypatch.setattr(PACKAGER, "native_build", lambda *args: pytest.fail("must not build"))
    with pytest.raises(PACKAGER.PackageError, match="within the workspace"):
        PACKAGER.package(
            workspace, output, "node", "sharp", source_mode="live", portable_runtime=True
        )
    assert not output.exists()


def test_live_native_builder_propagates_mode_without_changing_bundle_identity(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        PACKAGER.runpy,
        "run_path",
        lambda _: {"build": lambda *args, **kwargs: calls.append(kwargs) or args[1]},
    )
    app = tmp_path / "Colink.app"
    PACKAGER.native_build(
        tmp_path,
        app,
        "node",
        "sharp",
        tmp_path / "client",
        "local.codeconnect.menubar",
        source_mode="live",
    )
    assert calls[0]["source_mode"] == "live"
    assert calls[0]["bundle_id"] == "local.codeconnect.menubar"


def test_relocation_compares_complete_prerelease_version(tmp_path, monkeypatch):
    app = tmp_path / "image-root/Colink.app"
    write(app / "Contents/Resources/python/bin/python3.11", MACHO)
    write(
        app / "Contents/Info.plist",
        plistlib.dumps(
            {
                "CFBundleShortVersionString": "0.4.4",
                "CoLinkVersion": "0.4.4a1",
            }
        ),
    )
    commands = fake_tools(monkeypatch)
    original = PACKAGER.run_tool

    def run(args, **kwargs):
        if args[1:] == ["-m", "code_context", "--version"]:
            commands.append((args, kwargs))
            return "0.4.4a1"
        return original(args, **kwargs)

    monkeypatch.setattr(PACKAGER, "run_tool", run)
    PACKAGER.verify_relocation(app, tmp_path)
    assert app.exists()


def test_tool_failure_does_not_echo_subprocess_output_or_secrets(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1, args[0], output="synthetic-hidden-output", stderr="synthetic-hidden-stderr"
        )

    monkeypatch.setattr(PACKAGER.subprocess, "run", fail)
    with pytest.raises(PACKAGER.PackageError) as error:
        PACKAGER.run_tool(["codesign", "--verify", "app"])
    assert "synthetic-hidden" not in str(error.value)
    assert "preserved" in str(error.value)


def test_summary_is_relocation_invariant_and_tracks_permissions_and_content(tmp_path):
    first = tmp_path / "one"
    file = write(first / "relative.txt", "same-content")
    (first / "alias").symlink_to("relative.txt")
    original = PACKAGER.member_summary(first)
    second = tmp_path / "two"
    first.rename(second)
    assert PACKAGER.member_summary(second) == original
    file = second / file.name
    file.chmod(0o700)
    assert PACKAGER.member_summary(second)["sha256_tree"] != original["sha256_tree"]
    assert original["files"] == 1 and original["symlinks"] == 1
    assert original["size_bytes"] == len("same-content")


def test_cli_accepts_explicit_build_paths_and_does_not_publish(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(PACKAGER, "package", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(
        PACKAGER.sys,
        "argv",
        [
            "package.py",
            "--workspace",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "new"),
            "--node",
            "node-test",
            "--sharp",
            "sharp-test",
            "--client",
            "client-test",
        ],
    )
    PACKAGER.main()
    assert calls[0][0] == (tmp_path, tmp_path / "new", "node-test", "sharp-test")
    assert calls[0][1]["client"] == Path("client-test")
    assert "Colink-macos-arm64.dmg" in capsys.readouterr().out

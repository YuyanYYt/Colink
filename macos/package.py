"""Build a self-contained Apple Silicon release, preserving all build artifacts.

Run with the project's locked uv environment. Nothing downloads dependencies,
opens a tunnel, reads credentials, installs an app, or publishes a release.
"""

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import plistlib
import re
import runpy
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path

CHAT_URL = "https://chatgpt.com/plugins"
RUNTIME = {
    "mode": "bundled",
    "python": "python/bin/python3.11",
    "backend": "backend",
    "client": "tunnel-client/tunnel-client",
    "sampleRoot": "sample_project",
    "chatURL": CHAT_URL,
}
RUNTIME_PTH = "../../../../backend/src\n../../../../vendor\n"
PYTHON_PREFIX_PLACEHOLDER = "__COLINK_BUNDLED_PYTHON__"
EXCLUDED = {"__pycache__", "test", "tests", ".DS_Store", ".pytest_cache"}
MACHO_MAGIC = {
    bytes.fromhex(value)
    for value in (
        "feedface",
        "cefaedfe",
        "feedfacf",
        "cffaedfe",
        "cafebabe",
        "bebafeca",
        "cafebabf",
        "bfbafeca",
    )
}


class PackageError(RuntimeError):
    """A fail-closed packaging error; existing outputs must stay untouched."""


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def project_metadata(workspace: Path) -> dict:
    return tomllib.loads((workspace / "pyproject.toml").read_text())["project"]


def runtime_distributions(workspace: Path, site_packages: Path) -> list:
    """Use installed wheel metadata for markers/extras, and uv.lock for versions.

    This selects the runtime dependency closure only, not every installed wheel.
    packaging is a build-time dependency already present in the locked dev env.
    """
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    metadata = project_metadata(workspace)
    lock = tomllib.loads((workspace / "uv.lock").read_text())
    locked = {}
    for entry in lock["package"]:
        locked.setdefault(normalized(entry["name"]), set()).add(entry["version"])
    if metadata["version"] not in locked.get(normalized(metadata["name"]), set()):
        raise PackageError("Project version differs from uv.lock; run uv sync --locked.")
    installed = {
        normalized(dist.metadata["Name"]): dist
        for dist in importlib.metadata.distributions(path=[str(site_packages)])
    }
    pending = [(Requirement(value), {""}) for value in metadata["dependencies"]]
    selected = {}
    visited = set()
    environment = default_environment()
    while pending:
        requirement, parent_extras = pending.pop()
        if requirement.marker and not any(
            requirement.marker.evaluate({**environment, "extra": extra}) for extra in parent_extras
        ):
            continue
        name = normalized(requirement.name)
        extras = frozenset({"", *requirement.extras})
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        dist = installed.get(name)
        if (
            dist is None
            or dist.version not in locked.get(name, set())
            or not requirement.specifier.contains(dist.version)
            or requirement.url
        ):
            raise PackageError(f"Missing or mismatched locked runtime dependency: {name}.")
        if not dist.files:
            raise PackageError(f"Runtime dependency has no wheel file inventory: {name}.")
        selected[name] = dist
        pending.extend((Requirement(value), extras) for value in dist.requires or [])
    return [selected[name] for name in sorted(selected)]


def ignored(path: Path) -> bool:
    return (
        any(part in EXCLUDED or part.startswith(".env") for part in path.parts)
        or path.suffix in {".pyc", ".pyo"}
        or path.name in {"direct_url.json", ".empty"}
    )


def copy_file(source: Path, destination: Path, source_root: Path) -> None:
    """Never follow an absolute, escaping, or broken link into the package."""
    if not source.resolve().is_relative_to(source_root.resolve()):
        raise PackageError("A source file points outside its component.")
    if source.is_symlink():
        target = os.readlink(source)
        if Path(target).is_absolute() or not source.resolve().is_relative_to(source_root.resolve()):
            raise PackageError("A source symlink points outside its component.")
        if not source.exists():
            raise PackageError("A source symlink is broken.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(target, target_is_directory=source.is_dir())
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise PackageError("A required runtime file is missing or is not a regular file.")


def copy_tree(source: Path, destination: Path, *, python_only: bool = False) -> None:
    if source.is_symlink() or not source.is_dir():
        raise PackageError("A component must be a real directory.")
    destination.mkdir(parents=True)
    for current, directories, files in os.walk(source, followlinks=False):
        parent = Path(current)
        directories[:] = [name for name in directories if not ignored(Path(name))]
        for name in list(directories):
            entry = parent / name
            if entry.is_symlink():
                copy_file(entry, destination / entry.relative_to(source), source)
                directories.remove(name)
        for name in files:
            entry = parent / name
            relative = entry.relative_to(source)
            if ignored(relative) or (python_only and entry.suffix != ".py"):
                continue
            copy_file(entry, destination / relative, source)


def copy_python(home: Path, destination: Path) -> None:
    """Keep interpreter, standard runtime and license, not pip/build/Tk/test data."""
    if home.is_symlink() or not (home / "lib/python3.11/LICENSE.txt").is_file():
        raise PackageError("Python 3.11 runtime and its license are required.")
    for name in ("python3.11", "python3", "python"):
        source = home / "bin" / name
        if name == "python3.11" or source.exists() or source.is_symlink():
            copy_file(source, destination / "bin" / name, home)
    copy_file(home / "lib/libpython3.11.dylib", destination / "lib/libpython3.11.dylib", home)
    stdlib = home / "lib/python3.11"
    target = destination / "lib/python3.11"
    target.mkdir(parents=True)
    excluded = {"site-packages", "ensurepip", "idlelib", "tkinter", "turtledemo"}
    for entry in sorted(stdlib.iterdir()):
        if ignored(Path(entry.name)) or entry.name in excluded or entry.name.startswith("config-"):
            continue
        output = target / entry.name
        if entry.is_dir() and not entry.is_symlink():
            if entry.name == "lib-dynload":
                output.mkdir()
                for extension in sorted(entry.iterdir()):
                    if not ignored(Path(extension.name)) and not extension.name.startswith(
                        ("_test", "_tkinter", "_xxtest", "xxlimited")
                    ):
                        copy_file(extension, output / extension.name, home)
            else:
                copy_tree(entry, output)
        else:
            copy_file(entry, output, home)
            if entry.name.startswith("_sysconfigdata_") and entry.suffix == ".py":
                relocate_sysconfigdata(output, home)
    bundled_site = target / "site-packages"
    bundled_site.mkdir()
    # site resolves .pth entries relative to this file. -I still loads system
    # site-packages, so the safe tunnel launcher needs no PYTHON* environment.
    (bundled_site / "colink-runtime.pth").write_text(RUNTIME_PTH)
    copy_python_licenses(home, destination)


def relocate_sysconfigdata(path: Path, home: Path) -> None:
    """Scrub copied build metadata, resolving its prefix only after relocation."""
    if path.is_symlink():
        raise PackageError("Copied sysconfig data must be a regular file.")
    tree = ast.parse(path.read_text())
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "build_time_vars"
            for target in node.targets
        )
    ]
    if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Dict):
        raise PackageError("Python sysconfig build_time_vars must be one literal dictionary.")
    try:
        ast.literal_eval(assignments[0].value)
    except (ValueError, TypeError) as exc:
        raise PackageError("Python sysconfig build_time_vars is not literal data.") from exc
    prefix = str(home.absolute())
    for value in assignments[0].value.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            value.value = value.value.replace(prefix, PYTHON_PREFIX_PLACEHOLDER)
    source = (
        ast.unparse(tree)
        + "\n\n"
        + (
            "def _colink_relocate_build_time_vars():\n"
            "    import os\n"
            "    root = os.path.dirname(os.path.dirname("
            "os.path.dirname(os.path.abspath(__file__))))\n"
            "    for key, value in build_time_vars.items():\n"
            f"        if isinstance(value, str) and {PYTHON_PREFIX_PLACEHOLDER!r} in value:\n"
            "            build_time_vars[key] = value.replace("
            f"{PYTHON_PREFIX_PLACEHOLDER!r}, root)\n"
            "\n"
            "_colink_relocate_build_time_vars()\n"
            "del _colink_relocate_build_time_vars\n"
        )
    )
    if prefix in source:
        raise PackageError("Python sysconfig data still contains the source Python prefix.")
    path.write_text(source)


def copy_python_licenses(home: Path, destination: Path) -> None:
    """Retain extra distribution notices without including pip/setuptools."""
    for current, directories, files in os.walk(home, followlinks=False):
        directories[:] = [
            name for name in directories if not ignored(Path(name)) and name != "site-packages"
        ]
        parent = Path(current)
        for name in files:
            source = parent / name
            relative = source.relative_to(home)
            legal_directory = any(
                part.lower() in {"license", "licenses", "notices"} for part in relative.parts[:-1]
            )
            if ignored(relative) or not (
                legal_directory
                or name.upper().startswith(("LICENSE", "NOTICE", "COPYING", "COPYRIGHT"))
            ):
                continue
            output = destination / relative
            if not output.exists() and not output.is_symlink():
                copy_file(source, output, home)


def copy_vendor(distributions: list, source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for dist in distributions:
        if normalized(dist.metadata["Name"]) in {"pytest", "ruff"}:
            raise PackageError("Development-only tools must not be bundled.")
        licenses = 0
        for entry in dist.files:
            relative = Path(str(entry))
            # Wheel console scripts live outside site-packages; no shebang with
            # the build machine's interpreter belongs in this runtime.
            if relative.is_absolute() or ".." in relative.parts or ignored(relative):
                continue
            if relative.suffix == ".pth" or relative.name.startswith("_editable"):
                raise PackageError("Editable/pth runtime dependencies are not supported.")
            original = source / relative
            output = destination / relative
            if not output.exists() and not output.is_symlink():
                copy_file(original, output, source)
            if relative.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                licenses += 1
        if not licenses:
            raise PackageError(f"Runtime dependency license is missing: {dist.metadata['Name']}.")


def client_inventory(client: Path) -> tuple[Path, list[Path]]:
    directory = client if client.is_dir() else client.parent
    required = [directory / name for name in ("tunnel-client", "cloudflared", "LICENSE", "NOTICE")]
    licenses = sorted(directory.glob("*-licenses.txt"))
    sbom = sorted(directory.glob("*.spdx.json"))
    if not licenses or not sbom or any(not path.is_file() for path in required):
        raise PackageError("Official tunnel-client, cloudflared, licenses and SPDX are required.")
    notices = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.name.upper().startswith(("LICENSE", "NOTICE", "COPYING"))
    ]
    manifest = directory / "cloudflared-manifest.json"
    return directory, sorted(
        set(required + licenses + sbom + notices + ([manifest] if manifest.is_file() else []))
    )


def bundle_resources(
    workspace: Path,
    resources: Path,
    python_home: Path,
    site_packages: Path,
    client: Path,
    distributions: list,
) -> None:
    copy_python(python_home, resources / "python")
    copy_vendor(distributions, site_packages, resources / "vendor")
    copy_tree(
        workspace / "src/code_context", resources / "backend/src/code_context", python_only=True
    )
    sample = workspace / "examples/sample_project"
    for name in ("main.py", "models.py"):
        copy_file(sample / name, resources / "sample_project" / name, sample)
    client_root, files = client_inventory(client)
    for source in files:
        copy_file(source, resources / "tunnel-client" / source.name, client_root)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        source = workspace / name
        if name == "THIRD_PARTY_NOTICES.md" and not source.is_file():
            source = workspace / "THIRD_PARTY_NOTICES"
        copy_file(source, resources / "licenses" / source.name, workspace)
    (resources / "runtime.json").write_text(json.dumps(RUNTIME, indent=2) + "\n")


def validate_links(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink() and (
            Path(os.readlink(path)).is_absolute()
            or not path.resolve().is_relative_to(root.resolve())
            or not path.exists()
        ):
            raise PackageError("The app contains an absolute, escaping or broken symlink.")


def run_tool(arguments: list[str], **kwargs) -> str:
    try:
        result = subprocess.run(arguments, check=True, capture_output=True, text=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise PackageError(
            f"{Path(arguments[0]).name} failed; build artifacts were preserved."
        ) from exc
    return result.stdout.strip()


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(4) in MACHO_MAGIC


def sign_app(app: Path) -> None:
    for path in sorted(app.rglob("*")):
        if not is_macho(path):
            continue
        if "arm64" not in run_tool(["lipo", "-archs", str(path)]).split():
            raise PackageError("The app contains a native binary without Apple Silicon support.")
        dependencies = run_tool(["otool", "-L", str(path)]).splitlines()[1:]
        for line in dependencies:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith("/") and not dependency.startswith(
                ("/usr/lib/", "/System/Library/")
            ):
                raise PackageError("A native binary links to a non-system absolute path.")
        load_commands = run_tool(["otool", "-l", str(path)])
        for block in load_commands.split("Load command "):
            if "cmd LC_RPATH" in block:
                match = re.search(r"^\s+path (.+?) \(offset", block, re.MULTILINE)
                if (
                    match
                    and match[1].startswith("/")
                    and not match[1].startswith(("/usr/lib/", "/System/Library/"))
                ):
                    raise PackageError("A native binary has a non-system absolute rpath.")
            key = (
                "minos"
                if "cmd LC_BUILD_VERSION" in block
                else "version"
                if "cmd LC_VERSION_MIN_MACOSX" in block
                else None
            )
            if key:
                match = re.search(rf"^\s+{key} ([\d.]+)", block, re.MULTILINE)
                if match and tuple(map(int, (match[1].split(".") + ["0", "0"])[:3])) > (14, 0, 0):
                    raise PackageError("A native binary requires a newer OS than macOS 14.0.")
        # Sign each native component explicitly before sealing the outer app.
        run_tool(["codesign", "--force", "--sign", "-", str(path)])
        run_tool(["codesign", "--verify", "--strict", str(path)])
    run_tool(["codesign", "--force", "--sign", "-", str(app)])
    run_tool(["codesign", "--verify", "--deep", "--strict", str(app)])


def verify_relocation(app: Path, output_dir: Path) -> None:
    relocated = output_dir / "relocation-check" / app.name
    relocated.parent.mkdir()
    app.rename(relocated)
    try:
        # Do not forward credentials, developer PYTHONPATH, DYLD settings, or
        # the user's shell config. No HOME override and no network calls.
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
        python = relocated / "Contents/Resources/python/bin/python3.11"
        if not run_tool([str(python), "-I", "-B", "--version"], env=environment).startswith(
            "Python 3.11."
        ):
            raise PackageError("The relocated interpreter is not Python 3.11.")
        probe = (
            "import pathlib,sys,ssl,sqlite3; import code_context.cli,mcp,httpx; "
            "import pydantic_core,watchfiles,cryptography.hazmat.bindings._rust; "
            "import tree_sitter,tree_sitter_java; "
            "tree_sitter.Parser(tree_sitter.Language(tree_sitter_java.language())); "
            "root=pathlib.Path(sys.executable).resolve().parents[2]; "
            "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(root) "
            "for m in (code_context.cli,mcp,httpx,pydantic_core,watchfiles,"
            "tree_sitter,tree_sitter_java)); "
            "assert sys.dont_write_bytecode; "
        )
        run_tool(
            [
                str(python),
                "-I",
                "-B",
                "-c",
                probe + "assert sys.flags.isolated; print('Bundled imports OK')",
            ],
            env=environment,
            cwd=relocated.parent,
        )
        # Match the existing stdio launcher's argv shape, not just Swift's -I
        # mode. This environment keeps bytecode outside the signed bundle.
        tunnel_environment = {**environment, "PYTHONDONTWRITEBYTECODE": "1"}
        with (relocated / "Contents/Info.plist").open("rb") as stream:
            expected_version = plistlib.load(stream)["CFBundleShortVersionString"]
        cli_version = run_tool(
            [str(python), "-m", "code_context", "--version"],
            env=tunnel_environment,
            cwd=relocated.parent,
        )
        if cli_version != expected_version:
            raise PackageError("The original-shape child command loaded the wrong backend version.")
        run_tool(
            [
                str(python),
                "-c",
                probe + "assert not sys.flags.isolated; print('Bundled imports OK')",
            ],
            env=tunnel_environment,
            cwd=relocated.parent,
        )
        if any(
            path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
            for path in relocated.rglob("*")
        ):
            raise PackageError("A relocation probe wrote bytecode inside the signed app.")
        run_tool(["codesign", "--verify", "--deep", "--strict", str(relocated)])
    finally:
        # Moving a newly built app back does not discard any generated evidence.
        relocated.rename(app)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def member_summary(root: Path) -> dict:
    digest = hashlib.sha256()
    files = size = links = 0
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            links += 1
            record = [name, "symlink", os.readlink(path)]
        elif path.is_file():
            files += 1
            size += path.stat().st_size
            record = [name, path.stat().st_mode & 0o777, sha256(path)]
        else:
            continue
        digest.update((json.dumps(record, ensure_ascii=True) + "\n").encode())
    return {
        "files": files,
        "symlinks": links,
        "size_bytes": size,
        "sha256_tree": digest.hexdigest(),
    }


def native_build(
    workspace: Path, app: Path, node: str, sharp: str, client: Path, bundle_id: str
) -> Path:
    build = runpy.run_path(str(Path(__file__).with_name("build.py")))["build"]
    return build(workspace, app, node, sharp, client=client, chat_url=CHAT_URL, bundle_id=bundle_id)


def package(
    workspace: Path,
    output_dir: Path,
    node: str,
    sharp: str,
    *,
    client: Path | None = None,
    python_home: Path | None = None,
    site_packages: Path | None = None,
    bundle_id: str = "local.codeconnect.menubar",
) -> dict:
    workspace = workspace.resolve()
    output_dir = output_dir.absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise PackageError("Output already exists; choose a new --output-dir.")
    if sys.platform != "darwin" or platform.machine() != "arm64" or sys.version_info[:2] != (3, 11):
        raise PackageError("Packaging requires Apple Silicon macOS and the locked Python 3.11 env.")
    python_home = (python_home or Path(sys.base_prefix)).absolute()
    site_packages = (site_packages or Path(sysconfig.get_paths()["purelib"])).resolve()
    client = (client or workspace / ".artifacts/tools/tunnel-client-v0.0.15/extracted").absolute()
    client_root, _ = client_inventory(client)
    distributions = runtime_distributions(workspace, site_packages)
    metadata = project_metadata(workspace)
    for tool in ("codesign", "ditto", "hdiutil", "lipo", "otool", "install_name_tool"):
        if shutil.which(tool) is None:
            raise PackageError(f"Required macOS build tool is missing: {tool}.")
    output_dir.mkdir(parents=True)
    image_root = output_dir / "image-root"
    app = image_root / "Colink.app"
    native_build(workspace, app, node, sharp, client_root / "tunnel-client", bundle_id)
    bundle_resources(
        workspace, app / "Contents/Resources", python_home, site_packages, client, distributions
    )
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info["CFBundleShortVersionString"] != metadata["version"]:
        raise PackageError("Native app version differs from pyproject.toml.")
    validate_links(app)
    # The standalone dylib's LC_ID_DYLIB can contain the uv installation path.
    # Rewrite only the new bundle's copy before signing, never the source home.
    run_tool(
        [
            "install_name_tool",
            "-id",
            "@rpath/libpython3.11.dylib",
            str(app / "Contents/Resources/python/lib/libpython3.11.dylib"),
        ]
    )
    sign_app(app)
    verify_relocation(app, output_dir)
    # The only deliberate external link is the drag-to-install target, outside
    # the signed application and inside the DMG staging folder.
    (image_root / "Applications").symlink_to("/Applications", target_is_directory=True)
    archive = output_dir / "Colink-macos-arm64.zip"
    disk_image = output_dir / "Colink-macos-arm64.dmg"
    run_tool(
        ["ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", str(app), str(archive)]
    )
    run_tool(
        [
            "hdiutil",
            "create",
            "-volname",
            "Colink",
            "-srcfolder",
            str(image_root),
            "-format",
            "UDZO",
            str(disk_image),
        ]
    )
    run_tool(["hdiutil", "verify", str(disk_image)])
    artifacts = {
        path.name: {"size_bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in (archive, disk_image)
    }
    report = {
        "version": metadata["version"],
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "14.0"},
        "members": {
            "Colink.app": member_summary(app),
            **{
                name: member_summary(app / "Contents/Resources" / name)
                for name in (
                    "python",
                    "vendor",
                    "backend",
                    "tunnel-client",
                    "sample_project",
                    "licenses",
                )
            },
        },
        "artifacts": artifacts,
    }
    (output_dir / "SHA256SUMS").write_text(
        "".join(f"{value['sha256']}  {name}\n" for name, value in artifacts.items())
    )
    (output_dir / "package-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="new directory; never overwritten"
    )
    parser.add_argument("--node", default=shutil.which("node") or "node")
    parser.add_argument(
        "--sharp", default=str(Path(__file__).resolve().parent / "node_modules/sharp")
    )
    parser.add_argument(
        "--client", type=Path, help="official extracted directory or tunnel-client binary"
    )
    parser.add_argument("--python-home", type=Path)
    parser.add_argument("--site-packages", type=Path)
    parser.add_argument("--bundle-id", default="local.codeconnect.menubar")
    arguments = parser.parse_args()
    try:
        package(
            arguments.workspace,
            arguments.output_dir,
            arguments.node,
            arguments.sharp,
            client=arguments.client,
            python_home=arguments.python_home,
            site_packages=arguments.site_packages,
            bundle_id=arguments.bundle_id,
        )
    except PackageError as exc:
        parser.exit(1, f"{exc}\n")
    print(
        "Created Colink-macos-arm64.zip, Colink-macos-arm64.dmg, "
        "SHA256SUMS and package-report.json."
    )


if __name__ == "__main__":
    main()

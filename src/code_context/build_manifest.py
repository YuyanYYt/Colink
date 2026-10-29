"""Offline build provenance for source bytes and the bundle that contains them.

Workspace modules are parsed as text, never imported. Git identity is optional,
and is recorded only when the workspace is itself the repository root. Hashes
detect source differences; signature verification remains a separate operation.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "colink-build-manifest.json"
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_FILES = 5000
_IGNORED = {"__pycache__", "node_modules"}
_NATIVE_INPUTS = (
    "macos/build.py",
    "macos/package.py",
    "macos/render-icons.cjs",
    "macos/CodeConnect/Runtime.swift",
    "macos/CodeConnect/StorageLocation.swift",
    "macos/CodeConnect/Panel.swift",
    "macos/CodeConnect/main.swift",
    "macos/assets/logo.svg",
    "macos/assets/menubar.svg",
)


class BuildManifestError(ValueError):
    """A fixed diagnostic without source text, paths or tool output."""


def _read(path: Path) -> tuple[bytes, int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise BuildManifestError("BUILD_MANIFEST_SOURCE_INVALID")
            if before.st_size > _MAX_FILE_BYTES:
                raise BuildManifestError("BUILD_MANIFEST_SOURCE_LIMIT")
            raw = stream.read(_MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())

            def identity(value):
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if identity(before) != identity(after) or len(raw) != before.st_size:
                raise BuildManifestError("BUILD_MANIFEST_SOURCE_CHANGED")
            return raw, before.st_mode
    except OSError:
        raise BuildManifestError("BUILD_MANIFEST_SOURCE_INVALID") from None


def _inventory(root: Path) -> tuple[list[dict], dict[str, bytes], dict[str, int]]:
    if root.is_symlink() or not root.is_dir():
        raise BuildManifestError("BUILD_MANIFEST_SOURCE_INVALID")
    bodies, modes = {}, {}
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(
            name for name in names if name not in _IGNORED and not name.startswith(".")
        )
        if any((Path(directory) / name).is_symlink() for name in names):
            raise BuildManifestError("BUILD_MANIFEST_SOURCE_INVALID")
        for name in sorted(files):
            if not name.endswith(".py") or name.startswith("."):
                continue
            path = Path(directory) / name
            raw, mode = _read(path)
            total += len(raw)
            if len(bodies) >= _MAX_FILES or total > _MAX_SOURCE_BYTES:
                raise BuildManifestError("BUILD_MANIFEST_SOURCE_LIMIT")
            relative = path.relative_to(root).as_posix()
            bodies[relative], modes[relative] = raw, mode
    records = [
        {"path": path, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for path, raw in sorted(bodies.items())
    ]
    return records, bodies, modes


def _static(node, names):
    if isinstance(node, ast.Constant) and isinstance(node.value, str | int):
        return node.value
    if isinstance(node, ast.Name) and names.get(node.id) is not None:
        return names[node.id]
    if isinstance(node, ast.Attribute):
        if ast.dump(node.value) == ast.dump(ast.parse("sys.version_info", mode="eval").body):
            if node.attr in {"major", "minor"}:
                return getattr(sys.version_info, node.attr)
    if isinstance(node, ast.JoinedStr):
        return "".join(str(_static(item, names)) for item in node.values)
    if isinstance(node, ast.FormattedValue) and node.conversion == -1 and node.format_spec is None:
        return _static(node.value, names)
    if isinstance(node, ast.Set | ast.List | ast.Tuple):
        values = []
        for item in node.elts:
            if isinstance(item, ast.Starred):
                values.extend(_static(item.value, names))
            else:
                values.append(_static(item, names))
        return values
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        return frozenset(_static(node.args[0], names))
    raise ValueError("not a supported static constant")


def _constant(bodies, path, variable, names=None):
    try:
        tree = ast.parse(bodies[path])
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == variable for target in node.targets
            ):
                return _static(node.value, names or {})
    except (KeyError, SyntaxError, ValueError, TypeError, UnicodeError):
        pass
    return None


def _git(workspace: Path, bodies: dict[str, bytes], modes: dict[str, int]) -> dict:
    unknown = {"commit": "unknown", "dirty": None, "dirty_scope": "build_inputs"}

    def git(*arguments):
        return subprocess.check_output(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(workspace),
                *arguments,
            ],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )

    try:
        top = Path(os.fsdecode(git("rev-parse", "--show-toplevel").strip())).resolve()
        if top != workspace:
            return unknown
        commit = git("rev-parse", "--verify", "HEAD").decode().strip()
        algorithm = git("rev-parse", "--show-object-format").decode().strip()
        if algorithm not in {"sha1", "sha256"} or len(commit) != (
            40 if algorithm == "sha1" else 64
        ):
            return unknown
        paths = ["src/code_context", "pyproject.toml", "uv.lock", *_NATIVE_INPUTS]
        expected = {}
        for record in git("ls-tree", "-r", "-z", commit, "--", *paths).split(b"\0"):
            if not record:
                continue
            header, name = record.split(b"\t", 1)
            mode, kind, digest = header.decode().split()
            relative = os.fsdecode(name)
            if (
                relative in bodies
                or relative in _NATIVE_INPUTS
                or (
                    relative.startswith("src/code_context/")
                    and relative.endswith(".py")
                    and not any(
                        part in _IGNORED or part.startswith(".") for part in Path(relative).parts
                    )
                )
            ):
                expected[relative] = (mode, digest)
        actual = {}
        for path, raw in bodies.items():
            digest = hashlib.new(algorithm, f"blob {len(raw)}\0".encode() + raw).hexdigest()
            actual[path] = ("100755" if modes[path] & stat.S_IXUSR else "100644", digest)
        staged = {}
        for record in git("ls-files", "--stage", "-z", "--", *paths).split(b"\0"):
            if not record:
                continue
            header, name = record.split(b"\t", 1)
            mode, digest, stage = header.decode().split()
            relative = os.fsdecode(name)
            if relative in actual or relative in expected:
                if stage != "0":
                    return {"commit": commit, "dirty": True, "dirty_scope": "build_inputs"}
                staged[relative] = (mode, digest)
        return {
            "commit": commit,
            "dirty": actual != expected or staged != expected,
            "dirty_scope": "build_inputs",
        }
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return unknown


def _bundle(info: dict | None) -> dict:
    info = info or {}
    return {
        label: info.get(key, "unknown")
        for label, key in (
            ("identifier", "CFBundleIdentifier"),
            ("short_version", "CFBundleShortVersionString"),
            ("build", "CFBundleVersion"),
            ("package_version", "CoLinkVersion"),
        )
    }


def create_build_manifest(
    workspace: Path,
    *,
    bundled_source: Path | None = None,
    bundle_info: dict | None = None,
) -> dict:
    """Capture actual selected code bytes; never import the selected workspace."""
    workspace = workspace.resolve()
    source = bundled_source if bundled_source is not None else workspace / "src/code_context"
    records, bodies, _ = _inventory(source)
    _, workspace_bodies, workspace_modes = _inventory(workspace / "src/code_context")
    inputs = {f"src/code_context/{key}": value for key, value in workspace_bodies.items()}
    input_modes = {f"src/code_context/{key}": value for key, value in workspace_modes.items()}
    metadata = {}
    for name in ("pyproject.toml", "uv.lock"):
        path = workspace / name
        if path.exists() or path.is_symlink():
            inputs[name], input_modes[name] = _read(path)
            metadata[name] = hashlib.sha256(inputs[name]).hexdigest()
    native_inputs = {}
    for name in _NATIVE_INPUTS:
        path = workspace / name
        if path.exists() or path.is_symlink():
            if any(parent.is_symlink() for parent in path.parents if parent != workspace):
                raise BuildManifestError("BUILD_MANIFEST_SOURCE_INVALID")
            inputs[name], input_modes[name] = _read(path)
            native_inputs[name] = hashlib.sha256(inputs[name]).hexdigest()
    try:
        version = tomllib.loads(inputs.get("pyproject.toml", b"").decode())["project"]["version"]
    except (KeyError, UnicodeError, tomllib.TOMLDecodeError):
        version = "unknown"
    package = _constant(bodies, "__init__.py", "__version__") or "unknown"
    if package != "unknown" and version != "unknown" and package != version:
        raise BuildManifestError("BUILD_MANIFEST_VERSION_MISMATCH")
    declared = (bundle_info or {}).get("CoLinkVersion")
    if declared is not None and package != "unknown" and declared != package:
        raise BuildManifestError("BUILD_MANIFEST_VERSION_MISMATCH")
    short = (bundle_info or {}).get("CFBundleShortVersionString")
    release = re.match(r"^\d+\.\d+\.\d+", version) if isinstance(version, str) else None
    if short is not None and release is not None and short != release[0]:
        raise BuildManifestError("BUILD_MANIFEST_VERSION_MISMATCH")
    parser = _constant(bodies, "intelligence_models.py", "PARSER_VERSION")
    index = _constant(bodies, "intelligence_index.py", "INDEX_VERSION", {"PARSER_VERSION": parser})
    live = _constant(bodies, "live_index.py", "_INDEX_VERSION", {"INDEX_VERSION": index})
    scripts = _constant(bodies, "language_frontends.py", "SCRIPT_LANGUAGES")
    structure = _constant(
        bodies, "language_frontends.py", "STRUCTURE_LANGUAGES", {"SCRIPT_LANGUAGES": scripts}
    )
    flow = _constant(bodies, "language_frontends.py", "VALUE_FLOW_LANGUAGES")
    return {
        "schema_version": 1,
        "source": {
            "kind": "bundled" if bundled_source is not None else "workspace",
            "scope": "code_context Python modules; dependency trees and runtime state excluded",
            "git": _git(workspace, inputs, input_modes),
            "files": records,
            "sha256": hashlib.sha256(
                json.dumps(records, ensure_ascii=True, sort_keys=True).encode()
            ).hexdigest(),
            "matches_workspace": bodies == workspace_bodies,
        },
        "versions": {
            "project": version,
            "package": package,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "parser": parser or "unknown",
            "mirror_index": index or "unknown",
            "live_index": live or "unknown",
        },
        "capabilities": {
            "structure_languages": sorted(structure or []),
            "value_flow_languages": sorted(flow or []),
            "production_backend": "python",
            "rust_enabled": False,
        },
        "bundle": _bundle(bundle_info),
        "metadata_sha256": metadata,
        "native_source_sha256": native_inputs,
    }


def write_build_manifest(
    workspace: Path,
    resources: Path,
    *,
    bundled_source: Path | None = None,
    bundle_info: dict | None = None,
) -> dict:
    manifest = create_build_manifest(
        workspace, bundled_source=bundled_source, bundle_info=bundle_info
    )
    target = resources / MANIFEST_NAME
    if target.is_symlink():
        raise BuildManifestError("BUILD_MANIFEST_SOURCE_INVALID")
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def verify_build_manifest(manifest: dict, source: Path, *, bundle_info: dict | None = None) -> dict:
    """Compare code bytes independently of Git or the manifest producer's imports."""
    if manifest.get("schema_version") != 1:
        raise BuildManifestError("BUILD_MANIFEST_INVALID")
    expected = {}
    try:
        records = manifest["source"]["files"]
        if not isinstance(records, list) or len(records) > _MAX_FILES:
            raise ValueError
        for record in records:
            if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
                raise ValueError
            path = record["path"]
            parsed = PurePosixPath(path)
            if (
                not isinstance(path, str)
                or parsed.is_absolute()
                or ".." in parsed.parts
                or parsed.as_posix() != path
                or path in expected
                or type(record["size"]) is not int
                or record["size"] < 0
                or not isinstance(record["sha256"], str)
                or len(record["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in record["sha256"])
            ):
                raise ValueError
            expected[path] = record
        digest = hashlib.sha256(
            json.dumps(records, ensure_ascii=True, sort_keys=True).encode()
        ).hexdigest()
        if digest != manifest["source"]["sha256"]:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise BuildManifestError("BUILD_MANIFEST_INVALID") from None
    actual = {item["path"]: item for item in _inventory(source)[0]}
    changed = sorted(
        path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
    )
    missing, extra = (
        sorted(expected.keys() - actual.keys()),
        sorted(actual.keys() - expected.keys()),
    )
    bundle_matches = bundle_info is None or manifest.get("bundle") == _bundle(bundle_info)
    return {
        "matches": not (changed or missing or extra) and bundle_matches,
        "changed_count": len(changed),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "changed": changed[:20],
        "missing": missing[:20],
        "extra": extra[:20],
        "bundle_matches": bundle_matches,
    }

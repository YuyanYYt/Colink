"""Bounded Python import-root evidence from authorized, mirrored text only.

These roots map names to existing mirror paths; they never add search paths to
Python, read the host filesystem, execute a project, or widen source access.
"""

import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from code_context.intelligence_models import ParsedFile
from code_context.policy import validate_path

MAX_SOURCE_ROOTS = 32
MAX_ROOT_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class PythonSourceRoots:
    roots: tuple[str, ...] | None = None
    mode: str = "auto"
    diagnostic: str = ""


def _ordered(roots) -> tuple[str, ...]:
    return tuple(sorted(set(roots), key=lambda root: (len(PurePosixPath(root).parts), root)))


def read_root_configuration(content: str | None) -> PythonSourceRoots:
    """A project may explicitly select roots in [tool.colink] in pyproject.toml.

    Invalid metadata limits derived binding, not source reading. No diagnostic
    contains TOML values, exception text, host paths or the supplied source.
    """
    if content is None:
        return PythonSourceRoots()
    try:
        if len(content.encode("utf-8")) > MAX_ROOT_CONFIG_BYTES:
            raise ValueError("configuration budget")
        document = tomllib.loads(content)
        tools = document.get("tool", {})
        if not isinstance(tools, dict) or "colink" not in tools:
            return PythonSourceRoots()
        config = tools["colink"]
        if not isinstance(config, dict):
            raise ValueError("configuration table")
        if "python_source_roots" not in config:
            return PythonSourceRoots()
        roots = config["python_source_roots"]
        if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_SOURCE_ROOTS:
            raise ValueError("root count or type")
        normalized = []
        for root in roots:
            if not isinstance(root, str):
                raise ValueError("root type")
            if root in {"", "."}:
                normalized.append("")
            else:
                normalized.append(validate_path(root))
        return PythonSourceRoots(_ordered(normalized), "configured")
    except (ValueError, RecursionError):
        return PythonSourceRoots((), "invalid", "PYTHON_SOURCE_ROOT_CONFIG_INVALID")


def select_source_roots(
    files: dict[str, ParsedFile], configuration: PythonSourceRoots
) -> PythonSourceRoots:
    if configuration.roots is not None:
        return configuration
    python_paths = {path for path, parsed in files.items() if parsed.language == "python"}
    packages = {
        PurePosixPath(path).parent
        for path in python_paths
        if PurePosixPath(path).name in {"__init__.py", "__init__.pyi"}
        and PurePosixPath(path).parent != PurePosixPath(".")
    }
    imported_tops = {
        imp.module.split(".")[0]
        for parsed in files.values()
        if parsed.language == "python"
        for imp in parsed.imports
        if not imp.level and imp.module
    }
    roots = {""}
    # Preserve the conventional src-layout fallback, including bare modules and
    # namespace packages, but do not strip a real top-level package named src.
    if PurePosixPath("src") not in packages and any(
        PurePosixPath(path).parts[0] == "src" for path in python_paths
    ):
        roots.add("src")
    fallback = _ordered(roots)
    for package in sorted(packages):
        top = package
        while top.parent in packages:
            top = top.parent
        # A package boundary plus an observed absolute import is evidence for a
        # custom root. A missing __init__ alone is not: it may be a namespace.
        if top.name not in imported_tops:
            continue
        root = "" if top.parent == PurePosixPath(".") else str(top.parent)
        roots.add(root)
        if len(roots) > MAX_SOURCE_ROOTS:
            return PythonSourceRoots(fallback, "auto", "PYTHON_SOURCE_ROOT_LIMIT_EXCEEDED")
    return PythonSourceRoots(_ordered(roots), "auto")


def python_modules(path: str, source_roots: tuple[str, ...] = ("", "src")) -> list[str]:
    """All evidenced aliases, least-specific root first; never arbitrary suffixes."""
    names = []
    for root in source_roots:
        try:
            relative = PurePosixPath(path).relative_to(root or ".")
        except ValueError:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts and all(part.isidentifier() for part in parts):
            name = ".".join(parts)
            if name not in names:
                names.append(name)
    return names

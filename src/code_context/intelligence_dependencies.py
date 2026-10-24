"""Dependency provenance from authorized source facts and bounded declarations.

Nothing here imports project modules, inspects installed distributions, resolves
Maven artifacts, follows requirement includes, or retains URLs and source text.
Package catalog matches describe provenance; availability remains unverified.
"""

from __future__ import annotations

import configparser
import json
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import PurePosixPath

from code_context.intelligence_roots import python_modules
from code_context.policy import validate_path

DEPENDENCY_METADATA_VERSION = 1
MAX_DEPENDENCY_MANIFEST_BYTES = 64 * 1024
MAX_DEPENDENCY_MANIFESTS = 64
MAX_DEPENDENCY_DECLARATIONS = 512
MAX_DEPENDENCY_METADATA_BYTES = 64 * 1024

_DISTRIBUTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?=\s|\[|[<>=!~@;]|$)")
_MAVEN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")

# Deliberately finite, explicit aliases. Distribution names and import names are
# not generally interchangeable. A catalog hit also needs a project declaration.
_PYTHON_IMPORTS = {
    "beautifulsoup4": ("bs4",),
    "django": ("django",),
    "fastapi": ("fastapi",),
    "flask": ("flask",),
    "httpx": ("httpx",),
    "mcp": ("mcp",),
    "numpy": ("numpy",),
    "pandas": ("pandas",),
    "pathspec": ("pathspec",),
    "pillow": ("PIL",),
    "pydantic": ("pydantic",),
    "pytest": ("pytest",),
    "python-dateutil": ("dateutil",),
    "pyyaml": ("yaml",),
    "requests": ("requests",),
    "scikit-learn": ("sklearn",),
    "sqlalchemy": ("sqlalchemy",),
    "starlette": ("starlette",),
    "tree-sitter": ("tree_sitter",),
    "tree-sitter-java": ("tree_sitter_java",),
    "uvicorn": ("uvicorn",),
    "watchfiles": ("watchfiles",),
}

# Exact coordinates, not a groupId-to-package heuristic. Starters are omitted:
# their transitive dependencies require a real dependency resolver to establish.
_JAVA_IMPORTS = {
    "com.fasterxml.jackson.core:jackson-annotations": ("com.fasterxml.jackson.annotation",),
    "com.fasterxml.jackson.core:jackson-core": ("com.fasterxml.jackson.core",),
    "com.fasterxml.jackson.core:jackson-databind": ("com.fasterxml.jackson.databind",),
    "com.google.code.gson:gson": ("com.google.gson",),
    "com.google.guava:guava": ("com.google.common",),
    "junit:junit": ("org.junit",),
    "org.apache.commons:commons-lang3": ("org.apache.commons.lang3",),
    "org.junit.jupiter:junit-jupiter-api": ("org.junit.jupiter.api",),
    "org.postgresql:postgresql": ("org.postgresql",),
    "org.slf4j:slf4j-api": ("org.slf4j",),
    "org.springframework:spring-beans": ("org.springframework.beans",),
    "org.springframework:spring-context": (
        "org.springframework.context",
        "org.springframework.stereotype",
    ),
    "org.springframework:spring-core": ("org.springframework.core", "org.springframework.util"),
    "org.springframework:spring-jdbc": ("org.springframework.jdbc",),
    "org.springframework:spring-web": ("org.springframework.web",),
    "org.springframework.boot:spring-boot": ("org.springframework.boot",),
    "org.springframework.boot:spring-boot-autoconfigure": (
        "org.springframework.boot.autoconfigure",
    ),
}

# Public Java SE 17 package families. In particular, javax.servlet,
# javax.annotation (outside processing), javax.xml.bind and javax.xml.ws are not
# silently treated as part of a modern JDK. Catalog membership is not a version
# compatibility or class-existence assertion.
_JAVA_STANDARD_PREFIXES = (
    "java",
    "javax.accessibility",
    "javax.annotation.processing",
    "javax.crypto",
    "javax.imageio",
    "javax.lang.model",
    "javax.management",
    "javax.naming",
    "javax.net",
    "javax.print",
    "javax.script",
    "javax.security.auth",
    "javax.security.sasl",
    "javax.sound",
    "javax.sql",
    "javax.swing",
    "javax.tools",
    "javax.transaction.xa",
    "javax.xml.crypto",
    "javax.xml.datatype",
    "javax.xml.namespace",
    "javax.xml.parsers",
    "javax.xml.stream",
    "javax.xml.transform",
    "javax.xml.validation",
    "javax.xml.xpath",
    "org.ietf.jgss",
    "org.w3c.dom",
    "org.xml.sax",
)


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_dependency_manifest(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in {"pyproject.toml", "setup.cfg", "pom.xml"} or (
        name.startswith("requirements") and name.endswith(".txt")
    )


def parse_dependency_manifest(path: str, content: str | None) -> dict:
    """Extract source-free names from one already authorized, saved document."""
    validate_path(path)
    if not is_dependency_manifest(path):
        raise ValueError("unsupported dependency manifest")
    name = PurePosixPath(path).name
    result = {
        "path": path,
        "language": "java" if name == "pom.xml" else "python",
        "status": "ready",
        "declarations": [],
        "diagnostics": [],
    }

    def diagnose(code):
        result["status"] = "partial"
        if not any(item["code"] == code for item in result["diagnostics"]):
            result["diagnostics"].append({"code": code})

    def add(value, kind):
        if not isinstance(value, str) or not _DISTRIBUTION.fullmatch(value):
            diagnose("DEPENDENCY_DECLARATION_UNSUPPORTED")
            return
        declaration = {"kind": kind, "name": _normalize_distribution(value)}
        if declaration in result["declarations"]:
            return
        if len(result["declarations"]) >= MAX_DEPENDENCY_DECLARATIONS:
            diagnose("DEPENDENCY_DECLARATION_LIMIT_EXCEEDED")
            return
        result["declarations"].append(declaration)

    def requirement(value, kind="python_distribution"):
        if not isinstance(value, str) or not (match := _REQUIREMENT.match(value)):
            diagnose("DEPENDENCY_DECLARATION_UNSUPPORTED")
        else:
            suffix = value[match.end() :].lstrip()
            if suffix.startswith("[") and "]" in suffix:
                suffix = suffix.partition("]")[2].lstrip()
            if suffix.startswith("@"):
                address = suffix[1:].lstrip().lower()
                if address.startswith(("file:", "git+file:", "/", ".")):
                    kind = "python_local_distribution"
            add(match[1], kind)

    if content is None:
        diagnose("DEPENDENCY_MANIFEST_UNAVAILABLE")
        return result
    if len(content.encode("utf-8")) > MAX_DEPENDENCY_MANIFEST_BYTES:
        diagnose("DEPENDENCY_MANIFEST_BUDGET_EXCEEDED")
        return result
    try:
        if name == "pyproject.toml":
            document = tomllib.loads(content)
            project = document.get("project", {})
            if not isinstance(project, dict):
                raise ValueError("project table")
            groups = [project.get("dependencies", [])]
            for key in ("optional-dependencies",):
                extras = project.get(key, {})
                if not isinstance(extras, dict):
                    raise ValueError("dependency groups")
                groups.extend(extras.values())
            development = document.get("dependency-groups", {})
            if not isinstance(development, dict):
                raise ValueError("dependency groups")
            groups.extend(development.values())
            for values in groups:
                if not isinstance(values, list):
                    raise ValueError("dependency list")
                for value in values:
                    requirement(value)
            if "dependencies" in project.get("dynamic", []):
                diagnose("DYNAMIC_DEPENDENCIES_NOT_EVALUATED")
            poetry = document.get("tool", {}).get("poetry", {})
            if not isinstance(poetry, dict):
                raise ValueError("poetry table")
            poetry_groups = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
            extra_poetry = poetry.get("group", {})
            if not isinstance(extra_poetry, dict):
                raise ValueError("poetry groups")
            for group in extra_poetry.values():
                if not isinstance(group, dict):
                    raise ValueError("poetry group")
                poetry_groups.append(group.get("dependencies", {}))
            for values in poetry_groups:
                if not isinstance(values, dict):
                    raise ValueError("poetry dependency table")
                for value, specification in values.items():
                    if value != "python":
                        local = isinstance(specification, dict) and "path" in specification
                        add(value, "python_local_distribution" if local else "python_distribution")
            sources = document.get("tool", {}).get("uv", {}).get("sources", {})
            if not isinstance(sources, dict):
                raise ValueError("uv sources")
            for distribution, specification in sources.items():
                choices = specification if isinstance(specification, list) else [specification]
                if any(
                    isinstance(choice, dict) and ("path" in choice or choice.get("workspace"))
                    for choice in choices
                ) and isinstance(distribution, str):
                    normalized = _normalize_distribution(distribution)
                    for declaration in result["declarations"]:
                        if declaration["name"] == normalized:
                            declaration["kind"] = "python_local_distribution"
        elif name == "setup.cfg":
            document = configparser.ConfigParser(interpolation=None)
            document.read_string(content)
            values = [document.get("options", "install_requires", fallback="")]
            if document.has_section("options.extras_require"):
                values.extend(value for _, value in document.items("options.extras_require"))
            for value in values:
                for line in value.splitlines():
                    if line.strip() and not line.lstrip().startswith("#"):
                        requirement(line)
        elif name == "pom.xml":
            if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
                raise ValueError("XML declaration")
            document = ET.fromstring(content)
            namespace = (
                "{http://maven.apache.org/POM/4.0.0}" if document.tag.startswith("{") else ""
            )
            if document.tag != namespace + "project":
                raise ValueError("Maven project")
            if document.find(namespace + "profiles") is not None:
                diagnose("MAVEN_PROFILES_NOT_EVALUATED")
            for dependency in document.findall(f"{namespace}dependencies/{namespace}dependency"):
                group = dependency.findtext(namespace + "groupId", "").strip()
                artifact = dependency.findtext(namespace + "artifactId", "").strip()
                if not _MAVEN_NAME.fullmatch(group) or not _MAVEN_NAME.fullmatch(artifact):
                    diagnose("DEPENDENCY_DECLARATION_UNSUPPORTED")
                    continue
                value = {"kind": "maven_coordinate", "name": f"{group}:{artifact}"}
                if value not in result["declarations"]:
                    if len(result["declarations"]) >= MAX_DEPENDENCY_DECLARATIONS:
                        diagnose("DEPENDENCY_DECLARATION_LIMIT_EXCEEDED")
                        break
                    result["declarations"].append(value)
        else:
            for line in content.splitlines():
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                if value.startswith(("-r", "--requirement", "-c", "--constraint")):
                    diagnose("REQUIREMENT_INCLUDES_NOT_FOLLOWED")
                elif value.startswith("-"):
                    diagnose("REQUIREMENT_OPTIONS_NOT_EVALUATED")
                else:
                    requirement(value)
    except (
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        ET.ParseError,
        configparser.Error,
    ):
        # Partial or invalid metadata cannot establish provenance for any name.
        result["declarations"] = []
        diagnose("DEPENDENCY_MANIFEST_INVALID")
    return result


def merge_dependency_metadata(manifests: Iterable[dict], *, truncated: bool = False) -> dict:
    """Bound the entire publication, including long paths and diagnostic metadata."""
    result = {
        "version": DEPENDENCY_METADATA_VERSION,
        "manifests": [],
        "partial": False,
        "diagnostics": [],
    }
    declaration_count = 0
    for index, manifest in enumerate(manifests):
        if index >= MAX_DEPENDENCY_MANIFESTS:
            truncated = True
            break
        value = {**manifest, "declarations": list(manifest["declarations"])}
        remaining = max(0, MAX_DEPENDENCY_DECLARATIONS - declaration_count)
        if len(value["declarations"]) > remaining:
            value["declarations"] = value["declarations"][:remaining]
            value["status"] = "partial"
            value["diagnostics"] = [
                *value["diagnostics"],
                {"code": "DEPENDENCY_DECLARATION_LIMIT_EXCEEDED"},
            ]
            truncated = True
        candidate = {**result, "manifests": [*result["manifests"], value]}
        # Reserve enough room for fixed top-level diagnostics added below.
        if (
            len(json.dumps(candidate, ensure_ascii=False).encode("utf-8"))
            > MAX_DEPENDENCY_METADATA_BYTES - 256
        ):
            truncated = True
            break
        result["manifests"].append(value)
        declaration_count += len(value["declarations"])
        result["partial"] |= value["status"] != "ready"
    result["partial"] |= truncated
    if truncated:
        result["diagnostics"] = [{"code": "DEPENDENCY_METADATA_LIMIT_EXCEEDED"}]
    return result


class DependencyClassifier:
    """Conservative provenance labels; no unrecognized import becomes third-party."""

    def __init__(self, files: Iterable[dict], metadata: dict, source_roots: tuple[str, ...]):
        self.metadata = metadata
        self.python_roots = {}
        self.java_packages = {}
        self.declarations_by_directory = {}
        for manifest in metadata.get("manifests", []):
            key = manifest["language"], str(PurePosixPath(manifest["path"]).parent)
            self.declarations_by_directory.setdefault(key, []).extend(
                {**declaration, "path": manifest["path"]}
                for declaration in manifest["declarations"]
            )
        for item in files:
            if item["language"] == "python":
                for name in python_modules(item["path"], source_roots):
                    self.python_roots.setdefault(name.split(".")[0], item["path"])
            elif item["language"] == "java" and item["module"]:
                self.java_packages.setdefault(item["module"], item["path"])

    def _declarations(self, source_path: str, language: str) -> list[dict]:
        for parent in PurePosixPath(source_path).parents:
            key = language, str(parent)
            if key in self.declarations_by_directory:
                return self.declarations_by_directory[key]
        return []

    def classify(self, relation: dict, language: str) -> dict:
        module = relation.get("target_module") or relation["name"]
        top = module.split(".")[0]

        def result(kind, confidence, code, **evidence):
            return {
                "dependency_kind": kind,
                "dependency_confidence": confidence,
                "dependency_evidence": [{"code": code, **evidence}],
            }

        if relation.get("target_path"):
            return result(
                "local_module", "high", "AUTHORIZED_LOCAL_TARGET", path=relation["target_path"]
            )
        if language == "python" and relation["name"].startswith("."):
            return result("local_module", "high", "RELATIVE_LOCAL_IMPORT")
        if language == "python" and top in self.python_roots:
            return result(
                "local_module", "medium", "AUTHORIZED_LOCAL_ROOT", path=self.python_roots[top]
            )
        if language == "java":
            package = module
            while package:
                if package in self.java_packages:
                    return result(
                        "local_module",
                        "medium",
                        "AUTHORIZED_LOCAL_PACKAGE",
                        path=self.java_packages[package],
                    )
                package = package.rpartition(".")[0]
        if language == "python" and top in sys.stdlib_module_names:
            return result(
                "standard_library",
                "high",
                "PYTHON_STDLIB_CATALOG",
                catalog=f"python-{sys.version_info.major}.{sys.version_info.minor}",
            )
        if language == "java" and any(
            _within(module, prefix) for prefix in _JAVA_STANDARD_PREFIXES
        ):
            return result(
                "standard_library", "high", "JAVA_SE_NAMESPACE_CATALOG", catalog="java-se-17"
            )
        declarations = self._declarations(relation["source_path"], language)
        for declaration in sorted(
            declarations, key=lambda item: item["kind"] != "python_local_distribution"
        ):
            name = declaration["name"]
            prefixes = (
                _PYTHON_IMPORTS.get(name, ())
                if language == "python"
                else _JAVA_IMPORTS.get(name, ())
            )
            if any(_within(module, prefix) for prefix in prefixes):
                if declaration["kind"] == "python_local_distribution":
                    return result(
                        "local_module",
                        "medium",
                        "DECLARED_LOCAL_DISTRIBUTION_ALIAS_CATALOG",
                        path=declaration["path"],
                        declaration=name,
                    )
                return result(
                    "third_party",
                    "high",
                    "DECLARED_DEPENDENCY_ALIAS_CATALOG",
                    path=declaration["path"],
                    declaration=name,
                )
        answer = result("unknown", "unknown", "DEPENDENCY_PROVENANCE_UNPROVEN")
        # A same-spelling distribution is useful evidence, but cannot prove an
        # import mapping. Do not silently promote this hint to third_party.
        if language == "python":
            matches = [
                declaration
                for declaration in declarations
                if declaration["name"] == _normalize_distribution(top)
            ]
            if matches:
                answer["dependency_evidence"].append(
                    {
                        "code": "DECLARED_DISTRIBUTION_NAME_CANDIDATE",
                        "path": matches[0]["path"],
                        "declaration": matches[0]["name"],
                    }
                )
        return answer

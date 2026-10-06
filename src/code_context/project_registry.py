"""Local project registration and bounded discovery, with no source-body storage.

Registration and enablement are local control-plane operations, not MCP tools.
Discovery considers markers below a real project only at nested Git boundaries;
an unmarked workspace root is a container, even when locally registered. Local
registration can explicitly split other directories into independent projects.
"""

import hashlib
import json
import math
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from time import monotonic

from code_context.policy import excluded_path, validate_path
from code_context.scanner import ScanError, Scanner, _absolute_path, _identity, _version
from code_context.source_access import SourceAccess, SourceError

_MARKERS = (
    ".git",
    "pyproject.toml",
    "pom.xml",
    "settings.gradle",
    "settings.gradle.kts",
    "build.gradle",
    "build.gradle.kts",
)
_PRUNED = frozenset(
    {
        "vendor",
        "site-packages",
        "bower_components",
        ".gradle",
        ".m2",
        ".cache",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".swiftpm",
        "pods",
        "deriveddata",
        "out",
        "coverage",
        "htmlcov",
        ".hg",
        ".svn",
    }
)
_STATE_FILE = "projects.json"
_STATE_SCHEMA = 2
_MAX_STATE_BYTES = 1024 * 1024


class RegistryError(SourceError):
    """Content-free registry failure, optionally carrying visible candidate IDs."""

    def __init__(self, message: str, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.candidates = candidates


@dataclass(frozen=True, slots=True)
class _Project:
    project_id: str
    relative_root: str
    source_id: str
    display_name: str
    aliases: tuple[str, ...]
    enabled: bool
    name_origin: str


class _RegisteredSource(SourceAccess):
    """Keep retained accessors subject to current local authorization and boundaries."""

    def __init__(self, registry: "ProjectRegistry", project: _Project):
        super().__init__(registry.workspace / project.relative_root)
        self._registry = registry
        self._project_id = project.project_id
        if self.source_id != project.source_id:
            raise RegistryError("PROJECT_SOURCE_CHANGED: register and enable the source locally")

    @contextmanager
    def root_fd(self):
        # Coordinated callers can already hold SourceAccess.lock. Keep one lock order.
        with self.lock, self._registry._lock:
            self._registry._check_workspace()
            project = self._registry._enabled_project(self._project_id)
            if project.source_id != self.source_id:
                raise RegistryError(
                    "PROJECT_SOURCE_CHANGED: register and enable the source locally"
                )
            self.scanner._excluded = self._registry._exclusions(project.relative_root)
            with super().root_fd() as fd:
                yield fd


class ProjectRegistry:
    def __init__(
        self,
        workspace: Path,
        data_dir: Path | None = None,
        max_projects: int = 64,
        max_depth: int = 5,
        max_directories: int = 5000,
        max_seconds: float = 3,
    ) -> None:
        for value in (max_projects, max_directories):
            if type(value) is not int or value < 1:
                raise ValueError("project and directory limits must be positive integers")
        if type(max_depth) is not int or max_depth < 0:
            raise ValueError("max_depth must be a nonnegative integer")
        if (
            isinstance(max_seconds, bool)
            or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds)
            or max_seconds <= 0
        ):
            raise ValueError("max_seconds must be finite and positive")
        try:
            root = _absolute_path(Path(workspace).expanduser())
            if root == Path(root.anchor) or _absolute_path(Path.home()).is_relative_to(root):
                raise RegistryError("INVALID_WORKSPACE: choose a bounded working directory")
            self._workspace = SourceAccess(root)
            self.workspace = self._workspace.root
            self.data_dir = (
                _absolute_path(Path(data_dir).expanduser()) if data_dir is not None else None
            )
        except (OSError, ValueError, SourceError):
            raise RegistryError("INVALID_WORKSPACE: a real bounded directory is required") from None
        if self.data_dir is not None and self.workspace.is_relative_to(self.data_dir):
            raise RegistryError("INVALID_DATA_DIRECTORY: state must not contain the workspace")
        self._max_projects = max_projects
        self._max_depth = max_depth
        self._max_directories = max_directories
        self._max_seconds = max_seconds
        self._projects: dict[str, _Project] = {}
        self._sources: dict[str, _RegisteredSource] = {}
        self._lock = RLock()
        self._state_directory: SourceAccess | None = None
        self._persisted_version: tuple | None = None
        if self.data_dir is not None:
            self._prepare_data_directory()
            self._state_directory = SourceAccess(self.data_dir)
            state = self._read_state()
            if state is None:
                self._save()
            else:
                self._load(state)
                self._refresh()

    def discover(self) -> dict:
        """Inspect bounded directory metadata and register candidates as pending."""
        with self._lock, self._change():
            self._check_workspace()
            started = monotonic()
            reasons: set[str] = set()
            candidates: list[str] = []
            directories = 0

            def timed_out():
                if monotonic() - started >= self._max_seconds:
                    reasons.add("max_seconds")
                    return True
                return False

            def walk(fd, relative, depth, inside_project):
                nonlocal directories
                if timed_out():
                    return
                if directories >= self._max_directories:
                    reasons.add("max_directories")
                    return
                directories += 1
                try:
                    markers = self._markers(fd)
                except OSError:
                    reasons.add("unavailable_directory")
                    return
                registered = any(p.relative_root == relative for p in self._projects.values())
                candidate = (
                    registered or ".git" in markers or (bool(markers) and not inside_project)
                )
                # Selecting an unmarked workspace authorizes its root, but does not
                # turn every later Python/Java project into one of its submodules.
                boundary = bool(markers) or (registered and bool(relative))
                if candidate:
                    try:
                        project = self._candidate(relative, _identity(os.fstat(fd)))
                    except RegistryError as exc:
                        reasons.add(
                            "max_projects"
                            if str(exc).startswith("PROJECT_CAPACITY:")
                            else "unavailable_directory"
                        )
                        return
                    candidates.append(project.project_id)
                children = []
                try:
                    with os.scandir(fd) as iterator:
                        for entry in iterator:
                            if timed_out():
                                return
                            path = f"{relative}/{entry.name}" if relative else entry.name
                            if self._pruned(path) or not entry.is_dir(follow_symlinks=False):
                                continue
                            try:
                                self._relative_root(path)
                            except RegistryError:
                                reasons.add("invalid_directory")
                                continue
                            if depth >= self._max_depth:
                                reasons.add("max_depth")
                                continue
                            if len(children) >= self._max_directories - directories:
                                reasons.add("max_directories")
                                break
                            children.append((entry.name, path, entry.stat(follow_symlinks=False)))
                except OSError:
                    reasons.add("unavailable_directory")
                    return
                for name, path, info in sorted(children):
                    if timed_out():
                        return
                    if directories >= self._max_directories:
                        reasons.add("max_directories")
                        return
                    try:
                        with self._workspace.scanner._directory(fd, name, path, info) as child:
                            walk(child, path, depth + 1, inside_project or boundary)
                    except (OSError, ScanError):
                        reasons.add("unavailable_directory")

            try:
                with self._workspace.root_fd() as fd:
                    walk(fd, "", 0, False)
            except (OSError, SourceError):
                raise RegistryError(
                    "WORKSPACE_CHANGED: reauthorize the workspace locally"
                ) from None
            return {
                "candidates": [self._row(self._projects[p], True) for p in candidates],
                "partial": bool(reasons),
                "reason": ",".join(sorted(reasons)) if reasons else None,
                "directories": directories,
            }

    def register(
        self,
        relative_root: str = "",
        display_name: str | None = None,
        aliases: tuple[str, ...] = (),
        enabled: bool = False,
    ) -> str:
        with self._lock, self._change():
            self._check_workspace()
            relative = self._relative_root(relative_root)
            self._validate_enabled(enabled)
            source = self._probe_root(relative)
            name = self._text(
                display_name if display_name is not None else self._default_name(relative)
            )
            aliases = self._aliases(aliases)
            project_id = self._project_id(relative, source.source_id)
            self._put(
                _Project(
                    project_id,
                    relative,
                    source.source_id,
                    name,
                    aliases,
                    enabled,
                    "custom" if display_name is not None else "auto",
                )
            )
            return project_id

    def set_enabled(self, project_id: str, enabled: bool) -> None:
        with self._lock:
            self._check_workspace()
            self._validate_enabled(enabled)
            project = self._project(project_id)
            if enabled:
                if not self._available(project):
                    with self._change():
                        self._projects[project_id] = replace(project, enabled=False)
                    raise RegistryError(
                        "PROJECT_SOURCE_CHANGED: register the current source locally"
                    )
            with self._change():
                self._projects[project_id] = replace(project, enabled=enabled)

    def list_projects(self, enabled_only: bool = True) -> dict:
        with self._lock:
            self._check_workspace()
            self._validate_enabled(enabled_only)
            available = self._refresh()
            return {
                "projects": [
                    self._row(project, available[project.project_id])
                    for project in sorted(self._projects.values(), key=lambda p: p.relative_root)
                    if project.enabled or not enabled_only
                ]
            }

    def resolve_name(self, name: str) -> str:
        name = self._text(name)
        projects = self.list_projects()["projects"]
        exact = {
            p["project_id"]
            for p in projects
            if name in (p["display_name"], p["qualified_name"], *p["aliases"])
        }
        if len(exact) == 1:
            return next(iter(exact))
        if len(exact) > 1:
            raise RegistryError("AMBIGUOUS_PROJECT: choose a unique project", tuple(sorted(exact)))
        candidates = tuple(
            p["project_id"]
            for p in projects
            if any(
                name.casefold() in value.casefold()
                for value in (p["display_name"], p["qualified_name"], *p["aliases"])
            )
        )
        raise RegistryError("PROJECT_NAME_NOT_FOUND: choose an exact name or alias", candidates)

    def authorized_sources(self) -> dict[str, SourceAccess]:
        with self._lock:
            self._check_workspace()
            self._refresh()
            project_ids = [p.project_id for p in self._projects.values() if p.enabled]
        return {project_id: self.source(project_id) for project_id in project_ids}

    def names(self) -> dict[str, str]:
        return {p["project_id"]: p["display_name"] for p in self.list_projects()["projects"]}

    def source(self, project_id: str) -> SourceAccess:
        with self._lock:
            self._check_workspace()
            project = self._enabled_project(project_id)
            if not self._available(project):
                with self._change():
                    self._projects[project_id] = replace(project, enabled=False)
                raise RegistryError(
                    "PROJECT_SOURCE_CHANGED: register and enable the source locally"
                )
            if project_id not in self._sources:
                self._sources[project_id] = _RegisteredSource(self, project)
            source = self._sources[project_id]
        source.ensure_available()
        return source

    def _check_workspace(self):
        try:
            self._workspace.ensure_available()
        except SourceError:
            raise RegistryError("WORKSPACE_CHANGED: reauthorize the workspace locally") from None
        if self._state_directory is not None:
            try:
                with self._state_directory.root_fd() as directory:
                    self._private(os.fstat(directory), directory=True)
                    info = self._state_info(directory)
                    version = _version(info) if info is not None else None
                    if version != self._persisted_version:
                        raise RegistryError("REGISTRY_STORAGE_CHANGED: reload registry metadata")
            except RegistryError:
                raise
            except (OSError, SourceError):
                raise RegistryError("REGISTRY_STORAGE_CHANGED: reload registry metadata") from None

    @staticmethod
    def _markers(fd):
        markers = []
        for name in _MARKERS:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) or (name == ".git" and stat.S_ISDIR(info.st_mode)):
                markers.append(name)
        return markers

    def _project(self, project_id):
        if not isinstance(project_id, str) or project_id not in self._projects:
            raise RegistryError("UNKNOWN_PROJECT: choose a registered project")
        return self._projects[project_id]

    def _enabled_project(self, project_id):
        project = self._project(project_id)
        if not project.enabled:
            raise RegistryError("PROJECT_NOT_AUTHORIZED: enable the project locally")
        return project

    def _relative_root(self, relative):
        try:
            if not isinstance(relative, str):
                raise ValueError
            if relative:
                validate_path(relative)
                relative.encode("utf-8")
                if self._pruned(relative):
                    raise ValueError
        except (ValueError, UnicodeError):
            raise RegistryError(
                "INVALID_PROJECT_ROOT: use an allowed workspace-relative directory"
            ) from None
        return relative

    def _pruned(self, relative):
        if excluded_path(relative) or any(part.lower() in _PRUNED for part in relative.split("/")):
            return True
        if self.data_dir is not None:
            root = self.workspace / relative
            return root.is_relative_to(self.data_dir)
        return False

    @staticmethod
    def _text(value):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 128
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise RegistryError("INVALID_PROJECT_NAME: use a bounded nonempty name")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise RegistryError("INVALID_PROJECT_NAME: use a UTF-8 name") from None
        return value

    def _aliases(self, aliases):
        if not isinstance(aliases, tuple) or len(aliases) > 16:
            raise RegistryError("INVALID_ALIASES: use at most sixteen aliases")
        return tuple(dict.fromkeys(self._text(alias) for alias in aliases))

    @staticmethod
    def _validate_enabled(enabled):
        if type(enabled) is not bool:
            raise RegistryError("INVALID_ENABLEMENT: use an explicit boolean")

    def _probe_root(self, relative):
        try:
            return SourceAccess(self.workspace / relative)
        except SourceError:
            raise RegistryError(
                "PROJECT_SOURCE_UNAVAILABLE: use a real directory in the workspace"
            ) from None

    def _available(self, project):
        try:
            return self._probe_root(project.relative_root).source_id == project.source_id
        except RegistryError:
            return False

    def _project_id(self, relative, source_id):
        encoded = json.dumps([self._workspace.source_id, relative, source_id]).encode()
        return "p_" + hashlib.sha256(encoded).hexdigest()[:32]

    def _default_name(self, relative):
        name = relative or self.workspace.name
        if len(name) > 128:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
            name = name[:111] + "~" + digest
        return self._text(name)

    def _put(self, project):
        old = next(
            (p for p in self._projects.values() if p.relative_root == project.relative_root), None
        )
        if old is None and len(self._projects) >= self._max_projects:
            raise RegistryError("PROJECT_CAPACITY: registration limit reached")
        if old is not None and old.project_id != project.project_id:
            del self._projects[old.project_id]
            self._sources.pop(old.project_id, None)
        self._projects[project.project_id] = project

    def _candidate(self, relative, expected_identity):
        source = self._probe_root(relative)
        if source.root_identity != expected_identity:
            raise RegistryError("PROJECT_SOURCE_CHANGED: directory changed during discovery")
        project_id = self._project_id(relative, source.source_id)
        if project_id in self._projects:
            return self._projects[project_id]
        project = _Project(
            project_id, relative, source.source_id, self._default_name(relative), (), False, "auto"
        )
        self._put(project)
        return project

    def _exclusions(self, relative):
        root = self.workspace / relative
        exclusions = {
            (self.workspace / p.relative_root).relative_to(root).as_posix()
            for p in self._projects.values()
            if p.relative_root != relative
            and (self.workspace / p.relative_root).is_relative_to(root)
        }
        if self.data_dir is not None and self.data_dir.is_relative_to(root):
            exclusions.add(self.data_dir.relative_to(root).as_posix())
        return exclusions

    def _row(self, project, available):
        return {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "aliases": list(project.aliases),
            "relative_root": project.relative_root,
            # Old schemas cannot distinguish a user-entered basename from a default.
            # Keep it intact; this path-qualified name disambiguates without renaming.
            "qualified_name": self._default_name(project.relative_root)
            if project.relative_root
            else ".",
            "name_origin": project.name_origin,
            "status": "unavailable"
            if not available
            else "available"
            if project.enabled
            else "pending",
            "enabled": project.enabled,
        }

    def _refresh(self):
        available = {}
        with self._change():
            for project_id, project in list(self._projects.items()):
                available[project_id] = self._available(project)
                if project.name_origin == "auto":
                    project = replace(
                        project, display_name=self._default_name(project.relative_root)
                    )
                if not available[project_id] and project.enabled:
                    project = replace(project, enabled=False)
                self._projects[project_id] = project
        return available

    @contextmanager
    def _change(self):
        before = self._projects.copy()
        try:
            yield
            if self._projects != before:
                self._save()
        except BaseException:
            self._projects = before
            raise

    @staticmethod
    def _private(info, *, directory=False):
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or (not directory and info.st_nlink != 1)
        ):
            raise RegistryError(
                "UNSAFE_REGISTRY_STATE: private real directories and files are required"
            )

    def _prepare_data_directory(self):
        opened, links = [], []
        try:
            current = os.open(self.data_dir.anchor, Scanner._directory_flags())
            opened.append(current)
            for name in self.data_dir.parts[1:]:
                try:
                    child = os.open(name, Scanner._directory_flags(), dir_fd=current)
                except FileNotFoundError:
                    os.mkdir(name, 0o700, dir_fd=current)
                    child = os.open(name, Scanner._directory_flags(), dir_fd=current)
                opened.append(child)
                links.append((current, name, os.fstat(child)))
                current = child
            self._private(os.fstat(current), directory=True)
            for parent, name, expected in links:
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(
                    expected
                ):
                    raise RegistryError(
                        "REGISTRY_DIRECTORY_CHANGED: use the original state directory"
                    )
        except RegistryError:
            raise
        except OSError:
            raise RegistryError(
                "UNSAFE_REGISTRY_STATE: a private nofollow directory is required"
            ) from None
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def _state_info(self, fd):
        try:
            info = os.stat(_STATE_FILE, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        self._private(info)
        if info.st_size > _MAX_STATE_BYTES:
            raise RegistryError("REGISTRY_METADATA_LIMIT: registry metadata exceeds its budget")
        return info

    def _read_state(self):
        try:
            with self._state_directory.root_fd() as directory:
                self._private(os.fstat(directory), directory=True)
                before = self._state_info(directory)
                if before is None:
                    return None
                fd = os.open(
                    _STATE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                try:
                    if _version(os.fstat(fd)) != _version(before):
                        raise RegistryError("REGISTRY_STORAGE_CHANGED: reload registry metadata")
                    raw = bytearray()
                    while len(raw) <= _MAX_STATE_BYTES:
                        chunk = os.read(fd, min(65536, _MAX_STATE_BYTES + 1 - len(raw)))
                        if not chunk:
                            break
                        raw.extend(chunk)
                    after = self._state_info(directory)
                    if (
                        len(raw) > _MAX_STATE_BYTES
                        or after is None
                        or _version(os.fstat(fd)) != _version(before)
                        or _version(after) != _version(before)
                    ):
                        raise RegistryError("REGISTRY_STORAGE_CHANGED: reload registry metadata")
                    self._persisted_version = _version(after)
                finally:
                    os.close(fd)
            return json.loads(raw, object_pairs_hook=self._unique_keys)
        except RegistryError:
            raise
        except (OSError, SourceError, ValueError, UnicodeError, RecursionError):
            raise RegistryError(
                "INVALID_REGISTRY_METADATA: cannot safely load registration metadata"
            ) from None

    @staticmethod
    def _unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate registry key")
            result[key] = value
        return result

    def _load(self, state):
        try:
            if not isinstance(state, dict) or set(state) != {
                "schema",
                "workspace",
                "projects",
                "authorized_projects",
            }:
                raise ValueError
            if type(state["schema"]) is not int or state["schema"] not in (1, _STATE_SCHEMA):
                raise RegistryError(
                    "UNKNOWN_REGISTRY_SCHEMA: unsupported registry metadata version"
                )
            if state["workspace"] != {
                "path": str(self.workspace),
                "source_id": self._workspace.source_id,
            }:
                raise RegistryError(
                    "REGISTRY_WORKSPACE_MISMATCH: use the original workspace binding"
                )
            rows = state["projects"]
            if not isinstance(rows, list) or len(rows) > self._max_projects:
                raise ValueError
            roots = set()
            for row in rows:
                keys = {
                    "project_id",
                    "relative_root",
                    "source_id",
                    "display_name",
                    "aliases",
                    "enabled",
                }
                if state["schema"] == _STATE_SCHEMA:
                    keys.add("name_origin")
                if not isinstance(row, dict) or set(row) != keys:
                    raise ValueError
                relative = self._relative_root(row["relative_root"])
                source_id = row["source_id"]
                if not isinstance(source_id, str) or not re.fullmatch(r"[0-9a-f]{64}", source_id):
                    raise ValueError
                project_id = self._project_id(relative, source_id)
                if row["project_id"] != project_id or relative in roots:
                    raise ValueError
                if not isinstance(row["aliases"], list):
                    raise ValueError
                self._validate_enabled(row["enabled"])
                name_origin = row["name_origin"] if state["schema"] == _STATE_SCHEMA else "legacy"
                if name_origin not in ("auto", "custom", "legacy"):
                    raise ValueError
                self._projects[project_id] = _Project(
                    project_id,
                    relative,
                    source_id,
                    self._text(row["display_name"]),
                    self._aliases(tuple(row["aliases"])),
                    row["enabled"],
                    name_origin,
                )
                roots.add(relative)
            authorized = state["authorized_projects"]
            if not isinstance(authorized, list) or authorized != sorted(
                p.project_id for p in self._projects.values() if p.enabled
            ):
                raise ValueError
        except RegistryError:
            raise
        except (ValueError, TypeError, KeyError):
            raise RegistryError("INVALID_REGISTRY_METADATA: invalid registration records") from None

    def _save(self):
        if self._state_directory is None:
            return
        state = {
            "schema": _STATE_SCHEMA,
            "workspace": {"path": str(self.workspace), "source_id": self._workspace.source_id},
            "projects": [
                {
                    "project_id": p.project_id,
                    "relative_root": p.relative_root,
                    "source_id": p.source_id,
                    "display_name": p.display_name,
                    "aliases": list(p.aliases),
                    "enabled": p.enabled,
                    "name_origin": p.name_origin,
                }
                for p in sorted(self._projects.values(), key=lambda p: p.relative_root)
            ],
            "authorized_projects": sorted(
                p.project_id for p in self._projects.values() if p.enabled
            ),
        }
        raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > _MAX_STATE_BYTES:
            raise RegistryError("REGISTRY_METADATA_LIMIT: registry metadata exceeds its budget")
        try:
            with self._state_directory.root_fd() as directory:
                self._private(os.fstat(directory), directory=True)
                existing = self._state_info(directory)
                version = _version(existing) if existing is not None else None
                if version != self._persisted_version:
                    raise RegistryError("REGISTRY_STORAGE_CHANGED: reload registry metadata")
                temporary = _STATE_FILE + ".tmp-" + secrets.token_hex(12)
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    os.fchmod(fd, 0o600)
                    remaining = memoryview(raw)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError("incomplete metadata write")
                        remaining = remaining[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                current = self._state_info(directory)
                if (_version(current) if current is not None else None) != version:
                    raise RegistryError("REGISTRY_STORAGE_CHANGED: reload registry metadata")
                os.replace(temporary, _STATE_FILE, src_dir_fd=directory, dst_dir_fd=directory)
                self._persisted_version = _version(self._state_info(directory))
                os.fsync(directory)
        except RegistryError:
            raise
        except (OSError, SourceError):
            raise RegistryError(
                "REGISTRY_SAVE_FAILED: registration metadata could not be saved"
            ) from None

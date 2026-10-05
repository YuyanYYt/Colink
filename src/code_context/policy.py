"""Shared limits and rules enforced on both sides of synchronization."""

import fnmatch
import re
from pathlib import PurePosixPath

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
# Keep one complete source state atomic; allow JSON escaping/metadata headroom.
# This is a transport ceiling, not a second source-text quota.
MAX_REQUEST_BYTES = 256 * 1024 * 1024
MAX_FILES = 50_000

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "target",
        ".pytest_cache",
        ".ruff_cache",
        ".code-context",
        ".artifacts",
        ".ssh",
        ".aws",
        ".gnupg",
    }
)
EXCLUDED_NAMES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "credentials*",
    "secrets*",
    "service-account*.json",
    "service_account*.json",
    "client_secret*.json",
    "*.p12",
    "*.pfx",
    "*.db",
    "*.sqlite*",
    "*.pyc",
    ".DS_Store",
)

# Deliberately narrow: this is a useful guard, not a claim of complete secret detection.
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
)


def validate_path(path: str) -> str:
    """Accept only a normalized project-relative POSIX file path."""
    if not path or len(path) > 1024 or "\\" in path or ":" in path:
        raise ValueError("invalid relative path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("control characters are not allowed in paths")
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or str(parsed) != path
        or any(p in {".", ".."} for p in path.split("/"))
    ):
        raise ValueError("path must be normalized and remain within the project")
    return path


def excluded_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part.lower() in EXCLUDED_DIRS for part in parts) or any(
        fnmatch.fnmatch(part.lower(), pattern.lower())
        for part in parts
        for pattern in EXCLUDED_NAMES
    )


def content_problem(content: str) -> str | None:
    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError:
        return "invalid UTF-8 text"
    if len(raw) > MAX_FILE_BYTES:
        return f"file exceeds {MAX_FILE_BYTES} bytes"
    if "\x00" in content:
        return "binary content"
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        return "possible credential or private key"
    return None

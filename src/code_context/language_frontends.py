"""Source-language detection and bounded, non-executing parser dispatch.

Recognizing a suffix does not imply that structure or value-flow is supported.
Parsers consume only supplied text; this registry never opens another project.
"""

from pathlib import PurePosixPath

from code_context.intelligence_models import ParsedFile

SCRIPT_LANGUAGES = frozenset({"typescript", "javascript"})
VALUE_FLOW_LANGUAGES = frozenset({"python", "java"})
STRUCTURE_LANGUAGES = frozenset({"python", "java", *SCRIPT_LANGUAGES})
_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".swift": "swift",
    ".rs": "rust",
    ".vue": "vue",
}


def detect_language(path: str) -> str:
    """Detect known source text without advertising parser availability."""
    return _LANGUAGES.get(PurePosixPath(path).suffix.lower(), "unsupported")


def structure_language(path: str) -> str:
    language = detect_language(path)
    return language if language in STRUCTURE_LANGUAGES else "unsupported"


def parse_source(path: str, content: str) -> ParsedFile:
    language = structure_language(path)
    if language == "python":
        from code_context.intelligence_python import parse_python

        return parse_python(path, content)
    if language == "java":
        from code_context.intelligence_java import parse_java

        return parse_java(path, content)
    if language in SCRIPT_LANGUAGES:
        from code_context.intelligence_typescript import parse_script

        return parse_script(path, content, language=language)
    return ParsedFile(path=path, language="unsupported", status="unsupported")

"""Source-language detection and bounded, non-executing parser dispatch.

Recognizing a suffix does not imply that structure or value-flow is supported.
Parsers consume only supplied text; this registry never opens another project.
"""

from pathlib import PurePosixPath

from code_context.intelligence_models import ParsedFile

SCRIPT_LANGUAGES = frozenset({"typescript", "javascript"})
VALUE_FLOW_LANGUAGES = frozenset({"python", "java"})
STRUCTURE_LANGUAGES = frozenset({"python", "java", *SCRIPT_LANGUAGES})
LIVE_SCRIPT_LANGUAGES = frozenset({*SCRIPT_LANGUAGES, "vue"})
LIVE_STRUCTURE_LANGUAGES = frozenset({*STRUCTURE_LANGUAGES, "vue"})
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


def live_structure_language(path: str) -> str:
    if path.lower().endswith((".mts", ".cts")):
        return "typescript"
    language = detect_language(path)
    return language if language in LIVE_STRUCTURE_LANGUAGES else "unsupported"


def parse_live_source(path: str, content: str) -> ParsedFile:
    """Use the current live-workspace fact schema, independently of mirror snapshots."""
    language = live_structure_language(path)
    if language == "vue":
        from code_context.intelligence_vue import parse_vue

        return parse_vue(path, content)
    if language in SCRIPT_LANGUAGES:
        from code_context.intelligence_typescript import parse_script

        return parse_script(path, content, language=language, file_imports=True)
    if language == "python":
        from code_context.intelligence_python import parse_python

        return parse_python(path, content, flow_guards=True)
    if language == "java":
        from code_context.intelligence_java import parse_java

        return parse_java(path, content, flow_guards=True)
    return ParsedFile(path=path, language="unsupported", status="unsupported")


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

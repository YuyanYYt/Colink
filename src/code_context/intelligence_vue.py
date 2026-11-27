"""Read top-level Vue SFC script blocks without loading Vue, Vite, or target code."""

from dataclasses import dataclass, field
from html.parser import HTMLParser

from code_context.intelligence_models import MAX_FILE_NODES, MAX_PARSE_BYTES, ParsedFile, Scope
from code_context.intelligence_typescript import _extract_script, _Limit, _script_result

_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_LANGUAGES = {"js": "javascript", "ts": "typescript"}


@dataclass
class _Block:
    setup: bool
    language: str
    line: int
    column: int
    parts: list[str] = field(default_factory=list)


class _SfcBlocks(HTMLParser):
    """HTML raw-text and quoted attributes determine script boundaries.

    Only direct children of the SFC root are candidates. Template descendants,
    comments, style text, and custom-language blocks cannot introduce scripts.
    """

    def __init__(self, result):
        super().__init__(convert_charrefs=False)
        self.result = result
        self.stack = []
        self.blocks = {}
        self.seen = set()
        self.pending = None
        self.nodes = 0
        self.interpolation = False

    def partial(self, code):
        self.result.status = "partial"
        line, column = self.getpos()
        item = {"code": code, "line": line, "column": column + 1}
        if item not in self.result.diagnostics and len(self.result.diagnostics) < 20:
            self.result.diagnostics.append(item)

    def script(self, attributes):
        attrs = dict(attributes)
        setup = "setup" in attrs
        if setup in self.seen:
            self.blocks.pop(setup, None)
            self.partial("VUE_DUPLICATE_SCRIPT_BLOCK")
            return
        self.seen.add(setup)
        if len(attrs) != len(attributes) or any(key.startswith((":", "v-bind")) for key in attrs):
            self.partial("VUE_SCRIPT_ATTRIBUTES_PARTIAL")
            return
        if "src" in attrs:
            self.partial("VUE_EXTERNAL_SCRIPT_PARTIAL")
            return
        lang = (attrs.get("lang", "js") or "").strip().lower()
        script_type = (attrs.get("type") or "").strip().lower()
        if lang not in _LANGUAGES or script_type not in {
            "",
            "module",
            "application/javascript",
            "text/javascript",
        }:
            self.partial("VUE_SCRIPT_LANGUAGE_UNSUPPORTED")
            return
        if "generic" in attrs:
            self.partial("VUE_SCRIPT_ATTRIBUTES_PARTIAL")
        line, col = self.getpos()
        tag = self.get_starttag_text()
        lines = tag.count("\n")
        column = len(tag.rpartition("\n")[2]) + 1 + (col if lines == 0 else 0)
        self.pending = _Block(setup, _LANGUAGES[lang], line + lines, column)

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > MAX_FILE_NODES or len(self.stack) >= 256:
            raise _Limit
        if not self.stack:
            raw = False
            if tag == "script":
                self.script(attrs)
            elif tag == "template":
                self.partial("VUE_TEMPLATE_NOT_ANALYZED")
                raw = (dict(attrs).get("lang") or "html").lower() != "html"
            elif tag != "style":
                self.partial("VUE_CUSTOM_BLOCK_NOT_ANALYZED")
                raw = True
            if raw and tag not in _VOID and not self.get_starttag_text().endswith("/>"):
                self.set_cdata_mode(tag)
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.partial("VUE_MALFORMED_BLOCK")
            if tag not in self.stack:
                return
            while self.stack[-1] != tag:
                self.stack.pop()
        self.stack.pop()
        if not self.stack and tag == "script" and self.pending is not None:
            self.blocks[self.pending.setup] = self.pending
            self.pending = None

    def handle_data(self, data):
        if self.pending is not None:
            self.pending.parts.append(data)
        elif self.stack and self.stack[0] == "template" and not self.cdata_elem:
            opened, closed = data.rfind("{{"), data.rfind("}}")
            if max(opened, closed) >= 0:
                self.interpolation = opened > closed

    def _markup(self, index, parser):
        # Vue interpolation is template text. A quoted '<script>' or
        # '</template>' inside an interpolation does not open or close a block.
        if self.interpolation:
            close = self.rawdata.find("}}", index)
            if close < 0:
                return len(self.rawdata)
            self.interpolation = False
            return close + 2
        return parser(index)

    def parse_starttag(self, index):
        return self._markup(index, super().parse_starttag)

    def parse_endtag(self, index):
        return self._markup(index, super().parse_endtag)

    def parse_html_declaration(self, index):
        return self._markup(index, super().parse_html_declaration)

    def parse_pi(self, index):
        return self._markup(index, super().parse_pi)


def parse_vue(path: str, content: str) -> ParsedFile:
    result = _script_result(path, content, "vue")
    try:
        if len(content) > MAX_PARSE_BYTES or len(content.encode("utf-8")) > MAX_PARSE_BYTES:
            raise _Limit
        sfc = _SfcBlocks(result)
        sfc.feed(content)
        sfc.close()
        if sfc.stack:
            sfc.partial("VUE_MALFORMED_BLOCK")
        if not sfc.seen:
            sfc.partial("VUE_NO_INLINE_SCRIPT")
        remaining = MAX_FILE_NODES - sfc.nodes
        for block in sfc.blocks.values():
            scope = "<script setup>" if block.setup else ""
            if scope:
                result.scopes.append(Scope(scope, "script_setup"))
            try:
                remaining -= _extract_script(
                    result,
                    "".join(block.parts),
                    block.language,
                    remaining,
                    file_imports=True,
                    line=block.line,
                    column=block.column,
                    scope=scope,
                )
            except (_Limit, MemoryError):
                raise
            except Exception:
                # A broken JS/TS frontend must not suppress the other block.
                result.status = "partial"
                issue = {
                    "code": "VUE_SCRIPT_PARSE_ERROR",
                    "line": block.line,
                    "column": block.column,
                }
                if len(result.diagnostics) < 20:
                    result.diagnostics.append(issue)
    except (_Limit, MemoryError):
        result.status = "resource_limited"
        result.diagnostics = [{"code": "VUE_RESOURCE_LIMIT"}]
    except Exception:
        result.status = "parse_error"
        result.diagnostics = [{"code": "VUE_PARSE_ERROR"}]
    return result

"""Pure, bounded text edits; source access, hashes and authorization live elsewhere.

Only LF and CRLF delimit logical lines. A final newline terminates the last line
without creating a further empty line. Incoming text uses the original's dominant
ending (LF for ties or no endings); untouched text is never reformatted. An initial
UTF-8 BOM is protected, including when its first line or a fragment containing it
is replaced. This module performs no filesystem or persistence operations.
"""

from dataclasses import dataclass

from code_context.policy import MAX_FILE_BYTES
from code_context.source_access import SourceError

MAX_EDIT_BYTES = 256 * 1024
MAX_SNIPPET_CHARS = 4000
_BOM = "\ufeff"
_KEYS = {
    "insert_lines": frozenset({"kind", "line", "position", "text", "expected_context"}),
    "replace_lines": frozenset({"kind", "start_line", "end_line", "old_text", "new_text"}),
    "replace_fragment": frozenset({"kind", "old_text", "new_text"}),
}


class EditError(SourceError):
    """Content-free validation or conflict failure; never includes supplied text."""


@dataclass(frozen=True)
class TextEditResult:
    """Edited content and bounded previews of the actual splice.

    Line numbers refer to the original: the anchor for insertion, the inclusive range
    for line replacement, or the lines touched by the matched fragment. ``before`` and
    ``after`` are each limited to 4000 characters, not complete recovery material.
    Insertion has an empty ``before``; its ``after`` includes any boundary newline.
    """

    content: str
    start_line: int
    end_line: int
    before: str
    after: str


def _validate_text(value, *, limit=None):
    if not isinstance(value, str):
        raise EditError("INVALID_EDIT: text fields must be strings")
    if "\x00" in value or "\r" in value.replace("\r\n", ""):
        raise EditError("INVALID_TEXT: NUL and bare CR are not supported")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise EditError("INVALID_TEXT: text must be UTF-8 encodable") from None
    if limit is not None and size > limit:
        raise EditError("EDIT_SIZE_LIMIT: text field exceeds the edit byte limit")
    return size


def _line_number(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EditError("INVALID_RANGE: line numbers must be positive integers")
    return value


def _range_bounds(original, first, last):
    """Locate only the requested lines, without allocating a whole-file line list."""
    offset, current, start = 0, 1, 0
    while offset < len(original):
        newline = original.find("\n", offset)
        end = newline + 1 if newline >= 0 else len(original)
        if current == first:
            start = offset
        if current == last:
            return start, end
        offset, current = end, current + 1
    raise EditError("INVALID_RANGE: line range is outside the original text")


def _ending(text):
    if text.endswith("\r\n"):
        return "\r\n"
    return "\n" if text.endswith("\n") else ""


def _normalize(text, ending):
    return text.replace("\r\n", "\n").replace("\n", ending)


def _replacement(old, new, ending):
    new = _normalize(new, ending)
    old_ending = _ending(old)
    if old_ending and not new.endswith("\n"):
        new += old_ending
    return new


def _finish(original, start, end, replacement, first, last):
    if start == 0 and end > 0 and original.startswith(_BOM):
        if not replacement.startswith(_BOM):
            replacement = _BOM + replacement
    content = original[:start] + replacement + original[end:]
    if _validate_text(content) > MAX_FILE_BYTES:
        raise EditError("FILE_SIZE_LIMIT: edited content exceeds the file byte limit")
    return TextEditResult(
        content,
        first,
        last,
        original[start:end][:MAX_SNIPPET_CHARS],
        replacement[:MAX_SNIPPET_CHARS],
    )


def apply_text_edit(original: str, edit: dict) -> TextEditResult:
    """Validate and apply exactly one edit, returning text without writing it.

    Schemas are exact; all text fields are UTF-8, NUL/bare-CR free and at most
    256 KiB each before normalization. Integer line numbers reject booleans. The
    complete result, including its BOM and newlines, must fit ``MAX_FILE_BYTES``.

    ``insert_lines`` checks the target line without its ending or initial BOM.
    Empty text is a no-op. Empty files (and BOM-only files) accept line 1 before
    or after without forcing an ending. Else insertion adds only necessary line
    boundaries: before an existing line, or after an unterminated EOF, text never
    sticks to that line. An originally terminated EOF stays terminated.

    ``replace_lines`` uses 1-based inclusive existing lines and requires their
    complete raw text in ``old_text`` (including endings and the BOM at line 1).
    ``replace_fragment`` requires a nonempty, exact, unique occurrence, counting
    overlapping matches. Neither old-text field is normalized. Replacement text
    is normalized; if the old span ends in a newline and the replacement does not,
    its exact original ending is appended, even for an empty replacement. Without
    an old ending no final newline is forced. Only the matched span is changed,
    except that an original leading BOM cannot be removed or moved.
    """
    _validate_text(original)
    if not isinstance(edit, dict):
        raise EditError("INVALID_EDIT: edit must be a dictionary")
    kind = edit.get("kind")
    if not isinstance(kind, str) or kind not in _KEYS or edit.keys() != _KEYS[kind]:
        raise EditError("INVALID_EDIT: unsupported kind or unexpected edit fields")
    for key, value in edit.items():
        if key not in {"line", "start_line", "end_line"}:
            _validate_text(value, limit=MAX_EDIT_BYTES)
    crlf = original.count("\r\n")
    ending = "\r\n" if crlf > original.count("\n") - crlf else "\n"

    if kind == "insert_lines":
        line = _line_number(edit["line"])
        position = edit["position"]
        if position not in {"before", "after"}:
            raise EditError("INVALID_EDIT: insertion position must be before or after")
        if not original:
            if line != 1:
                raise EditError("INVALID_RANGE: empty text only accepts insertion at line 1")
            start, end = 0, 0
        else:
            start, end = _range_bounds(original, line, line)
        target = original[start:end]
        target_ending = _ending(target)
        context = target[: -len(target_ending)] if target_ending else target
        if line == 1:
            context = context.removeprefix(_BOM)
        if context != edit["expected_context"]:
            raise EditError("TEXT_MISMATCH: insertion context does not match")
        offset = start if position == "before" else end
        if offset == 0 and original.startswith(_BOM):
            offset = 1
        text = _normalize(edit["text"], ending)
        if text:
            if position == "after" and not target_ending and original not in {"", _BOM}:
                if not text.startswith("\n") and not text.startswith("\r\n"):
                    text = ending + text
            if not text.endswith("\n"):
                if offset < len(original):
                    text += ending
                elif position == "after" and target_ending:
                    text += target_ending
        return _finish(original, offset, offset, text, line, line)

    old, new = edit["old_text"], edit["new_text"]
    if kind == "replace_lines":
        first, last = _line_number(edit["start_line"]), _line_number(edit["end_line"])
        if first > last:
            raise EditError("INVALID_RANGE: start line must not exceed end line")
        start, end = _range_bounds(original, first, last)
        if original[start:end] != old:
            raise EditError("TEXT_MISMATCH: original line range does not match")
    else:
        if not old:
            raise EditError("INVALID_EDIT: fragment must be nonempty")
        start = original.find(old)
        if start < 0:
            raise EditError("TEXT_MISMATCH: fragment was not found")
        if original.find(old, start + 1) >= 0:
            raise EditError("AMBIGUOUS_EDIT: fragment must match exactly once")
        end = start + len(old)
        first = original.count("\n", 0, start) + 1
        last = original.count("\n", 0, end - 1) + 1
    return _finish(original, start, end, _replacement(old, new, ending), first, last)

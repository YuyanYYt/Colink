"""Exact, bounded text pages, including continuation within very long lines."""


def physical_lines(content: str, *, keepends=False):
    """LF/CRLF editor lines; Unicode separators inside strings are not lines.

    An EOF terminator does not invent an additional empty line. Bare CR remains
    source text (controlled edits separately reject that unsupported ending).
    """
    if not content:
        return []
    pieces = content.split("\n")
    terminated = content.endswith("\n")
    if terminated:
        pieces.pop()
    lines = [
        piece + "\n" if i < len(pieces) - 1 or terminated else piece
        for i, piece in enumerate(pieces)
    ]
    if keepends:
        return lines
    return [
        line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line
        for line in lines
    ]


def source_page(
    content: str,
    start_line: int,
    end_line: int,
    max_chars: int,
    char_offset: int = 0,
    *,
    physical=False,
) -> dict:
    if not 1 <= max_chars <= 50_000 or char_offset < 0:
        raise ValueError("invalid source page character bounds")
    lines = (
        physical_lines(content, keepends=True) if physical else content.splitlines(keepends=True)
    )
    if char_offset and (start_line > len(lines) or char_offset >= len(lines[start_line - 1])):
        raise ValueError("character offset is outside the starting line")
    chunks, remaining, last = [], max_chars, min(end_line, len(lines))
    continuation = None
    for number in range(start_line, min(end_line, len(lines)) + 1):
        offset = char_offset if number == start_line else 0
        text = lines[number - 1][offset:]
        taken = min(remaining, len(text))
        chunks.append(text[:taken])
        remaining -= taken
        last = number
        if taken < len(text):
            continuation = number, offset + taken
            break
        if remaining == 0 and number < min(end_line, len(lines)):
            continuation = number + 1, 0
            break
    has_more = continuation is not None or last < len(lines)
    next_line, next_char = continuation or ((last + 1, 0) if has_more else (None, None))
    return {
        "content": "".join(chunks),
        "start_line": start_line,
        "end_line": last,
        "total_lines": len(lines),
        "has_more": has_more,
        "next_start_line": next_line,
        "next_char_offset": next_char,
        "char_offset": char_offset,
        "content_truncated": continuation is not None,
    }

import pytest

from code_context.source_page import physical_lines, source_page


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\v", "\f", "\x1c"])
def test_source_string_separator_is_not_a_physical_line(separator):
    first = f'value = "first{separator}second"\r\n'
    content = first + "target = 1\n"
    assert physical_lines(content, keepends=True) == [first, "target = 1\n"]
    page = source_page(content, 2, 2, 1000, physical=True)
    assert page["content"] == "target = 1\n" and page["total_lines"] == 2
    # The historical mirror's line convention is not silently changed here.
    assert source_page(content, 1, 10, 1000)["total_lines"] == 3


@pytest.mark.parametrize(
    "content,expected",
    [
        ("", []),
        ("\n", [""]),
        ("\r\n", [""]),
        ("a\n\n", ["a", ""]),
        ("a\nb", ["a", "b"]),
        ("bare\rtext", ["bare\rtext"]),
    ],
)
def test_physical_eof_and_endings_are_exact(content, expected):
    assert physical_lines(content) == expected
    assert "".join(physical_lines(content, keepends=True)) == content


def test_physical_page_continuation_does_not_split_unicode_newline_like_characters():
    content = "a" * 1500 + "\u2028" + "b" * 1500 + "\nlast\n"
    first = source_page(content, 1, 2, 1000, physical=True)
    second = source_page(
        content, first["next_start_line"], 2, 1000, first["next_char_offset"], physical=True
    )
    assert first["total_lines"] == second["total_lines"] == 2
    assert first["next_start_line"] == second["next_start_line"] == 1
    assert first["content"] + second["content"] == content[:2000]

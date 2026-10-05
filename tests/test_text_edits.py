from dataclasses import FrozenInstanceError

import pytest

from code_context.policy import MAX_FILE_BYTES
from code_context.source_access import SourceError
from code_context.text_edits import (
    MAX_EDIT_BYTES,
    MAX_SNIPPET_CHARS,
    EditError,
    TextEditResult,
    apply_text_edit,
)


def insert(line=1, position="before", text="新增", expected_context="甲"):
    return {
        "kind": "insert_lines",
        "line": line,
        "position": position,
        "text": text,
        "expected_context": expected_context,
    }


def lines(start=1, end=1, old="甲\n", new="替换"):
    return {
        "kind": "replace_lines",
        "start_line": start,
        "end_line": end,
        "old_text": old,
        "new_text": new,
    }


def fragment(old="甲", new="替换"):
    return {"kind": "replace_fragment", "old_text": old, "new_text": new}


@pytest.mark.parametrize(
    ("original", "line", "position", "text", "context", "expected"),
    [
        ("甲\n乙\n丙", 1, "before", "新", "甲", "新\n甲\n乙\n丙"),
        ("甲\n乙\n丙", 1, "after", "新", "甲", "甲\n新\n乙\n丙"),
        ("甲\n乙\n丙", 2, "before", "新\n", "乙", "甲\n新\n乙\n丙"),
        ("甲\n乙\n丙", 2, "after", "新\n", "乙", "甲\n乙\n新\n丙"),
        ("甲\n乙\n丙", 3, "before", "新", "丙", "甲\n乙\n新\n丙"),
        ("甲\n乙\n丙", 3, "after", "新", "丙", "甲\n乙\n丙\n新"),
        ("甲\n", 1, "after", "新", "甲", "甲\n新\n"),
        ("甲\n", 1, "after", "新\n", "甲", "甲\n新\n"),
        ("甲", 1, "before", "新\n", "甲", "新\n甲"),
        ("甲", 1, "after", "\n新", "甲", "甲\n新"),
        ("甲", 1, "after", "新\n", "甲", "甲\n新\n"),
        ("甲", 1, "after", "\n", "甲", "甲\n"),
        ("甲", 1, "before", "\n", "甲", "\n甲"),
        ("\n", 1, "before", "新", "", "新\n\n"),
        ("\n", 1, "after", "新", "", "\n新\n"),
        ("甲\n\n乙", 2, "before", "新", "", "甲\n新\n\n乙"),
        ("甲\n\n乙", 2, "after", "新", "", "甲\n\n新\n乙"),
        ("甲\n乙", 1, "after", "新一\n新二", "甲", "甲\n新一\n新二\n乙"),
        ("甲\n乙", 1, "before", "\n新\n", "甲", "\n新\n甲\n乙"),
        ("甲\r\n乙", 1, "after", "新\n行\r\n", "甲", "甲\r\n新\r\n行\r\n乙"),
        ("甲\r\n乙", 2, "after", "\r\n新", "乙", "甲\r\n乙\r\n新"),
        ("甲\r\n", 1, "after", "新", "甲", "甲\r\n新\r\n"),
        ("甲\n", 1, "before", "新\r\n行", "甲", "新\n行\n甲\n"),
        ("甲\r\n乙\n丙\r\n丁", 2, "after", "新\n行", "乙", "甲\r\n乙\n新\r\n行\r\n丙\r\n丁"),
        ("甲\r\n乙\n", 1, "before", "新\r\n行", "甲", "新\n行\n甲\r\n乙\n"),
        ("甲\n乙\n丙\r\n", 3, "after", "新", "丙", "甲\n乙\n丙\r\n新\r\n"),
        ("\ufeff甲\r\n乙", 1, "before", "新", "甲", "\ufeff新\r\n甲\r\n乙"),
        ("\ufeff甲", 1, "after", "新", "甲", "\ufeff甲\n新"),
        ("甲\v乙\u2028丙\n丁", 1, "after", "新", "甲\v乙\u2028丙", "甲\v乙\u2028丙\n新\n丁"),
    ],
)
def test_insert_head_middle_tail_and_exact_boundaries(
    original, line, position, text, context, expected
):
    result = apply_text_edit(original, insert(line, position, text, context))
    assert result.content == expected
    assert (result.start_line, result.end_line) == (line, line)
    assert result.before == ""


@pytest.mark.parametrize("original", ["", "\ufeff"])
@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("text", ["", "中文", "中文\n", "甲\r\n乙"])
def test_insert_empty_or_bom_only_file(original, position, text):
    result = apply_text_edit(original, insert(1, position, text, ""))
    assert result.content == original + text.replace("\r\n", "\n")
    assert result.after == text.replace("\r\n", "\n")


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("original", ["甲", "甲\n", "\ufeff甲\r\n乙"])
def test_empty_insertion_is_a_noop(original, position):
    result = apply_text_edit(original, insert(1, position, "", "甲"))
    assert result == TextEditResult(original, 1, 1, "", "")


def test_insertion_previews_include_only_the_actual_splice():
    result = apply_text_edit("甲", insert(1, "after", "乙", "甲"))
    assert result == TextEditResult("甲\n乙", 1, 1, "", "\n乙")
    result = apply_text_edit("\ufeff甲\n", insert(text="乙"))
    assert result == TextEditResult("\ufeff乙\n甲\n", 1, 1, "", "乙\n")


@pytest.mark.parametrize(
    ("original", "start", "end", "old", "new", "expected", "after"),
    [
        ("甲\n乙\n丙", 1, 1, "甲\n", "新", "新\n乙\n丙", "新\n"),
        ("甲\n乙\n丙", 2, 2, "乙\n", "新一\n新二", "甲\n新一\n新二\n丙", "新一\n新二\n"),
        ("甲\n乙\n丙", 3, 3, "丙", "新", "甲\n乙\n新", "新"),
        ("甲\n乙\n", 2, 2, "乙\n", "新", "甲\n新\n", "新\n"),
        ("甲\n乙\n丙", 1, 2, "甲\n乙\n", "新", "新\n丙", "新\n"),
        ("甲\n乙", 1, 2, "甲\n乙", "新", "新", "新"),
        ("甲\n", 1, 1, "甲\n", "", "\n", "\n"),
        ("甲", 1, 1, "甲", "", "", ""),
        ("甲\n乙", 1, 1, "甲\n", "", "\n乙", "\n"),
        ("甲\r\n乙", 1, 1, "甲\r\n", "新\n行", "新\r\n行\r\n乙", "新\r\n行\r\n"),
        ("甲\r\n乙\r\n丙\n", 3, 3, "丙\n", "新", "甲\r\n乙\r\n新\n", "新\n"),
        ("甲\r\n乙\r\n丙\n丁", 3, 3, "丙\n", "新\n", "甲\r\n乙\r\n新\r\n丁", "新\r\n"),
        ("甲\n", 1, 1, "甲\n", "新\r\n", "新\n", "新\n"),
        ("\ufeff甲\n乙", 1, 1, "\ufeff甲\n", "新", "\ufeff新\n乙", "\ufeff新\n"),
        ("\ufeff甲", 1, 1, "\ufeff甲", "\ufeff新", "\ufeff新", "\ufeff新"),
        ("\ufeff甲\r\n", 1, 1, "\ufeff甲\r\n", "", "\ufeff\r\n", "\ufeff\r\n"),
        ("\ufeff", 1, 1, "\ufeff", "新", "\ufeff新", "\ufeff新"),
        ("甲\v乙\n丙", 1, 1, "甲\v乙\n", "新", "新\n丙", "新\n"),
    ],
)
def test_replace_lines_preserves_raw_unmodified_segments(
    original, start, end, old, new, expected, after
):
    result = apply_text_edit(original, lines(start, end, old, new))
    assert result == TextEditResult(expected, start, end, old, after)


@pytest.mark.parametrize(
    ("original", "old", "new", "expected", "start", "end", "after"),
    [
        ("甲 foo 乙", "foo", "中文", "甲 中文 乙", 1, 1, "中文"),
        ("甲\n乙\n丙", "乙", "新", "甲\n新\n丙", 2, 2, "新"),
        ("甲\n乙\n丙", "甲\n乙\n", "新", "新\n丙", 1, 2, "新\n"),
        ("甲\n乙", "乙", "", "甲\n", 2, 2, ""),
        ("甲\n乙\n", "乙\n", "", "甲\n\n", 2, 2, "\n"),
        ("甲\r\n乙\r\n丙\n丁", "乙", "新\n行", "甲\r\n新\r\n行\r\n丙\n丁", 2, 2, "新\r\n行"),
        ("甲\r\n乙\r\n丙\n", "丙\n", "新", "甲\r\n乙\r\n新\n", 3, 3, "新\n"),
        ("甲\n乙\n丙\r\n丁", "丙\r\n", "新", "甲\n乙\n新\r\n丁", 3, 3, "新\r\n"),
        ("\ufeff甲\n乙", "甲", "新", "\ufeff新\n乙", 1, 1, "新"),
        ("\ufeff甲\n乙", "\ufeff甲\n", "新", "\ufeff新\n乙", 1, 1, "\ufeff新\n"),
        ("\ufeff甲", "\ufeff甲", "\ufeff新", "\ufeff新", 1, 1, "\ufeff新"),
        ("\ufeff", "\ufeff", "", "\ufeff", 1, 1, "\ufeff"),
    ],
)
def test_unique_fragment_only_changes_the_matched_span(
    original, old, new, expected, start, end, after
):
    result = apply_text_edit(original, fragment(old, new))
    assert result == TextEditResult(expected, start, end, old, after)


@pytest.mark.parametrize(
    ("original", "old", "error"),
    [
        ("甲", "", "INVALID_EDIT"),
        ("甲", "乙", "TEXT_MISMATCH"),
        ("甲甲", "甲", "AMBIGUOUS_EDIT"),
        ("aaa", "aa", "AMBIGUOUS_EDIT"),
        ("甲\n甲\n", "甲\n", "AMBIGUOUS_EDIT"),
    ],
)
def test_fragment_requires_exact_nonempty_unique_match(original, old, error):
    with pytest.raises(EditError, match=error):
        apply_text_edit(original, fragment(old))


@pytest.mark.parametrize(
    ("original", "edit"),
    [
        ("甲", insert(expected_context="乙")),
        ("甲\n", insert(expected_context="甲\n")),
        ("\ufeff甲", insert(expected_context="\ufeff甲")),
        ("甲 ", insert(expected_context="甲")),
        ("甲\r\n乙", lines(old="甲\n")),
        ("甲\n乙", lines(old="甲")),
        ("\ufeff甲\n", lines(old="甲\n")),
        ("甲", fragment("甲 ")),
    ],
)
def test_context_and_old_text_are_never_fuzzed_or_normalized(original, edit):
    with pytest.raises(EditError, match="TEXT_MISMATCH"):
        apply_text_edit(original, edit)


@pytest.mark.parametrize("value", [False, True, 0, -1, 1.0, "1", None, [], {}])
@pytest.mark.parametrize("field", ["line", "start_line", "end_line"])
def test_line_numbers_are_real_positive_integers(value, field):
    edit = insert() if field == "line" else lines()
    edit[field] = value
    with pytest.raises(EditError, match="INVALID_RANGE"):
        apply_text_edit("甲\n", edit)


@pytest.mark.parametrize(
    ("original", "edit"),
    [
        ("", insert(2, expected_context="")),
        ("", lines(old="")),
        ("甲", insert(2, expected_context="")),
        ("甲\n", insert(2, expected_context="")),
        ("甲\n乙", lines(2, 1, "")),
        ("甲", lines(1, 2, "甲")),
        ("甲\n", lines(2, 2, "")),
        ("甲", insert(10**100, expected_context="甲")),
        ("甲", lines(1, 10**100, "甲")),
    ],
)
def test_empty_replacements_and_out_of_bounds_ranges_are_rejected(original, edit):
    with pytest.raises(EditError, match="INVALID_RANGE"):
        apply_text_edit(original, edit)


@pytest.mark.parametrize(
    "edit",
    [None, [], (), "insert_lines", 1, False, {}, {"kind": "other"}, {"kind": []}, {"kind": False}],
)
def test_edit_must_be_a_dictionary_with_a_known_kind(edit):
    with pytest.raises(EditError, match="INVALID_EDIT"):
        apply_text_edit("甲\n", edit)


@pytest.mark.parametrize("edit", [insert(), lines(), fragment()])
def test_every_schema_rejects_unknown_keys(edit):
    edit["unexpected"] = "must not be ignored"
    with pytest.raises(EditError, match="INVALID_EDIT"):
        apply_text_edit("甲\n", edit)


@pytest.mark.parametrize("edit", [insert(), lines(), fragment()])
def test_every_schema_rejects_missing_keys(edit):
    for key in edit:
        incomplete = {name: value for name, value in edit.items() if name != key}
        with pytest.raises(EditError, match="INVALID_EDIT"):
            apply_text_edit("甲\n", incomplete)


@pytest.mark.parametrize("position", ["Before", "around", "", None, False, 1, [], {}])
def test_insertion_position_is_strict(position):
    with pytest.raises(EditError, match="INVALID_EDIT"):
        apply_text_edit("甲", insert(position=position))


@pytest.mark.parametrize("value", [None, False, 1, b"text", [], {}])
@pytest.mark.parametrize("factory", [insert, lines, fragment])
def test_all_text_fields_reject_non_strings(value, factory):
    valid = factory()
    for key in valid.keys() - {"kind", "position", "line", "start_line", "end_line"}:
        edit = {**valid, key: value}
        with pytest.raises(EditError, match="INVALID_EDIT"):
            apply_text_edit("甲\n", edit)


@pytest.mark.parametrize("invalid", ["甲\r乙", "甲\x00乙", "甲\ud800乙", "甲\udfff乙"])
def test_original_and_every_incoming_text_field_reject_unsafe_text(invalid):
    with pytest.raises(EditError, match="INVALID_TEXT"):
        apply_text_edit(invalid, fragment("甲"))
    for valid in (insert(), lines(), fragment()):
        for key in valid.keys() - {"kind", "position", "line", "start_line", "end_line"}:
            with pytest.raises(EditError, match="INVALID_TEXT"):
                apply_text_edit("甲\n", {**valid, key: invalid})


def test_fragment_cannot_leave_a_split_crlf_as_a_bare_cr():
    with pytest.raises(EditError, match="INVALID_TEXT"):
        apply_text_edit("甲\r\n乙", fragment("\n乙", "新"))


@pytest.mark.parametrize("factory", [insert, lines, fragment])
def test_text_field_limit_is_in_utf8_bytes_for_every_payload_field(factory):
    valid = factory()
    for key in valid.keys() - {"kind", "position", "line", "start_line", "end_line"}:
        for oversized in ("x" * (MAX_EDIT_BYTES + 1), "中" * (MAX_EDIT_BYTES // 3 + 1)):
            with pytest.raises(EditError, match="EDIT_SIZE_LIMIT"):
                apply_text_edit("甲\n", {**valid, key: oversized})


def test_exact_text_field_limit_and_normalization_expansion_are_allowed():
    payload = "x" * MAX_EDIT_BYTES
    assert apply_text_edit("", insert(text=payload, expected_context="")).content == payload
    context = "x" * MAX_EDIT_BYTES
    assert apply_text_edit(context, insert(text="", expected_context=context)).content == context
    assert apply_text_edit(payload, lines(old=payload, new="")).content == ""
    assert apply_text_edit(payload, fragment(payload, payload)).content == payload
    new = "\n" * MAX_EDIT_BYTES
    result = apply_text_edit("甲\r\n", insert(position="after", text=new))
    assert result.content == "甲\r\n" + "\r\n" * MAX_EDIT_BYTES


def test_complete_file_limit_includes_utf8_bom_and_boundary_newlines():
    original = "\ufeff" + "x" * (MAX_FILE_BYTES - 4)
    result = apply_text_edit(original, fragment("\ufeff", "y"))
    assert len(result.content.encode("utf-8")) == MAX_FILE_BYTES
    with pytest.raises(EditError, match="FILE_SIZE_LIMIT"):
        apply_text_edit(original, fragment("\ufeff", "yy"))
    original = "a\n" + "x" * (MAX_FILE_BYTES - 2)
    with pytest.raises(EditError, match="FILE_SIZE_LIMIT"):
        apply_text_edit(original, insert(1, "after", "b", "a"))
    assert len(apply_text_edit(original, fragment("a", "a")).content.encode()) == MAX_FILE_BYTES


def test_result_limit_not_a_character_count_or_an_original_size_limit():
    original = "a" + "中" * (MAX_FILE_BYTES // 3)
    with pytest.raises(EditError, match="FILE_SIZE_LIMIT"):
        apply_text_edit(original, fragment("a", "中"))
    original = "unique" + "x" * MAX_FILE_BYTES
    result = apply_text_edit(original, fragment("unique", ""))
    assert len(result.content.encode()) == MAX_FILE_BYTES


def test_preview_limits_are_characters_not_bytes_and_results_are_frozen():
    old, new = "甲" * 5000, "乙" * 5000
    result = apply_text_edit(old, fragment(old, new))
    assert result.content == new
    assert result.before == old[:MAX_SNIPPET_CHARS]
    assert result.after == new[:MAX_SNIPPET_CHARS]
    assert len(result.before) == len(result.after) == 4000
    with pytest.raises(FrozenInstanceError):
        result.content = "changed"


def test_edit_error_is_source_error_and_does_not_echo_inputs():
    original, expected = "private original marker", "private incoming marker"
    with pytest.raises(SourceError) as caught:
        apply_text_edit(original, insert(expected_context=expected))
    assert isinstance(caught.value, EditError)
    assert original not in str(caught.value)
    assert expected not in str(caught.value)
    invalid = "private invalid marker\ud800"
    with pytest.raises(EditError) as caught:
        apply_text_edit("甲", fragment(new=invalid))
    assert "private invalid marker" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_edit_dictionary_and_original_are_not_mutated():
    edit, original = insert(text="乙\r\n"), "\ufeff甲\n"
    saved = edit.copy()
    result = apply_text_edit(original, edit)
    assert edit == saved
    assert original == "\ufeff甲\n"
    assert result.content == "\ufeff乙\n甲\n"

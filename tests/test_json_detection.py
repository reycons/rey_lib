"""Detection contract for rey_lib.files.json.

These cases were captured from the viewer implementation that owned this logic
before it moved, and they are a regression net rather than a specification of
what JSON detection ideally ought to be. Detection policy is unchanged by the
move: what the viewer classified before, it classifies now.

The interesting cases are the ones where detection deliberately says no to
valid JSON -- a bare scalar -- and where it deliberately stops looking.
"""

from __future__ import annotations

import pytest

from rey_lib.files.json import looks_like_json, looks_like_jsonl
from rey_lib.viewers.language import classify_text_language


# ---------------------------------------------------------------------------
# looks_like_json
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        "[1, 2]",
        '{\n  "a": 1,\n  "b": [2]\n}',
        "{}",
        "[]",
    ],
)
def test_an_object_or_array_is_json(text: str) -> None:
    assert looks_like_json(text) is True


@pytest.mark.parametrize("text", ['"hello"', "42", "true", "false", "null", "1.5"])
def test_a_bare_scalar_is_valid_json_but_not_detected(text: str) -> None:
    """Deliberate, not an oversight.

    Each of these parses. Detection still refuses them because a file holding
    ``42`` is not usefully shown to a reader as a JSON document, and treating
    it as one would take precedence over every later classifier.
    """
    import json as stdlib_json

    assert stdlib_json.loads(text) is not Ellipsis  # it really does parse
    assert looks_like_json(text) is False


@pytest.mark.parametrize("text", ["{not json}", "{", "[1,", '{"a": }', ""])
def test_malformed_or_empty_text_is_not_json(text: str) -> None:
    assert looks_like_json(text) is False


def test_surrounding_whitespace_does_not_hide_the_opening_brace() -> None:
    """The viewer trimmed before asking; the boundary trims for every caller."""
    assert looks_like_json('   \n {"a": 1}\n  ') is True


def test_several_records_are_not_one_json_document() -> None:
    """Two objects on two lines is JSONL, and is not valid JSON."""
    assert looks_like_json('{"a": 1}\n{"b": 2}') is False


# ---------------------------------------------------------------------------
# looks_like_jsonl
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}\n{"b": 2}',
        "[1]\n[2]",
        '{"a": 1}\n\n\n{"b": 2}',
        '{"a": 1}\n{"b": 2}\n',
    ],
)
def test_two_or_more_record_lines_are_jsonl(text: str) -> None:
    """Blank lines are skipped, so they neither count nor interrupt."""
    assert looks_like_jsonl(text) is True


def test_a_single_record_is_not_jsonl() -> None:
    """One line cannot establish the shape, so JSON detection gets it instead."""
    assert looks_like_jsonl('{"a": 1}') is False
    assert looks_like_json('{"a": 1}') is True


@pytest.mark.parametrize(
    "text",
    [
        "1\n2",
        '"a"\n"b"',
        '{"a": 1}\n2',
        '{"a": 1}\n{bad}',
        "",
    ],
)
def test_scalar_or_malformed_lines_are_not_jsonl(text: str) -> None:
    """Every checked line must be an object or an array, not merely valid."""
    assert looks_like_jsonl(text) is False


def test_detection_stops_after_the_fifth_record() -> None:
    """Bounded on purpose: this runs on previews, not whole files.

    A malformed sixth line is not examined, so the text is still reported as
    JSONL. Preserved exactly as the viewer behaved.
    """
    text = "\n".join([f'{{"a": {index}}}' for index in range(5)] + ["{bad}"])

    assert looks_like_jsonl(text) is True


def test_a_pretty_printed_object_is_json_and_not_jsonl() -> None:
    """The case the ordering in the viewer exists to get right."""
    text = '{\n  "a": 1,\n  "b": 2\n}'

    assert looks_like_jsonl(text) is False
    assert looks_like_json(text) is True


# ---------------------------------------------------------------------------
# The viewer keeps classifying exactly as it did
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"a": 1}', "json"),
        ("[1, 2]", "json"),
        ('{\n  "a": 1,\n  "b": 2\n}', "json"),
        ('{"a": 1}\n{"b": 2}', "jsonl"),
        ('{"a": 1}\n\n{"b": 2}', "jsonl"),
        ("42", "text"),
        ('"hello"', "text"),
        ("", "unknown"),
    ],
)
def test_content_classification_is_unchanged_by_the_move(
    content: str, expected: str
) -> None:
    assert classify_text_language(content=content) == expected


def test_jsonl_is_still_decided_before_json() -> None:
    """Order matters: several object lines must not be reported as json."""
    assert classify_text_language(content='{"a": 1}\n{"b": 2}') == "jsonl"

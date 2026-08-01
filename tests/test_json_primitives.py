"""Boundary contract for rey_lib.files.json.

This is a shared boundary, so the edges matter more than the happy path: what
an absent assertion does, which failures become which error, and whether a
rendering mode leaks whitespace into one that must not have it. Two of these
pin defects the module actually had.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from rey_lib.files import primitive_file_io
from rey_lib.files.json import (
    JsonReadError,
    parse_json_text,
    read_json_file,
    render_json,
    write_json_file,
)


# ---------------------------------------------------------------------------
# 1-2. Type assertion, and null
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("[1, 2]", [1, 2]),
        ('"s"', "s"),
        ("7", 7),
        ("true", True),
        ("null", None),
    ],
)
def test_expect_none_asserts_nothing(text: str, expected: object) -> None:
    assert parse_json_text(text) == expected


def test_null_is_a_value_until_expect_rejects_it() -> None:
    assert parse_json_text("null") is None

    with pytest.raises(JsonReadError, match="must be dict, not NoneType"):
        parse_json_text("null", expect=dict)


def test_a_satisfied_assertion_returns_the_value(tmp_path: Path) -> None:
    assert parse_json_text('{"a": 1}', expect=dict) == {"a": 1}
    assert parse_json_text("[1]", expect=(dict, list)) == [1]


def test_a_failed_assertion_names_both_types() -> None:
    with pytest.raises(JsonReadError, match="must be dict, not list"):
        parse_json_text("[1]", expect=dict)
    with pytest.raises(JsonReadError, match="must be dict or list, not int"):
        parse_json_text("1", expect=(dict, list))


# ---------------------------------------------------------------------------
# 3-4. Which failure becomes which error
# ---------------------------------------------------------------------------

def test_decode_failures_from_both_entry_points_are_json_read_errors(
    tmp_path: Path,
) -> None:
    with pytest.raises(JsonReadError, match=r"Invalid JSON in 'mem' at line 1 column 2"):
        parse_json_text("{bad", source="mem")

    source = tmp_path / "bad.json"
    source.write_text("{bad", encoding="utf-8")
    with pytest.raises(JsonReadError, match="Invalid JSON.*bad.json.*line 1 column 2"):
        read_json_file(source)


def test_a_missing_file_is_a_read_failure_not_a_syntax_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(JsonReadError) as excinfo:
        read_json_file(tmp_path / "missing.json")

    assert "Cannot read" in str(excinfo.value)
    assert "Invalid JSON" not in str(excinfo.value)


def test_undecodable_bytes_are_a_read_failure_not_a_syntax_error(
    tmp_path: Path,
) -> None:
    """Regression: UnicodeDecodeError is not an OSError and once leaked raw.

    Reporting it as malformed JSON would send a caller looking for a bad brace
    in a file that never became text.
    """
    source = tmp_path / "enc.json"
    source.write_bytes(b'\xff\xfe{"a": 1}')

    with pytest.raises(JsonReadError) as excinfo:
        read_json_file(source)

    assert "Cannot read" in str(excinfo.value)
    assert "Invalid JSON" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. Newline
# ---------------------------------------------------------------------------

def test_newline_produces_exactly_one_or_none(tmp_path: Path) -> None:
    target = tmp_path / "n.json"

    write_json_file(target, {"a": 1}, newline=True)
    with_newline = target.read_text(encoding="utf-8")
    write_json_file(target, {"a": 1}, newline=False)
    without = target.read_text(encoding="utf-8")

    assert with_newline.endswith("\n")
    assert not with_newline.endswith("\n\n")
    assert not without.endswith("\n")
    assert with_newline == without + "\n"


# ---------------------------------------------------------------------------
# 6. Atomic writing
# ---------------------------------------------------------------------------

def test_the_temporary_file_is_created_beside_the_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temp file elsewhere would make the replace a cross-device copy."""
    seen: list[str | None] = []
    real_mkstemp = tempfile.mkstemp

    def spy(*args: object, **kwargs: object):
        seen.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(primitive_file_io.tempfile, "mkstemp", spy)
    target = tmp_path / "nested" / "out.json"
    write_json_file(target, {"a": 1})

    assert seen == [str(target.parent)]


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path: Path) -> None:
    target = tmp_path / "keep.json"
    write_json_file(target, {"good": 1})
    before = target.read_text(encoding="utf-8")

    class Unrenderable:
        def __repr__(self) -> str:
            raise RuntimeError("render exploded")

    with pytest.raises(RuntimeError):
        write_json_file(target, {"bad": Unrenderable()})

    assert target.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


# ---------------------------------------------------------------------------
# 7. Rendering modes stay separate
# ---------------------------------------------------------------------------

def test_canonical_carries_no_indentation_or_spacing() -> None:
    rendered = render_json({"b": 1, "a": {"z": 2, "y": 3}}, mode="canonical")

    assert rendered == '{"a":{"y":3,"z":2},"b":1}'
    assert "\n" not in rendered
    assert " " not in rendered


def test_canonical_leaves_non_ascii_literal() -> None:
    """Deliberate: escaping would change hashes two contracts already persist.

    canonical is not a formatting preference. rule_sets and inspection each
    arrived at this representation independently, for identity, and hold
    stored hashes against it.
    """
    assert render_json({"p": "Ünïcode"}, mode="canonical") == '{"p":"Ünïcode"}'
    # The other modes are unaffected and still escape.
    assert render_json({"p": "Ünïcode"}) == '{"p": "\\u00dcn\\u00efcode"}'


# Captured from the two live identity implementations before any migration.
# A change here is a change to persisted identity, never a formatting tidy-up.
CANONICAL_FIXTURES: tuple[tuple[str, object, str, str], ...] = (
    ("ascii_nested", {"b": 1, "a": {"z": 2, "y": [1, 2]}},
     '{"a":{"y":[1,2],"z":2},"b":1}', "1ac1a410f5c8cd26"),
    ("unicode", {"path": "/data/Ünïcode/x.csv", "name": "café"},
     '{"name":"café","path":"/data/Ünïcode/x.csv"}', "5cc55f1815ed16a5"),
    ("cjk", {"col": "字段", "v": "値"}, '{"col":"字段","v":"値"}', "dccf70f311020085"),
    ("null_and_bool", {"n": None, "t": True, "f": False},
     '{"f":false,"n":null,"t":true}', "22e00dc2f7b01420"),
    ("empty", {}, "{}", "44136fa355b3678a"),
    ("list_root", [{"b": 2, "a": 1}, "Ünïcode"], '[{"a":1,"b":2},"Ünïcode"]',
     "d0e7d6cc0d68d34e"),
    ("deep", {"a": {"b": {"c": {"d": "é"}}}}, '{"a":{"b":{"c":{"d":"é"}}}}',
     "dd24bf0103b67cbe"),
)


@pytest.mark.parametrize(
    ("label", "value", "expected_text", "expected_sha"), CANONICAL_FIXTURES
)
def test_canonical_matches_the_persisted_identity_representation(
    label: str,
    value: object,
    expected_text: str,
    expected_sha: str,
) -> None:
    """These bytes and hashes are a contract, not an implementation detail."""
    import hashlib

    rendered = render_json(value, mode="canonical")

    assert rendered == expected_text, label
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16] == expected_sha


def test_each_mode_renders_its_own_shape() -> None:
    value = {"b": 1, "a": 2}

    assert render_json(value) == '{"b": 1, "a": 2}'
    assert render_json(value, mode="compact") == '{"b": 1, "a": 2}'
    assert render_json(value, mode="pretty") == '{\n  "a": 2,\n  "b": 1\n}'


def test_canonical_is_stable_across_key_order() -> None:
    """The property that makes it safe to hash."""
    assert render_json({"a": 1, "b": 2}, mode="canonical") == render_json(
        {"b": 2, "a": 1}, mode="canonical"
    )


def test_a_non_json_native_value_is_stringified_not_raised() -> None:
    assert render_json({"p": Path("/x/y")}) == '{"p": "/x/y"}'


# ---------------------------------------------------------------------------
# 8. Invalid arguments fail explicitly
# ---------------------------------------------------------------------------

def test_an_unknown_render_mode_is_refused() -> None:
    with pytest.raises(JsonReadError, match="Unknown JSON render mode 'fancy'"):
        render_json({}, mode="fancy")  # type: ignore[arg-type]


@pytest.mark.parametrize("expect", ["dict", 42, (), (dict, "list"), object()])
def test_an_unusable_expect_is_refused(expect: object) -> None:
    """Regression: isinstance's TypeError once leaked through this argument."""
    with pytest.raises(JsonReadError, match="expect must be a type or tuple of types"):
        parse_json_text('{"a": 1}', expect=expect)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 9. What the error names
# ---------------------------------------------------------------------------

def test_the_source_is_reported_as_a_string_from_either_entry_point(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bad.json"
    source.write_text("{bad", encoding="utf-8")

    with pytest.raises(JsonReadError) as from_file:
        read_json_file(source)
    with pytest.raises(JsonReadError) as from_text:
        parse_json_text("{bad", source=str(source))

    assert str(source) in str(from_file.value)
    assert str(source) in str(from_text.value)


def test_an_unnamed_source_is_omitted_rather_than_shown_empty() -> None:
    with pytest.raises(JsonReadError) as excinfo:
        parse_json_text("{bad")

    assert "Invalid JSON at line" in str(excinfo.value)
    assert "in ''" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["compact", "pretty", "canonical"])
def test_every_mode_round_trips_through_a_file(tmp_path: Path, mode: str) -> None:
    value = {"b": [1, 2, {"c": None}], "a": "Ünïcode", "t": True}
    target = tmp_path / f"{mode}.json"

    write_json_file(target, value, mode=mode)  # type: ignore[arg-type]

    assert read_json_file(target, expect=dict) == value

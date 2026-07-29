"""Focused tests for the strict generic JSONL file reader."""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.files import JsonlReadError, JsonlRecord, read_jsonl_file


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, *lines: str, name: str = "data.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    return path


def _records(result: list[JsonlRecord]) -> list[dict]:
    return [dict(item.record) for item in result]


def _lines(result: list[JsonlRecord]) -> list[int]:
    return [item.line_number for item in result]


# ---------------------------------------------------------------------------
# Basic reading
# ---------------------------------------------------------------------------


def test_reads_a_valid_jsonl_file(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', '{"a": 2}')
    assert _records(read_jsonl_file(path)) == [{"a": 1}, {"a": 2}]


def test_records_are_returned_in_physical_order(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"n": 3}', '{"n": 1}', '{"n": 2}')
    assert [item["n"] for item in _records(read_jsonl_file(path))] == [3, 1, 2]


def test_line_numbers_are_one_based(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', '{"a": 2}')
    assert _lines(read_jsonl_file(path)) == [1, 2]


def test_blank_lines_are_skipped_but_still_count(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"record_type": "one"}', "", '{"record_type": "two"}')
    result = read_jsonl_file(path)
    assert _lines(result) == [1, 3]
    assert _records(result) == [{"record_type": "one"}, {"record_type": "two"}]


def test_whitespace_only_lines_are_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', "   ", "\t", '{"a": 2}')
    assert _lines(read_jsonl_file(path)) == [1, 4]


def test_full_object_is_returned_without_projection(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1, "b": {"c": 2}, "d": [1, 2]}')
    assert _records(read_jsonl_file(path)) == [{"a": 1, "b": {"c": 2}, "d": [1, 2]}]


def test_empty_file_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert read_jsonl_file(path) == []


def test_blank_only_file_is_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, "", "   ", "")
    assert read_jsonl_file(path) == []


def test_any_suffix_is_accepted(tmp_path: Path) -> None:
    """Valid JSONL content is sufficient; the suffix is not enforced."""
    path = _write(tmp_path, '{"a": 1}', name="manifest.log")
    assert _records(read_jsonl_file(path)) == [{"a": 1}]


def test_a_string_path_is_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}')
    assert _records(read_jsonl_file(str(path))) == [{"a": 1}]


def test_more_than_250_records_are_all_returned(tmp_path: Path) -> None:
    """There is no default record limit."""
    path = _write(tmp_path, *[f'{{"n": {index}}}' for index in range(400)])
    result = read_jsonl_file(path)
    assert len(result) == 400
    assert _lines(result)[-1] == 400


# ---------------------------------------------------------------------------
# Strict parsing
# ---------------------------------------------------------------------------


def test_malformed_json_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', "{not json}")
    with pytest.raises(JsonlReadError):
        read_jsonl_file(path)


def test_error_names_the_file_and_line(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', '{"a": 2}', '{"a": ')
    with pytest.raises(JsonlReadError) as excinfo:
        read_jsonl_file(path)
    message = str(excinfo.value)
    assert str(path) in message
    assert "line 3" in message


def test_parse_error_preserves_the_underlying_cause(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": }')
    with pytest.raises(JsonlReadError) as excinfo:
        read_jsonl_file(path)
    assert excinfo.value.__cause__ is not None


def test_a_json_array_row_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', "[1, 2, 3]")
    with pytest.raises(JsonlReadError, match="expected a JSON object"):
        read_jsonl_file(path)


@pytest.mark.parametrize("scalar", ["1", '"text"', "true", "null"])
def test_a_json_scalar_row_raises(tmp_path: Path, scalar: str) -> None:
    path = _write(tmp_path, scalar)
    with pytest.raises(JsonlReadError, match="expected a JSON object"):
        read_jsonl_file(path)


def test_two_json_values_on_one_line_raise(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1} {"b": 2}')
    with pytest.raises(JsonlReadError):
        read_jsonl_file(path)


def test_no_partial_result_is_returned_after_a_parse_failure(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', "{broken", '{"a": 3}')
    with pytest.raises(JsonlReadError):
        read_jsonl_file(path)


def test_a_later_malformed_line_still_fails_when_filtered_out(tmp_path: Path) -> None:
    """Parsing is strict for every physical line, not only for matching rows."""
    path = _write(tmp_path, '{"keep": true}', "{broken")
    with pytest.raises(JsonlReadError):
        read_jsonl_file(path, filters={"keep": True})


# ---------------------------------------------------------------------------
# File behaviour
# ---------------------------------------------------------------------------


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(JsonlReadError, match="does not exist"):
        read_jsonl_file(tmp_path / "absent.jsonl")


def test_directory_path_raises(tmp_path: Path) -> None:
    with pytest.raises(JsonlReadError, match="not a regular file"):
        read_jsonl_file(tmp_path)


def test_a_non_path_argument_raises(tmp_path: Path) -> None:
    with pytest.raises(JsonlReadError, match="must be a Path or str"):
        read_jsonl_file(42)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_exact_string_filter(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '{"record_type": "inventory", "n": 1}',
        '{"record_type": "classification", "n": 2}',
    )
    result = read_jsonl_file(path, filters={"record_type": "inventory"})
    assert _records(result) == [{"record_type": "inventory", "n": 1}]


def test_exact_numeric_filter(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"n": 1}', '{"n": 2}')
    assert _records(read_jsonl_file(path, filters={"n": 2})) == [{"n": 2}]


def test_exact_boolean_filter(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"ok": true}', '{"ok": false}')
    assert _records(read_jsonl_file(path, filters={"ok": False})) == [{"ok": False}]


def test_boolean_and_integer_filters_stay_distinct(tmp_path: Path) -> None:
    """JSON keeps true and 1 distinct even though Python treats them as equal."""
    path = _write(tmp_path, '{"flag": true}', '{"flag": 1}')
    assert _records(read_jsonl_file(path, filters={"flag": True})) == [{"flag": True}]
    assert _records(read_jsonl_file(path, filters={"flag": 1})) == [{"flag": 1}]


def test_json_null_filter_matches_only_null(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"v": null}', '{"v": 0}', '{"v": ""}')
    assert _records(read_jsonl_file(path, filters={"v": None})) == [{"v": None}]


def test_multiple_filters_must_all_match(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '{"a": 1, "b": 1}',
        '{"a": 1, "b": 2}',
        '{"a": 2, "b": 2}',
    )
    result = read_jsonl_file(path, filters={"a": 1, "b": 2})
    assert _records(result) == [{"a": 1, "b": 2}]


def test_a_missing_filtered_field_does_not_match(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', '{"b": 1}')
    assert _records(read_jsonl_file(path, filters={"a": 1})) == [{"a": 1}]


def test_empty_filters_return_every_record(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', '{"a": 2}')
    assert len(read_jsonl_file(path, filters={})) == 2


def test_filtering_preserves_order_and_line_numbers(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '{"keep": true, "n": 1}',
        '{"keep": false, "n": 2}',
        '{"keep": true, "n": 3}',
    )
    result = read_jsonl_file(path, filters={"keep": True})
    assert _lines(result) == [1, 3]
    assert [item["n"] for item in _records(result)] == [1, 3]


def test_non_mapping_filters_are_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}')
    with pytest.raises(JsonlReadError, match="must be a mapping"):
        read_jsonl_file(path, filters=[("a", 1)])


def test_empty_filter_key_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}')
    with pytest.raises(JsonlReadError, match="non-empty strings"):
        read_jsonl_file(path, filters={"": 1})


# ---------------------------------------------------------------------------
# Field projection
# ---------------------------------------------------------------------------


def test_only_requested_fields_are_returned(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1, "b": 2, "c": 3}')
    assert _records(read_jsonl_file(path, fields=["a", "c"])) == [{"a": 1, "c": 3}]


def test_requested_field_order_is_preserved(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1, "b": 2, "c": 3}')
    projected = read_jsonl_file(path, fields=["c", "a"])[0].record
    assert list(projected) == ["c", "a"]


def test_a_missing_requested_field_stays_absent(tmp_path: Path) -> None:
    """No implicit null is inserted, so absence stays distinguishable."""
    path = _write(tmp_path, '{"a": 1}')
    assert _records(read_jsonl_file(path, fields=["a", "missing"])) == [{"a": 1}]


def test_omitting_fields_returns_the_whole_object(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1, "b": 2}')
    assert _records(read_jsonl_file(path)) == [{"a": 1, "b": 2}]


def test_duplicate_field_names_are_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}')
    with pytest.raises(JsonlReadError, match="requested twice"):
        read_jsonl_file(path, fields=["a", "a"])


def test_empty_field_name_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}')
    with pytest.raises(JsonlReadError, match="non-empty strings"):
        read_jsonl_file(path, fields=["a", ""])


def test_a_bare_string_is_not_a_field_sequence(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}')
    with pytest.raises(JsonlReadError, match="sequence of names"):
        read_jsonl_file(path, fields="a")


def test_projection_does_not_mutate_the_parsed_values(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": {"nested": 1}, "b": 2}')
    projected = read_jsonl_file(path, fields=["a"])[0].record
    assert projected["a"] == {"nested": 1}
    assert _records(read_jsonl_file(path)) == [{"a": {"nested": 1}, "b": 2}]


def test_projection_preserves_line_numbers(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}', "", '{"a": 2}')
    assert _lines(read_jsonl_file(path, fields=["a"])) == [1, 3]


def test_filters_apply_before_projection(tmp_path: Path) -> None:
    """A field may be filtered on without being projected."""
    path = _write(tmp_path, '{"keep": true, "a": 1}', '{"keep": false, "a": 2}')
    result = read_jsonl_file(path, filters={"keep": True}, fields=["a"])
    assert _records(result) == [{"a": 1}]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def test_public_surface_is_exported_from_rey_lib_files() -> None:
    import rey_lib.files as files

    for name in ("read_jsonl_file", "JsonlRecord", "JsonlReadError"):
        assert name in files.__all__, name
        assert hasattr(files, name), name


def test_records_are_immutable(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"a": 1}')
    item = read_jsonl_file(path)[0]
    with pytest.raises(Exception):
        item.line_number = 5  # type: ignore[misc]


def test_the_display_reader_is_untouched() -> None:
    """The strict reader is distinct from the tolerant projection."""
    from rey_lib.logs import read_jsonl_records

    assert read_jsonl_records is not read_jsonl_file

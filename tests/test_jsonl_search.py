"""Focused tests for strict per-record JMESPath JSONL search."""

from __future__ import annotations

import tomllib
from dataclasses import FrozenInstanceError
from pathlib import Path

import jmespath
import pytest

import rey_lib.files.jsonl as jsonl_module
from rey_lib.files.jsonl import (
    JsonlReadError,
    JsonlSearchResult,
    search_jsonl_file,
)


def _write(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "data.jsonl"
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    return path


def test_nested_projection_uses_standard_jmespath(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '{"identity": {"file_id": "one"}}',
        '{"identity": {"file_id": "two"}}',
    )
    results = search_jsonl_file(path, "identity.file_id")
    assert [result.value for result in results] == ["one", "two"]


def test_expression_is_compiled_once_per_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path, '{"n": 1}', '{"n": 2}', '{"n": 3}')
    original_compile = jmespath.compile
    compiled: list[str] = []

    def capture(expression: str):
        compiled.append(expression)
        return original_compile(expression)

    monkeypatch.setattr(jmespath, "compile", capture)
    assert len(search_jsonl_file(path, "n")) == 3
    assert compiled == ["n"]


def test_predicate_results_are_values_not_implicit_row_filters(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"status": "ready"}', '{"status": "waiting"}')
    results = search_jsonl_file(path, "status == 'ready'")
    assert [result.value for result in results] == [True, False]


def test_every_falsey_jmespath_result_is_retained(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '{"value": false}',
        '{"value": 0}',
        '{"value": ""}',
        '{"value": []}',
        '{"value": {}}',
        '{"value": null}',
    )
    results = search_jsonl_file(path, "value")
    assert [result.value for result in results] == [False, 0, "", [], {}, None]
    assert [result.line_number for result in results] == [1, 2, 3, 4, 5, 6]


def test_search_preserves_physical_lines_across_blanks(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"n": 1}', "", "   ", '{"n": 2}')
    results = search_jsonl_file(path, "n")
    assert [result.line_number for result in results] == [1, 4]
    assert [result.value for result in results] == [1, 2]


def test_standard_jmespath_functions_are_available(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"items": [1, 2, 3]}')
    assert search_jsonl_file(path, "length(items)")[0].value == 3


def test_search_does_not_mutate_the_parsed_source_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path, '{"items": [3, 1, 2]}')
    source = {"items": [3, 1, 2]}

    def source_records(_path: Path):
        yield 1, source

    monkeypatch.setattr(jsonl_module, "_iter_jsonl_objects", source_records)
    result = search_jsonl_file(path, "sort(items)")

    assert result == [JsonlSearchResult(line_number=1, value=[1, 2, 3])]
    assert source == {"items": [3, 1, 2]}


@pytest.mark.parametrize("expression", ["", "   ", None, 42])
def test_expression_must_be_a_nonempty_string(
    tmp_path: Path,
    expression: object,
) -> None:
    path = _write(tmp_path, '{"n": 1}')
    with pytest.raises(JsonlReadError, match="non-empty string"):
        search_jsonl_file(path, expression)  # type: ignore[arg-type]


def test_invalid_expression_fails_before_file_iteration(tmp_path: Path) -> None:
    path = _write(tmp_path, "{malformed json}")
    with pytest.raises(JsonlReadError, match="Invalid JMESPath expression"):
        search_jsonl_file(path, "[")


def test_invalid_expression_error_includes_expression_and_parse_problem(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, '{"n": 1}')
    with pytest.raises(JsonlReadError) as excinfo:
        search_jsonl_file(path, "items[")
    message = str(excinfo.value)
    assert "items[" in message
    assert excinfo.value.__cause__ is not None


def test_search_keeps_strict_jsonl_failure_behavior(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"n": 1}', "{broken", '{"n": 3}')
    with pytest.raises(JsonlReadError, match="line 2"):
        search_jsonl_file(path, "n")


def test_search_has_no_implicit_record_limit(tmp_path: Path) -> None:
    path = _write(tmp_path, *[f'{{"n": {index}}}' for index in range(400)])
    results = search_jsonl_file(path, "n")
    assert len(results) == 400
    assert results[-1] == JsonlSearchResult(line_number=400, value=399)


def test_search_results_are_immutable(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"n": 1}')
    result = search_jsonl_file(path, "n")[0]
    with pytest.raises(FrozenInstanceError):
        result.line_number = 2  # type: ignore[misc]


def test_search_api_is_exported_from_rey_lib_files() -> None:
    import rey_lib.files as files

    for name in ("search_jsonl_file", "JsonlSearchResult"):
        assert name in files.__all__
        assert hasattr(files, name)


def test_jmespath_is_a_declared_rey_lib_dependency() -> None:
    project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    assert any(dependency.startswith("jmespath") for dependency in dependencies)

"""Schema files load through the shared JSON boundary.

Both sites read a JSON Schema chosen by an operator -- a sidecar beside a
contract, or the --schema argument to the CLI. Neither has a lenient parsing
policy to preserve, so both go through read_json_file.

This is a deliberate behaviour change, not a transparent refactor. A malformed
schema previously raised a bare JSONDecodeError naming no file, which told an
operator something was wrong at line 1 column 19 of nothing in particular. It
now raises JsonReadError carrying the path. Nothing catches either exception at
any level, so the change is visible only in what the operator is told.

The CLI site is inline in _cmd_run rather than behind a helper, so it is tested
through _cmd_run itself -- the schema load is that function's first statement.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from rey_lib.files.json import JsonReadError
from rey_lib.llm import cli as llm_cli
from rey_lib.llm import runner as llm_runner

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "café": {"type": "number"}},
    "required": ["name"],
}


def _write_sidecar(directory: Path, text: str) -> Path:
    """Write a contract and the sidecar the runner derives from it."""
    contract = directory / "analysis.yaml"
    contract.write_text("name: analysis\n", encoding="utf-8")
    (directory / "analysis.schema.json").write_text(text, encoding="utf-8")
    return contract


def _cli_args(tmp_path: Path, schema: Path | None) -> argparse.Namespace:
    """Build the argument set _cmd_run reads, with a real data file."""
    data = tmp_path / "input.txt"
    data.write_text("some input", encoding="utf-8")
    return argparse.Namespace(
        schema=str(schema) if schema else "",
        data=str(data),
        contract=str(tmp_path / "analysis.yaml"),
        pipeline_id="p",
        stage_id="s",
        provider="test",
        model="m",
        max_tokens=10,
        max_rows=10,
        log="",
        stream=False,
        quiet=True,
    )


# ---------------------------------------------------------------------------
# 1. Successful parsing is unchanged
# ---------------------------------------------------------------------------

def test_a_valid_sidecar_schema_loads_exactly_as_before(tmp_path: Path) -> None:
    contract = _write_sidecar(tmp_path, json.dumps(SCHEMA, indent=2))

    assert llm_runner._load_sidecar_schema(contract) == SCHEMA


def test_a_sidecar_holding_unicode_and_nesting_survives_the_round_trip(
    tmp_path: Path,
) -> None:
    """The file is read as text and then parsed, so encoding is not renegotiated."""
    nested = {"a": {"b": [1, {"c": "Ünïcode"}]}, "字段": None}
    contract = _write_sidecar(tmp_path, json.dumps(nested, ensure_ascii=False))

    assert llm_runner._load_sidecar_schema(contract) == nested


def test_no_sidecar_is_still_none_rather_than_an_error(tmp_path: Path) -> None:
    """An absent sidecar is the normal case, not a failure."""
    contract = tmp_path / "analysis.yaml"
    contract.write_text("name: analysis\n", encoding="utf-8")

    assert llm_runner._load_sidecar_schema(contract) is None


def test_the_cli_passes_the_parsed_schema_through_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end at the real call site, not just the parse."""
    schema_path = tmp_path / "cli.schema.json"
    schema_path.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(request: Any, on_chunk: Any = None) -> Any:
        captured["output_schema"] = request.output_schema
        return argparse.Namespace(status="success", parsed_response=None)

    monkeypatch.setattr(llm_runner, "run", fake_run)

    with pytest.raises(SystemExit):
        llm_cli._cmd_run(_cli_args(tmp_path, schema_path))

    assert captured["output_schema"] == SCHEMA


# ---------------------------------------------------------------------------
# 2. Malformed JSON identifies the source path
# ---------------------------------------------------------------------------

def test_a_malformed_sidecar_names_the_file_that_is_wrong(tmp_path: Path) -> None:
    """The point of the change: the operator is told which file to open."""
    contract = _write_sidecar(tmp_path, '{"type": "object",}')

    with pytest.raises(JsonReadError) as excinfo:
        llm_runner._load_sidecar_schema(contract)

    message = str(excinfo.value)
    assert "analysis.schema.json" in message
    assert "line 1" in message


def test_a_malformed_cli_schema_names_the_file_that_is_wrong(tmp_path: Path) -> None:
    schema_path = tmp_path / "cli.schema.json"
    schema_path.write_text("{not json}", encoding="utf-8")

    with pytest.raises(JsonReadError) as excinfo:
        llm_cli._cmd_run(_cli_args(tmp_path, schema_path))

    assert str(schema_path) in str(excinfo.value)


def test_a_missing_cli_schema_is_a_read_failure_not_a_syntax_error(
    tmp_path: Path,
) -> None:
    """A path typo and a bad file are different problems and read differently."""
    with pytest.raises(JsonReadError) as excinfo:
        llm_cli._cmd_run(_cli_args(tmp_path, tmp_path / "absent.json"))

    assert "Cannot read" in str(excinfo.value)
    assert "Invalid JSON" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. No expect=dict constraint is added
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [("[1, 2]", [1, 2]), ('"schema"', "schema"), ("42", 42), ("null", None)],
)
def test_a_non_object_schema_is_returned_rather_than_refused(
    tmp_path: Path, text: str, expected: object
) -> None:
    """Whether a schema must be an object is a schema decision, not a read one.

    Adding expect=dict here would be a new validation rule smuggled in behind a
    mechanics change. These documents are unusable as schemas, and refusing them
    belongs to whatever validates schemas, not to the function that reads one.
    """
    contract = _write_sidecar(tmp_path, text)

    assert llm_runner._load_sidecar_schema(contract) == expected


def test_the_cli_also_adds_no_type_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_path = tmp_path / "cli.schema.json"
    schema_path.write_text("[1, 2]", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(request: Any, on_chunk: Any = None) -> Any:
        captured["output_schema"] = request.output_schema
        return argparse.Namespace(status="success", parsed_response=None)

    monkeypatch.setattr(llm_runner, "run", fake_run)

    with pytest.raises(SystemExit):
        llm_cli._cmd_run(_cli_args(tmp_path, schema_path))

    assert captured["output_schema"] == [1, 2]

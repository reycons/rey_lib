"""Diagnostics from the existing SQLFluff artifact engine."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from rey_lib.artifacts import lint_artifact
from rey_lib.artifacts.engines.sqlfluff_engine import SqlFluffEngine


def test_lint_artifact_routes_findings_through_sqlfluff(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "sqlfluff.cfg"
    config_path.write_text("[sqlfluff]\ndialect = ansi\n", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def lint(content: str, *, config_path: str):
        calls.append((content, config_path))
        return [{
            "code": "PRS",
            "description": "Found unparsable section",
            "line_no": 2,
            "line_pos": 3,
            "end_line_no": 2,
            "end_line_pos": 8,
        }]

    monkeypatch.setitem(sys.modules, "sqlfluff", SimpleNamespace(lint=lint))
    diagnostics = lint_artifact(
        "SELECT nope",
        "sql",
        {"sql": {
            "enabled": True,
            "engine": "sqlfluff",
            "config_path": str(config_path),
        }},
    )

    assert calls == [("SELECT nope", str(config_path))]
    assert diagnostics == [{
        "message": "Found unparsable section",
        "severity": "error",
        "start_line": 2,
        "start_column": 3,
        "end_line": 2,
        "end_column": 8,
        "code": "PRS",
        "source": "sqlfluff",
    }]


def test_valid_sql_returns_no_diagnostics(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "sqlfluff.cfg"
    config_path.write_text("[sqlfluff]\ndialect = ansi\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "sqlfluff", SimpleNamespace(lint=lambda *_args, **_kwargs: []))

    assert lint_artifact(
        "SELECT 1",
        "sql",
        {"sql": {
            "enabled": True,
            "engine": "sqlfluff",
            "config_path": str(config_path),
        }},
    ) == []


def test_read_json_auto_close_aligns_with_leading_comma(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "sqlfluff.cfg"
    config_path.write_text("[sqlfluff]\ndialect = duckdb\n", encoding="utf-8")
    formatted = (
        "SELECT *\n"
        "FROM read_json_auto(\n"
        "\t'/data/kickouts.jsonl'\n"
        "\t, format = 'newline_delimited'\n"
        ")\n"
    )
    monkeypatch.setitem(
        sys.modules,
        "sqlfluff",
        SimpleNamespace(fix=lambda *_args, **_kwargs: formatted),
    )

    result = SqlFluffEngine().process(
        "SELECT * FROM read_json_auto(...)\n",
        "sql",
        {"config_path": str(config_path)},
    )
    assert result == (
        "SELECT *\n"
        "FROM read_json_auto(\n"
        "\t'/data/kickouts.jsonl'\n"
        "\t, format = 'newline_delimited'\n"
        "\t)\n"
    )
    indented = result.splitlines()[2:]
    assert all(line.startswith("\t") for line in indented)
    assert not any(line.startswith(" ") for line in indented)


def test_alignment_rule_applies_to_every_multiline_function(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "sqlfluff.cfg"
    config_path.write_text("[sqlfluff]\ndialect = duckdb\n", encoding="utf-8")
    formatted = (
        "SELECT coalesce(\n"
        "\tfirst_value\n"
        "\t, second_value\n"
        ")\n"
    )
    monkeypatch.setitem(
        sys.modules,
        "sqlfluff",
        SimpleNamespace(fix=lambda *_args, **_kwargs: formatted),
    )

    assert SqlFluffEngine().process(
        "SELECT coalesce(first_value, second_value)\n",
        "sql",
        {"config_path": str(config_path)},
    ).splitlines()[-1] == "\t)"

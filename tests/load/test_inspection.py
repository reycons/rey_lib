"""What the inspection builders are given, read from the three canonical objects.

Asserted: each selected kind's arguments, that a half-named load stays
half-named without anything being validated, that files are read here and
refused as the load refuses them, and what the Transform says execution sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.load import Source, Target, Transform, inspection_arguments

_EMPTY_SOURCE = {"file": "", "file_type": "", "statement": "", "source_connection": ""}
_EMPTY_TARGET = {"destination": "", "connection": "", "out_file": ""}
_DECLARED = {"columns": [{"source": "a", "name": "a"}]}


def _arguments(
    source: Optional[Source] = None,
    transform: Optional[Transform] = None,
    target: Optional[Target] = None,
) -> dict[str, Any]:
    return inspection_arguments(source or Source(), transform or Transform(), target or Target())


class TestTheSource:

    def test_a_file(self) -> None:
        got = _arguments(Source({"file": "/in.csv", "file-type": "csv"}))

        assert {k: got[k] for k in _EMPTY_SOURCE} == {
            **_EMPTY_SOURCE, "file": "/in.csv", "file_type": "csv",
        }

    def test_a_query(self) -> None:
        got = _arguments(Source({"statement": "select 1", "source-connection": "w"},
                                selected="database"))

        assert {k: got[k] for k in _EMPTY_SOURCE} == {
            **_EMPTY_SOURCE, "statement": "select 1", "source_connection": "w",
        }

    def test_a_sql_file_is_read_into_the_statement(self, tmp_path: Path) -> None:
        held = tmp_path / "q.sql"
        held.write_text("select 2", encoding="utf-8")

        got = _arguments(Source({"sql-file": str(held), "source-connection": "w"},
                                selected="sql_file"))

        assert (got["statement"], got["source_connection"]) == ("select 2", "w")

    @pytest.mark.parametrize("content", [None, "   "])
    def test_a_missing_or_empty_sql_file_is_refused(
        self, tmp_path: Path, content: Optional[str],
    ) -> None:
        held = tmp_path / "q.sql"
        if content is not None:
            held.write_text(content, encoding="utf-8")

        with pytest.raises(ConfigError, match="sql_file"):
            _arguments(Source({"sql-file": str(held)}, selected="sql_file"))

    def test_a_governed_file_gives_nothing_yet(self) -> None:
        got = _arguments(Source({"file-manifest-id": "10"}, selected="manifest"))

        assert {k: got[k] for k in _EMPTY_SOURCE} == _EMPTY_SOURCE


class TestTheTarget:

    def test_a_table(self) -> None:
        got = _arguments(target=Target({"table": "s.t", "connection": "w"}))

        assert {k: got[k] for k in _EMPTY_TARGET} == {
            **_EMPTY_TARGET, "destination": "s.t", "connection": "w",
        }

    def test_a_file(self) -> None:
        got = _arguments(target=Target({"out-file": "/o.csv"}, selected="file"))

        assert {k: got[k] for k in _EMPTY_TARGET} == {**_EMPTY_TARGET, "out_file": "/o.csv"}


class TestAHalfNamedLoad:

    def test_nothing_named_is_every_argument_empty(self) -> None:
        assert _arguments() == {**_EMPTY_SOURCE, **_EMPTY_TARGET, "transform": None}

    def test_nothing_is_validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(_self: Any) -> list[str]:
            raise AssertionError("inspection must not validate")

        for cls in (Source, Transform, Target):
            monkeypatch.setattr(cls, "validate", refuse)

        got = _arguments(
            Source({"source-connection": "w"}, selected="database"),
            Transform(selected="declaration"),
            Target({"connection": "w"}),
        )

        assert got["statement"] == "" and got["destination"] == ""
        assert got["transform"] is None


class TestTheTransform:

    def test_identity_is_none(self) -> None:
        assert _arguments(transform=Transform())["transform"] is None

    def test_an_inline_declaration_is_parsed(self) -> None:
        got = _arguments(transform=Transform({"transform": json.dumps(_DECLARED)}))

        assert got["transform"] == _DECLARED

    def test_a_yaml_file_is_read_here(self, tmp_path: Path) -> None:
        held = tmp_path / "t.yaml"
        held.write_text("columns:\n  - {source: a, name: a}\n", encoding="utf-8")

        got = _arguments(transform=Transform({"transform-file": str(held)}))

        assert got["transform"] == _DECLARED

    def test_a_stored_declaration(self) -> None:
        got = _arguments(transform=Transform({"declaration": _DECLARED}, selected="manifest"))

        assert got["transform"] == _DECLARED

    def test_an_unreadable_declaration_is_refused(self) -> None:
        with pytest.raises(ConfigError):
            _arguments(transform=Transform({"transform": "columns: [unclosed"}))


class TestTheExecutedDeclaration:

    @pytest.mark.parametrize(("values", "kind", "expected"), [
        ({}, "identity", None),
        ({"transform": json.dumps(_DECLARED)}, "declaration", _DECLARED),
        ({"declaration": _DECLARED}, "manifest", _DECLARED),
    ])
    def test_for_each_kind(
        self, values: dict[str, Any], kind: str, expected: Optional[dict[str, Any]],
    ) -> None:
        assert Transform(values, selected=kind).executed_declaration() == expected

    def test_an_incomplete_kind_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="missing"):
            Transform(selected="yaml").executed_declaration()

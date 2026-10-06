"""Preview and source columns, through the canonical objects themselves.

Asserted: an incomplete Source answers nothing; a complete one is resolved,
sampled and shown through the Transform it is paired with; an incomplete
Transform is refused as a load refuses it; what the Transform shows is what
the preview executes (backlog 681); and "there is more" is stated.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import ConfigError

from rey_lib.load import Source, Transform
from rey_lib.load.execution import preview_selected, source_columns


@pytest.fixture()
def csv_file(tmp_path: Path) -> Path:
    held = tmp_path / "in.csv"
    held.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")
    return held


def _renaming() -> Transform:
    return Transform(
        {"transform": json.dumps({"columns": [{"source": "a", "name": "a_out"}]})},
        selected="declaration",
    )


class TestSourceColumns:

    def test_a_complete_source_answers_its_own_columns(self, csv_file: Path) -> None:
        assert source_columns(SimpleNamespace(), Source({"file": str(csv_file)})) == ["a", "b"]

    def test_an_incomplete_source_answers_none(self) -> None:
        assert source_columns(SimpleNamespace(), Source(selected="file")) == []


class TestPreviewSelected:

    def test_the_rows_come_through_the_transform(self, csv_file: Path) -> None:
        found = preview_selected(
            SimpleNamespace(), Source({"file": str(csv_file)}), _renaming(), limit=10,
        )

        assert "a_out" in found.columns
        assert [row["a_out"] for row in found.rows] == ["1", "2", "3"]
        assert found.truncated is False

    def test_an_incomplete_transform_is_refused_never_passed_through(self, csv_file: Path) -> None:
        with pytest.raises(ConfigError, match=r"Transform \(declaration\) is missing: transform"):
            preview_selected(
                SimpleNamespace(), Source({"file": str(csv_file)}),
                Transform(selected="declaration"), limit=10,
            )

    def test_an_untouched_grid_executes_as_the_pass_through_it_shows(self, csv_file: Path) -> None:
        shown = Transform(selected="declaration")
        shown.observe_source_columns(["a", "b"])

        found = preview_selected(
            SimpleNamespace(), Source({"file": str(csv_file)}), shown, limit=10,
        )

        assert list(found.columns) == ["a", "b"]
        assert found.rows[0] == {"a": "1", "b": "x"}

    def test_a_partial_declaration_executes_every_column_it_shows(self, csv_file: Path) -> None:
        shown = _renaming()
        shown.observe_source_columns(["a", "b"])

        found = preview_selected(
            SimpleNamespace(), Source({"file": str(csv_file)}), shown, limit=10,
        )

        assert list(found.columns) == ["a_out", "b"]
        assert found.rows[0] == {"a_out": "1", "b": "x"}

    def test_more_than_asked_for_is_stated(self, csv_file: Path) -> None:
        found = preview_selected(
            SimpleNamespace(), Source({"file": str(csv_file)}), Transform(), limit=2,
        )

        assert len(found.rows) == 2
        assert found.truncated is True

    def test_an_incomplete_source_answers_nothing(self) -> None:
        found = preview_selected(
            SimpleNamespace(), Source(selected="file"), Transform(), limit=10,
        )

        assert (found.columns, found.rows) == ((), ())


class TestLoadArguments:

    def test_the_selected_configurations_are_the_invocation(self) -> None:
        from rey_lib.load import Target
        from rey_lib.load.execution import load_arguments

        source = Source({"statement": "SELECT 1", "source-connection": "warehouse"},
                        selected="database")
        transform = _renaming()
        target = Target({"table": "public.orders", "connection": "warehouse"},
                        selected="database")

        arguments = load_arguments(source, transform, target)

        assert arguments["statement"] == "SELECT 1"
        assert arguments["source-connection"] == "warehouse"
        assert arguments["transform"] == transform.configuration()["transform"]
        assert arguments["table"] == "public.orders"
        assert arguments["connection"] == "warehouse"
        assert "file" not in arguments

    def test_unset_values_are_dropped(self) -> None:
        from rey_lib.load import Target
        from rey_lib.load.execution import load_arguments

        arguments = load_arguments(Source(selected="file"), Transform(), Target())

        assert all(value not in (None, "", False) for value in arguments.values())

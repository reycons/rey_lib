"""LinkedLoad: one Source and its Transform, held together (backlog 681).

Asserted: observing the Source tells the Transform its columns; a preview runs
the HELD objects, so an edit to either is what the next preview executes; an
incomplete Transform is refused; and nothing about a preview is kept.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.load import LinkedLoad, Source, Transform


@pytest.fixture()
def csv_file(tmp_path: Path) -> Path:
    held = tmp_path / "in.csv"
    held.write_text("a,b\n1,x\n2,y\n3,z\n", encoding="utf-8")
    return held


def _linked(csv_file: Path) -> LinkedLoad:
    return LinkedLoad(Source({"file": str(csv_file)}), Transform(selected="declaration"))


class TestObservingTheSource:

    def test_the_transform_is_told_the_source_columns(self, csv_file: Path) -> None:
        linked = _linked(csv_file)

        assert linked.observe_source(SimpleNamespace()) is True
        assert linked.transform.source_columns() == ["a", "b"]

    def test_a_source_that_cannot_be_read_tells_it_nothing(self, tmp_path: Path) -> None:
        linked = LinkedLoad(Source({"file": str(tmp_path / "no.unknown")}), Transform())
        linked.transform.observe_source_columns(["kept"])

        assert linked.observe_source(SimpleNamespace()) is False
        assert linked.transform.source_columns() == ["kept"]


class TestPreview:

    def test_it_runs_the_mapping_the_transform_shows(self, csv_file: Path) -> None:
        linked = _linked(csv_file)
        linked.observe_source(SimpleNamespace())

        found = linked.preview(SimpleNamespace(), 10)

        assert list(found.columns) == ["a", "b"]
        assert [row["a"] for row in found.rows] == ["1", "2", "3"]

    def test_an_edit_to_the_held_transform_is_what_the_next_preview_runs(
        self, csv_file: Path,
    ) -> None:
        linked = _linked(csv_file)
        linked.observe_source(SimpleNamespace())
        linked.preview(SimpleNamespace(), 10)

        linked.transform.edit_column(0, "name", "a_out")

        assert list(linked.preview(SimpleNamespace(), 10).columns) == ["a_out", "b"]

    def test_an_edit_to_the_held_source_is_what_the_next_preview_runs(
        self, csv_file: Path, tmp_path: Path,
    ) -> None:
        shorter = tmp_path / "shorter.csv"
        shorter.write_text("a,b\n9,8\n", encoding="utf-8")
        linked = _linked(csv_file)
        linked.observe_source(SimpleNamespace())

        linked.source.update("file", str(shorter))

        assert linked.preview(SimpleNamespace(), 10).rows == ({"a": "9", "b": "8"},)

    def test_an_incomplete_transform_is_refused(self, csv_file: Path) -> None:
        with pytest.raises(ConfigError, match="is missing: transform"):
            _linked(csv_file).preview(SimpleNamespace(), 10)

    def test_nothing_about_a_preview_is_kept(self, csv_file: Path) -> None:
        linked = LinkedLoad(
            Source({"file": str(csv_file)}),
            Transform({"transform": json.dumps({"columns": [{"source": "a", "name": "a"}]})}),
        )
        before = (dict(vars(linked)), linked.transform.declaration())

        linked.preview(SimpleNamespace(), 10)

        assert (dict(vars(linked)), linked.transform.declaration()) == before

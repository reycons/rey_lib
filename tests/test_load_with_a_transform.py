"""A load can be TOLD what to do with its columns.

Before this a load that came from anywhere but a configured file feed applied
nothing at all -- the transform object in its triple was identity and there
was no parameter for a declaration to arrive through. The behaviour existed;
no ad-hoc load could reach it.

    Source object -> TRANSFORM OBJECT -> Destination object

What is asserted is that the declaration reaches the same object the load runs,
that a load given none is exactly what it was, and that what Preview shows is
what Run would write.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.data import ColumnTransform, IdentityTransform
from rey_lib.files.data_file import data_file_for
from rey_lib.load import load_operation

#: Rename one column and parse another. Small, because the rules themselves
#: are proved where they live.
DECLARATION = {
    "columns": [
        {"name": "identifier", "source": "a"},
        {"name": "label", "source": "b"},
    ],
}


@pytest.fixture()
def engine():
    from rey_lib.db.duckdb_utils import open_memory_connection

    with open_memory_connection() as conn:
        conn.execute("CREATE TABLE orders (a INTEGER, b VARCHAR)")
        conn.execute("INSERT INTO orders VALUES (1, 'x'), (2, 'y')")
        yield conn


@pytest.fixture()
def named(engine, monkeypatch):
    monkeypatch.setattr(
        load_operation, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(handle=lambda: engine),
    )
    return SimpleNamespace(log_depth=0)


class TestTheBuilderChooses:
    """ONE place a load's transform is decided."""

    def test_no_declaration_is_identity(self) -> None:
        built = load_operation._build_transform(SimpleNamespace(), None, None)

        assert isinstance(built, IdentityTransform)

    def test_a_declaration_is_the_applying_transform(self) -> None:
        built = load_operation._build_transform(
            SimpleNamespace(), None, DECLARATION,
        )

        assert isinstance(built, ColumnTransform)
        assert not isinstance(built, IdentityTransform)

    def test_it_resolves_secrets_rather_than_leaving_the_object_none(
        self, monkeypatch
    ) -> None:
        """A declaration NAMES an environment variable; something trusted
        reads it. An ad-hoc load quietly having no secrets would make an
        `encrypt` rule fail in a way that looks like a bad key.
        """
        monkeypatch.setenv("A_TEST_KEY", "resolved")
        built = load_operation._build_transform(
            SimpleNamespace(), None,
            {"columns": [{"name": "c", "source": "a",
                          "transform": {"type": "encrypt", "key_env": "A_TEST_KEY"}}]},
        )

        assert built.secrets == {"A_TEST_KEY": "resolved"}


class TestAQueryLoadRunsIt:

    @staticmethod
    def _rows(engine, target, **kwargs) -> list[dict[str, Any]]:
        return data_file_for(target).read()

    def test_a_declaration_renames_the_columns_written(
        self, named, tmp_path
    ) -> None:
        """THE PROOF. The rename is what no ad-hoc load could do."""
        out = tmp_path / "rows.csv"

        load_operation.load_query_to_file(
            named, _Log(), "SELECT a, b FROM orders ORDER BY a", "w", str(out),
            transform=DECLARATION,
        )

        assert data_file_for(out).read() == [
            {"identifier": "1", "label": "x"},
            {"identifier": "2", "label": "y"},
        ]

    def test_a_rule_changes_the_value(self, named, tmp_path) -> None:
        """A declared rule, applied on the way out.

        Two columns rather than one, deliberately: a single-column CSV reads
        back as no rows at all -- the delimited reader has no delimiter to
        find -- which is a fact about that reader and predates this. A test
        proving a transform ran should not be able to fail for it.
        """
        out = tmp_path / "rows.csv"

        load_operation.load_query_to_file(
            named, _Log(), "SELECT a, b FROM orders ORDER BY a", "w", str(out),
            transform={"columns": [
                {"name": "kept", "source": "a"},
                {"name": "shout", "source": "b",
                 "transform": {"type": "constant", "value": "CONSTANT"}},
            ]},
        )

        assert data_file_for(out).read() == [
            {"kept": "1", "shout": "CONSTANT"},
            {"kept": "2", "shout": "CONSTANT"},
        ]

    def test_no_declaration_writes_exactly_what_it_did_before(
        self, named, tmp_path
    ) -> None:
        """THE REGRESSION THAT MATTERS.

        Every load that names no transformation must be untouched by this.
        """
        out = tmp_path / "rows.csv"

        load_operation.load_query_to_file(
            named, _Log(), "SELECT a, b FROM orders ORDER BY a", "w", str(out),
        )

        assert data_file_for(out).read() == [
            {"a": "1", "b": "x"}, {"a": "2", "b": "y"},
        ]


class TestTheScreenSeesWhatTheLoadWillDo:
    """Preview and the endpoint inspection run the SAME transform."""

    def test_the_preview_shows_the_transformed_records(self, named) -> None:
        found = load_operation.preview_load(
            named, statement="SELECT a, b FROM orders ORDER BY a",
            source_connection="w", declaration=DECLARATION,
        )

        assert found.columns == ("identifier", "label")
        assert found.rows == (
            {"identifier": 1, "label": "x"}, {"identifier": 2, "label": "y"},
        )

    def test_a_prospective_destination_gets_the_declared_columns(
        self, named, tmp_path
    ) -> None:
        """A file has no columns until it is written, and what WOULD be
        written is now the transform's output rather than the query's.
        """
        found = load_operation.inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
            out_file=str(tmp_path / "rows.csv"), transform=DECLARATION,
        )

        assert found.source == ("identifier", "label")
        assert found.destination == ("identifier", "label")

    def test_without_one_they_are_the_source_s_own_names(self, named, tmp_path) -> None:
        found = load_operation.inspect_load_endpoints(
            named, statement="SELECT a, b FROM orders", source_connection="w",
            out_file=str(tmp_path / "rows.csv"),
        )

        assert found.source == ("a", "b")


class _Log:
    def append(self, *_a, **_k) -> None:
        pass

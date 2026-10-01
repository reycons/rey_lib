"""The Source: one canonical object for where a load's records come from.

What is asserted is the contract every entry point relies on -- which kinds
exist, what a selection keeps, what validation refuses, that the declarative
form round-trips, and that resolution goes through the existing resolvers and
leaves nothing live behind.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence

import pytest

from rey_lib.db.query_source import QuerySource
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file.base import DataFile
from rey_lib.load import Source
from rey_lib.load import source as source_module
from rey_lib.load.source import SOURCE_FIELDS


class CountingReader:
    """The source-context contract, answered from rows, counting calls."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[tuple[Optional[int], Optional[int]]] = []

    def file_source_context(
        self,
        file_manifest_id: Optional[int] = None,
        file_mutation_id: Optional[int] = None,
        required: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append((file_manifest_id, file_mutation_id))
        return [{
            "file_manifest_id": file_manifest_id or 10,
            "file_mutation_id": file_mutation_id or 25,
            "file_type_id": 4, "installation_id": 1,
            "path": self.path, "layout": "DELIMITED_HEADER",
            "data_profile_id": None, "data_profile_field_id": None,
            "transform_id": None, "transform_column_id": None,
            "profile_header_definition": None, "transform_row_filter": None,
            "transform_is_default": True,
        }]


@pytest.fixture()
def connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for the configured connections, recording which were named."""
    named: list[str] = []

    def shared_connection(ctx: Any, name: str) -> Any:
        named.append(name)
        return SimpleNamespace(handle=lambda: f"handle:{name}")

    monkeypatch.setattr(source_module, "shared_connection", shared_connection)
    return named


class TestTheKindsAndTheSelection:

    def test_the_kinds_are_the_four_source_kinds(self) -> None:
        assert Source.kinds() == ("file", "database", "sql_file", "manifest")

    def test_the_fields_are_the_loaders_source_parameters(self) -> None:
        assert set(SOURCE_FIELDS) == {
            "file", "file-type", "source-connection", "statement", "sql-file",
            "file-manifest-id", "file-mutation-id", "file-type-id",
        }

    def test_switching_kinds_keeps_what_was_said_under_the_other(self) -> None:
        source = Source()
        source.update("file", "/data/a.csv")
        source.select("database")
        source.update("statement", "select 1")
        source.select("file")

        assert source.configuration() == {
            "file": "/data/a.csv", "file-type": None, "file-mutation-id": None,
        }
        assert source.value("statement") == "select 1"

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no source kind"):
            Source().select("table")

    def test_a_field_that_is_not_a_source_field_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a source field"):
            Source().update("table", "public.t")


class TestWhichKindIsInForce:

    def test_a_choice_wins(self) -> None:
        assert Source({"file": "/a.csv"}, selected="database").selected_kind() == "database"

    def test_unchosen_it_is_the_kind_whose_own_fields_carry_values(self) -> None:
        assert Source({"file-manifest-id": 10}).selected_kind() == "manifest"
        assert Source({"sql-file": "/q.sql"}).selected_kind() == "sql_file"

    def test_a_shared_field_alone_decides_nothing(self) -> None:
        """A connection says nothing about whether a query was typed or read."""
        assert Source({"source-connection": "warehouse"}).selected_kind() == "file"

    def test_nothing_said_is_the_first_kind(self) -> None:
        assert Source().selected_kind() == "file"


class TestWhatValidationRefuses:

    @pytest.mark.parametrize("values, selected, missing", [
        ({}, "file", ["file"]),
        ({"source-connection": "w"}, "database", ["statement"]),
        ({"statement": "select 1"}, "database", ["source-connection"]),
        ({"source-connection": "w"}, "sql_file", ["sql-file"]),
        ({"file-type-id": 4}, "manifest", ["file-manifest-id or file-mutation-id"]),
        ({"file-mutation-id": 25}, "manifest", []),
        ({"file": "/a.csv"}, "file", []),
    ])
    def test_each_kind_names_what_it_still_needs(
        self, values: dict[str, Any], selected: str, missing: list[str],
    ) -> None:
        assert Source(values, selected=selected).validate() == missing


class TestTheDeclarativeForm:

    def test_it_round_trips(self) -> None:
        source = Source({"file": "/a.csv", "statement": "select 1"}, selected="file")

        again = Source.from_declaration(source.declaration())

        assert again.declaration() == source.declaration()
        assert again.selected_kind() == "file"

    def test_it_holds_nothing_live(self) -> None:
        declared = Source({"file": "/a.csv"}).declaration()
        assert declared == {"selected": None, "values": {"file": "/a.csv"}}


class TestResolution:

    def test_a_file_resolves_to_a_data_file_without_reading_it(self) -> None:
        resolved = Source({"file": "/nowhere/a.csv"}).resolve(SimpleNamespace())

        assert isinstance(resolved, DataFile)

    def test_a_query_resolves_to_a_query_source_on_its_connection(
        self, connections: list[str],
    ) -> None:
        adapter = object()
        resolved = Source(
            {"source-connection": "warehouse", "statement": "select 1"},
            selected="database",
        ).resolve(SimpleNamespace(), adapter=adapter)

        assert isinstance(resolved, QuerySource)
        assert (resolved.conn, resolved.statement, resolved.adapter) == (
            "handle:warehouse", "select 1", adapter,
        )
        assert connections == ["warehouse"]

    def test_a_sql_file_is_read_into_one_query_source(
        self, connections: list[str], tmp_path: Path,
    ) -> None:
        query = tmp_path / "q.sql"
        query.write_text("select 2", encoding="utf-8")

        resolved = Source(
            {"source-connection": "warehouse", "sql-file": str(query)},
        ).resolve(SimpleNamespace())

        assert isinstance(resolved, QuerySource)
        assert resolved.statement == "select 2"

    @pytest.mark.parametrize("content", [None, "   "])
    def test_a_missing_or_empty_sql_file_is_refused(
        self, connections: list[str], tmp_path: Path, content: Optional[str],
    ) -> None:
        query = tmp_path / "q.sql"
        if content is not None:
            query.write_text(content, encoding="utf-8")

        with pytest.raises(ConfigError, match="sql_file"):
            Source({"source-connection": "w", "sql-file": str(query)}).resolve(SimpleNamespace())

    def test_a_governed_file_resolves_through_one_governed_read(self) -> None:
        reader = CountingReader("/data/governed.txt")

        resolved = Source({"file-manifest-id": "10"}).resolve(SimpleNamespace(), reader=reader)

        assert isinstance(resolved, DataFile)
        assert reader.calls == [(10, None)]

    def test_a_governed_file_needs_a_reader(self) -> None:
        with pytest.raises(ConfigError, match="reader"):
            Source({"file-manifest-id": 10}).resolve(SimpleNamespace())

    def test_an_incomplete_selection_is_refused_before_anything_is_built(
        self, connections: list[str],
    ) -> None:
        with pytest.raises(ConfigError, match="statement"):
            Source({"source-connection": "w"}, selected="database").resolve(SimpleNamespace())
        assert connections == []

    def test_resolution_leaves_nothing_live_on_the_source(
        self, connections: list[str],
    ) -> None:
        source = Source({"source-connection": "w", "statement": "select 1"})
        before = dict(vars(source))

        source.resolve(SimpleNamespace(), adapter=object())

        assert vars(source) == before

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
from rey_lib.load.source import SOURCE_FIELD_METADATA, SOURCE_FIELDS


class CountingReader:
    """The source-context contract, answered from rows, counting calls."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[tuple[Optional[int], Optional[int]]] = []

    def get_file_manifests(
        self,
        *,
        file_manifest_id: Optional[int] = None,
        file_mutation_id: Optional[int] = None,
        **_filters: Any,
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
        with pytest.raises(ConfigError, match="no source kind"):
            Source().select("table")

    def test_a_field_that_is_not_a_source_field_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="not a source field"):
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


class TestItsLiveQuery:
    """#1151: what a reader runs is the Source's own statement on its own connection."""

    def test_a_database_source_runs_its_statement_on_its_connection(
        self, connections: list[str],
    ) -> None:
        source = Source({"source-connection": "file_reader",
                         "statement": "select * from read_csv('f.csv')"}, selected="database")

        query = source.query(SimpleNamespace())

        assert query.statement == "select * from read_csv('f.csv')"
        assert query.connection.handle() == "handle:file_reader"
        assert connections == ["file_reader"]

    def test_an_edit_is_what_the_next_query_runs(self, connections: list[str]) -> None:
        source = Source({"source-connection": "warehouse", "statement": "select 1"},
                        selected="database")

        source.update("statement", "select 2")

        assert source.query(SimpleNamespace()).statement == "select 2"

    def test_a_source_with_no_connection_is_refused(self, connections: list[str]) -> None:
        source = Source({"statement": "select 1"}, selected="database")

        with pytest.raises(ConfigError, match="statement and its connection"):
            source.query(SimpleNamespace())
        assert connections == []

    def test_a_file_source_runs_no_statement(self) -> None:
        with pytest.raises(ConfigError, match="only a database Source"):
            Source({"file": "/nowhere/a.csv"}).query(SimpleNamespace())


class TestResolution:

    def test_a_file_resolves_to_a_data_file_without_reading_it(self) -> None:
        resolved = Source({"file": "/nowhere/a.csv"}).resolve(SimpleNamespace())

        assert isinstance(resolved, DataFile)

    def test_a_query_resolves_to_a_query_source_on_its_connection(
        self, connections: list[str],
    ) -> None:
        resolved = Source(
            {"source-connection": "warehouse", "statement": "select 1"},
            selected="database",
        ).resolve(SimpleNamespace())

        assert isinstance(resolved, QuerySource)
        assert (resolved.conn, resolved.statement) == ("handle:warehouse", "select 1")
        assert not hasattr(resolved, "adapter")  # it reads through DatabaseQuery (757)
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

        source.resolve(SimpleNamespace())

        assert vars(source) == before


class TestItsSnapshot:
    """The Source answers its own projection: one snapshot of one state (backlog 681)."""

    _FACTS = {
        "file_manifest_id": 3, "file_mutation_id": 12, "installation_id": 1,
        "data_profile_id": 4, "file_name": "trades.csv",
        "mutation_choices": [{"file_mutation_id": 12, "result": "sanitized"}],
        "transform_choices": [{"transform_id": 20, "transform_name": "t", "is_default": True}],
        "selected_transform_id": 20, "transform_query_id": None,
    }

    def test_the_external_contract(self) -> None:
        source = Source({"file": "/in.csv"})
        source.observe_governed_context(self._FACTS)

        shown = source.projection(SimpleNamespace())

        assert shown == {
            "kinds": list(source.kinds()),
            "fields": {
                "file": ["file", "file-type", "file-mutation-id"],
                "database": ["source-connection", "statement"],
                "sql_file": ["source-connection", "sql-file"],
                "manifest": ["file-manifest-id", "file-mutation-id", "file-type-id"],
            },
            "selected": "file",
            "values": {"file": "/in.csv"},
            "configuration": dict(source.configuration()),
            "missing": [],
            "field_metadata": SOURCE_FIELD_METADATA,
            "governed": {
                "identity": {
                    "file_manifest_id": 3, "file_mutation_id": 12,
                    "installation_id": 1, "data_profile_id": 4,
                },
                "file": {"file_name": "trades.csv"},
                "saved_settings": {
                    "choices": [{"transform_id": 20, "transform_name": "t", "is_default": True}],
                    "selected": 20,
                },
            },
            "choices": {
                "file-type": [{"value": one, "label": one} for one in sorted(DataFile.formats())],
                "file-mutation-id": [{"value": "12", "label": "sanitized"}],
            },
            "operations": [],
        }

    def test_every_field_states_what_it_is(self) -> None:
        """The Source's vocabulary describes each of its fields, once (#1151)."""
        assert set(SOURCE_FIELD_METADATA) == set(SOURCE_FIELDS)
        for name, found in SOURCE_FIELD_METADATA.items():
            assert found["value_type"] and found["description"], name

    def test_its_connection_says_it_is_a_connection(self) -> None:
        """#1151 row 1: the domain value it holds -- not how it is drawn."""
        assert SOURCE_FIELD_METADATA["source-connection"]["value_type"] == "connection"

    def test_file_type_is_offered_the_data_formats_of_files_one_registry(self) -> None:
        """Read from DataFile.formats() (#1133), never a second list."""
        shown = Source({"file": "/in.csv"}).projection(SimpleNamespace())

        assert [one["value"] for one in shown["choices"]["file-type"]] == sorted(
            DataFile.formats())

    def test_its_governed_context_is_read_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = Source({"file": "/in.csv"})
        source.observe_governed_context(self._FACTS)
        reads: list[int] = []
        real = Source.governed_context

        def counting(self: Source) -> Any:
            reads.append(1)
            return real(self)

        monkeypatch.setattr(Source, "governed_context", counting)

        source.projection(SimpleNamespace())

        assert len(reads) == 1

    def test_ungoverned_it_shows_no_governed_facts(self) -> None:
        shown = Source({"file": "/in.csv"}).projection(SimpleNamespace())

        assert shown["governed"] is None
        assert "file-mutation-id" not in shown["choices"]

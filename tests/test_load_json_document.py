"""Loading a table that was exported as a JSON document.

A JSONL file is one object per line. A table exported as JSON is ONE VALUE --
an array of row objects, usually under a single key naming the table:

    {"asset": [{"asset_id": 1, ...}, {"asset_id": 2, ...}]}

Handing that to the JSONL reader produced ``Invalid JSONL at line 1``, which
is the symptom this format exists to remove.

**But producing rows was never the bar.** The architecture settled on

    ConfiguredLoad
      |- DataFile        this physical file
      |- DataTransform   what the records contain
      +- DataLoader      where the records go

and a new format that produced rows through anything else would be the symptom
fixed and the architecture broken. So the FIRST test here is not that JSON
loads -- it is WHICH OBJECTS the load used to do it. Everything after that
assumes the chain and tests the format.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.files import configured_load, file_loader
from rey_lib.files.data_file import DataFileStructureError
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.files.data_file import data_file_for
from rey_lib.files.data_file.base import RecordShape
from rey_lib.files.data_file.keyed import JsonFile, JsonlFile
from rey_lib.files.data_loader import DataLoader
from rey_lib.files.data_transform import DataTransform


class _Adapter:
    """Records what the load decided, instead of reaching a database."""

    def __init__(self, *, exists: bool = True) -> None:
        self._exists = exists
        self.created: list[tuple] = []
        self.inserted: list[tuple] = []

    def table_exists(self, _conn, _schema, _table) -> bool:
        return self._exists

    def get_table_columns(self, _conn, _schema, _table) -> list[str]:
        return ["asset_id", "name"] if self._exists else []

    def create_staging_table_if_not_exists(self, _conn, schema, table, defs):
        self.created.append((schema, table, defs))
        self._exists = True
        return True

    def bulk_insert(self, _conn, schema, table, rows, columns) -> int:
        self.inserted.append((schema, table, rows, columns))
        return len(rows)

    def is_truncation_error(self, _exc) -> bool:
        return False


def _document(tmp_path: Path, document, name: str = "asset.json") -> Path:
    """Write one JSON document, whatever shape it is."""
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


#: The shape a table export actually produces: rows under a key naming it.
_EXPORTED = {"asset": [
    {"asset_id": 1, "name": "a"},
    {"asset_id": 2, "name": "b"},
]}


def _load(tmp_path, monkeypatch, run_log, adapter, source=None,
          destination="testing.asset", **kwargs) -> int:
    """Run the real direct-load entry point over a JSON document.

    ``load_file_to_table`` is what the Console's Load control calls, so this
    exercises the path a reader takes rather than a per-file step underneath
    it.
    """
    monkeypatch.setattr(file_loader, "_db_adapter", adapter)
    # Resolved from the target now, so it is substituted where it is resolved.
    monkeypatch.setattr(
        file_loader, "shared_connection",
        lambda _ctx, _name: SimpleNamespace(
            handle=lambda: SimpleNamespace(
                commit=lambda: None, rollback=lambda: None,
            )
        ),
    )
    monkeypatch.setattr(file_loader, "_execute_movements", lambda *a, **k: None)
    return file_loader.load_file_to_table(
        SimpleNamespace(log_depth=0), run_log,
        source if source is not None else _document(tmp_path, _EXPORTED),
        destination, "a_connection", **kwargs,
    )


class TestJsonLoadsThroughTheCanonicalChain:
    """The condition for calling any of this done.

    Asserted on the objects the load ACTUALLY used, captured as it ran --
    not inferred from rows appearing at the far end, which would pass just
    as well if JSON had been bolted onto the legacy reader.
    """

    def test_the_three_stages_are_the_architecture_s_own(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """source -> transform -> target, captured AS THE ARGUMENTS.

        Stronger than it used to be. The three objects were keyword arguments
        beside a path, a schema and a table; now they are the transfer step's
        three POSITIONAL parameters, so capturing the call captures the whole
        contract. A load reaching the table another way would not present
        three objects here at all.
        """
        used: dict = {}
        stage = file_loader._load_one_file

        def _spy(source, transform, target, **context):
            used.update(
                source=source, transform=transform, target=target, **context,
            )
            return stage(source, transform, target, **context)

        monkeypatch.setattr(file_loader, "_load_one_file", _spy)

        loaded = _load(tmp_path, monkeypatch, run_log, _Adapter())

        assert loaded == 2
        assert isinstance(used["source"], JsonFile)
        assert isinstance(used["transform"], DataTransform)
        assert isinstance(used["target"], DatabaseObjectIdentity)
        assert isinstance(used["loader"], DataLoader)

    def test_no_connection_crosses_the_transfer_boundary(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """An open connection is the runtime form of ONE KIND of endpoint.

        A file-to-file transfer has none, so a boundary demanding one would
        not be symmetric however its parameters were spelled. The connection
        is resolved below, from the target.
        """
        seen: dict = {}
        stage = file_loader._load_one_file

        def _spy(source, transform, target, **context):
            seen.update(context)
            return stage(source, transform, target, **context)

        monkeypatch.setattr(file_loader, "_load_one_file", _spy)
        _load(tmp_path, monkeypatch, run_log, _Adapter())

        assert "conn" not in seen
        assert not any("conn" in name for name in seen)

    def test_there_is_no_legacy_path_left_to_take(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """This used to prove JSON avoided the pre-DataFile reader.

        It now proves something stronger and simpler: THE READER IS GONE.
        A deny-list of formats the loader read without a data object cannot
        be re-entered, quietly or otherwise, because only a data object
        crosses the boundary at all.
        """
        assert not hasattr(file_loader, "_read_unmigrated_source")
        assert not hasattr(file_loader, "_UNMIGRATED_FILE_TYPES")
        assert not hasattr(file_loader, "_validate_load_header")

        assert _load(tmp_path, monkeypatch, run_log, _Adapter()) == 2

    def test_the_document_is_resolved_from_its_suffix_alone(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """No caller declares JSON for this to work.

        The Console hands over a path; if ``.json`` named no format, the load
        would refuse before any of the above mattered.
        """
        adapter = _Adapter()

        assert _load(tmp_path, monkeypatch, run_log, adapter) == 2
        _schema, _table, rows, columns = adapter.inserted[0]
        assert columns == ["asset_id", "name"]
        assert rows == _EXPORTED["asset"]


class TestTheTwoShapesATableExportProduces:
    """Both are read. Neither is guessed at."""

    def test_rows_under_a_key_naming_the_table(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        adapter = _Adapter()

        assert _load(tmp_path, monkeypatch, run_log, adapter) == 2
        assert adapter.inserted[0][2] == _EXPORTED["asset"]

    def test_the_rows_on_their_own(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        adapter = _Adapter()
        source = _document(tmp_path, _EXPORTED["asset"])

        assert _load(tmp_path, monkeypatch, run_log, adapter, source=source) == 2
        assert adapter.inserted[0][2] == _EXPORTED["asset"]

    def test_values_keep_their_json_types(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        """The whole reason this format is worth having over CSV.

        A delimited reader has only text to offer. This one carries types,
        and discarding them here would be a loss the destination cannot undo.
        """
        adapter = _Adapter()

        _load(tmp_path, monkeypatch, run_log, adapter)

        assert adapter.inserted[0][2][0]["asset_id"] == 1
        assert not isinstance(adapter.inserted[0][2][0]["asset_id"], str)


class TestWhatIsRefusedRatherThanGuessedAt:
    """Each refusal names what was actually found.

    A document this format cannot read is a file the caller must look at, so
    every message has to say which thing was wrong -- the key, its value, or
    an entry.
    """

    def _refusal(self, tmp_path: Path, document, name="asset.json") -> str:
        source = _document(tmp_path, document, name=name)
        with pytest.raises(DataFileStructureError) as raised:
            JsonFile(source).read()
        return str(raised.value)

    def test_several_keys_are_not_searched_for_an_array(
        self, tmp_path: Path
    ) -> None:
        """Which table loaded would otherwise depend on key order.

        A file holding two exported tables would silently load one of them.
        """
        message = self._refusal(tmp_path, {"asset": [{"a": 1}], "site": [{"b": 2}]})

        assert "keys are: asset, site" in message

    def test_one_key_holding_something_other_than_an_array(
        self, tmp_path: Path
    ) -> None:
        """The key is right and its value is not. Said as such.

        Reporting this as "keys are: asset" would describe a document whose
        key was wrong, and send the reader to look at the wrong thing.
        """
        message = self._refusal(tmp_path, {"asset": {"asset_id": 1}})

        assert "'asset'" in message
        assert "array of rows" in message

    def test_an_entry_that_is_not_an_object_names_no_columns(
        self, tmp_path: Path
    ) -> None:
        message = self._refusal(tmp_path, [{"a": 1}, 2])

        assert "entry 2" in message

    def test_a_scalar_document_is_not_a_table(self, tmp_path: Path) -> None:
        message = self._refusal(tmp_path, "hello")

        assert "no rows in it" in message


class TestAnEmptyDocumentIsReadRatherThanRefused:
    """Reading it is not the problem; having no columns is.

    Both shapes of empty are well-formed JSON holding zero rows, so the file
    is read successfully and the load then has nothing to insert. That is the
    same outcome an empty ``.jsonl`` has always had -- see backlog
    a_zero_row_keyed_file_cannot_create_a_table -- and is deliberately not
    turned into a JSON-specific refusal here.
    """

    def test_both_shapes_of_empty_read_as_zero_rows(self, tmp_path: Path) -> None:
        assert JsonFile(_document(tmp_path, [])).read() == []
        assert JsonFile(_document(tmp_path, {"asset": []})).read() == []

    def test_an_empty_document_loads_nothing_and_raises_nothing(
        self, tmp_path: Path, monkeypatch, run_log
    ) -> None:
        adapter = _Adapter()
        source = _document(tmp_path, {"asset": []})

        assert _load(tmp_path, monkeypatch, run_log, adapter, source=source) == 0
        assert adapter.inserted == []


class TestJsonIsNotAKindOfJsonl:
    """Two formats that share a base, not one format with two spellings.

    They have the same question about their columns -- the names are on every
    record -- and completely different answers about how a record is found.
    Making one a subclass of the other would put a document-wide parse where
    a line reader is expected.
    """

    def test_neither_is_a_subclass_of_the_other(self) -> None:
        assert not issubclass(JsonFile, JsonlFile)
        assert not issubclass(JsonlFile, JsonFile)

    def test_a_jsonl_file_is_still_refused_as_a_document(
        self, tmp_path: Path
    ) -> None:
        """The reverse of the original failure, and equally a refusal.

        One object per line is not one document, so JSON must not accept it
        by parsing the first line and ignoring the rest.
        """
        source = tmp_path / "asset.json"
        source.write_text('{"asset_id": 1}\n{"asset_id": 2}\n', encoding="utf-8")

        with pytest.raises(Exception):      # noqa: B017 -- a parse, not a shape
            JsonFile(source).read()


class TestTheFileObjectSaysWhetherItHoldsRecords:
    """The capability, asked of the file rather than guessed from its name.

    `JsonFile._rows` reads the same rule to find the records; a consumer that
    only needs to know whether a path is worth querying asks `record_shape()`.
    Two copies of a rule about what a table is would eventually disagree, and
    the disagreement would show as a file one surface offers and another
    refuses.
    """

    def _shape(self, tmp_path: Path, document) -> RecordShape:
        return JsonFile(_document(tmp_path, document, name="shape.json")).record_shape()

    def test_the_two_table_shapes_are_recognised(self, tmp_path: Path) -> None:
        assert self._shape(tmp_path, [{"id": 1}]) == RecordShape(
            holds_records=True, record_key=None
        )
        assert self._shape(tmp_path, {"asset": [{"id": 1}]}) == RecordShape(
            holds_records=True, record_key="asset"
        )

    def test_an_empty_key_is_a_key_not_an_absent_one(self, tmp_path: Path) -> None:
        """THE DISTINCTION THIS TYPE EXISTS FOR.

        `{"": [...]}` is legal JSON: a document with one key that happens to
        be empty. A contract returning "" to mean "the document IS the array"
        could not tell these two documents apart, and a caller testing the
        key for truthiness would unwrap neither -- producing exactly the
        one-row result that unwrapping exists to avoid.
        """
        bare = self._shape(tmp_path, [{"id": 1}])
        keyed_on_empty = self._shape(tmp_path, {"": [{"id": 1}]})

        assert bare.record_key is None
        assert keyed_on_empty.record_key == ""
        assert bare != keyed_on_empty
        # Both ARE tables. The difference is only where the rows sit.
        assert bare.holds_records and keyed_on_empty.holds_records

    def test_what_is_not_a_table(self, tmp_path: Path) -> None:
        """Including an empty document, which is a dict with no key at all."""
        for document in (
            {"a": [], "b": []},          # several keys: not searched
            {"asset": {"id": 1}},        # one key, not an array
            "hello",                     # a scalar
            42,
            None,
            {},
        ):
            shape = self._shape(tmp_path, document)
            assert shape.holds_records is False, document
            assert shape.record_key is None, document

    def test_an_empty_array_is_still_a_table(self, tmp_path: Path) -> None:
        """Zero rows is a row count, not a shape. Both ways of writing it."""
        assert self._shape(tmp_path, []).holds_records is True
        assert self._shape(tmp_path, {"asset": []}).holds_records is True

    def test_not_being_a_table_is_an_answer_but_being_unreadable_is_not(
        self, tmp_path: Path
    ) -> None:
        """THE LINE THIS CAPABILITY MUST HOLD.

        A configuration document is a fine file that holds no rows, and that
        is an ANSWER. A file that will not parse is not a shape at all, and
        softening it here would take a real error away from every direct
        caller -- leaving them to read "no records" and never learn the file
        was broken. Whoever only wants routing information catches it, and
        does so knowing it is choosing to ignore an error.
        """
        assert self._shape(tmp_path, {"name": "x"}).holds_records is False

        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(Exception):      # noqa: B017 -- a read, not a shape
            JsonFile(broken).record_shape()


class TestEveryDataFileAnswersTheSameQuestion:
    """The capability is the DataFile's, not a JSON special case.

    A named .csv and a table-shaped .json reach a query surface for the SAME
    reason. Without that, one arrives by extension and the other by content
    inspection -- two architectural reasons for one destination.
    """

    def test_a_delimited_file_holds_records_without_being_read(
        self, tmp_path: Path
    ) -> None:
        """True by construction: a DataFile is a thing that produces rows.

        Asserted against a file whose CONTENTS would fail to parse as
        anything, to prove the answer came from the type and not a read.
        """
        source = tmp_path / "holdings.csv"
        source.write_text("a,b\n1,2\n", encoding="utf-8")

        assert data_file_for(source).record_shape() == RecordShape(
            holds_records=True, record_key=None
        )

    def test_json_is_the_format_that_has_to_look(self, tmp_path: Path) -> None:
        """Two files of one format, two answers. No other format does this."""
        table = _document(tmp_path, {"asset": [{"id": 1}]}, name="table.json")
        config = _document(tmp_path, {"name": "x", "version": 2}, name="conf.json")

        assert data_file_for(table).record_shape().holds_records is True
        assert data_file_for(config).record_shape().holds_records is False

    def test_a_keyed_line_file_also_holds_records(self, tmp_path: Path) -> None:
        """JSONL says records too -- and viewer policy still keeps it.

        Recorded here because it is the proof that this capability is NOT
        "should open in a query surface": the routing table sends JSONL to
        its own inspector, above and regardless of this answer.
        """
        source = tmp_path / "run.jsonl"
        source.write_text('{"a": 1}\n', encoding="utf-8")

        assert data_file_for(source).record_shape().holds_records is True


class TestTheRefusalsSurvivedTheExtraction:
    """The regression pulling the rule out of `_rows` could have caused.

    Every message must still name the thing that was actually wrong. These
    duplicate TestWhatIsRefusedRatherThanGuessedAt deliberately: that class
    says what a reader is told, this one says the extraction did not change
    it.
    """

    def test_each_shape_still_gets_its_own_message(self, tmp_path: Path) -> None:
        cases = {
            "several keys":  ({"asset": [{"a": 1}], "site": [{"b": 2}]},
                              "keys are: asset, site"),
            "one non-array": ({"asset": {"a": 1}}, "holds one key, 'asset'"),
            "a scalar":      ("hello", "no rows in it"),
            "empty object":  ({}, "keys are: (none)"),
        }
        for label, (document, expected) in cases.items():
            source = _document(tmp_path, document, name="refused.json")
            with pytest.raises(DataFileStructureError) as raised:
                JsonFile(source).read()
            assert expected in str(raised.value), label
            assert raised.value.validation_name == "load_json_shape", label

    def test_a_document_keyed_on_the_empty_string_still_loads(
        self, tmp_path: Path
    ) -> None:
        """The reader agrees with the shape contract about this document."""
        source = _document(tmp_path, {"": [{"id": 1}, {"id": 2}]})

        assert JsonFile(source).read() == [{"id": 1}, {"id": 2}]

"""A file that knows what it is.

No database is opened, and nothing here loads anything: at this step the
hierarchy has no callers. What is asserted is the OBJECT -- what each format
says about itself, and which of two very different questions its validation is
answering.

The distinction that runs through all of it:

    validate(expected)   does this file match the destination?
    validate(None)       is this file coherent with ITSELF?

``None`` is not ``[]``. An empty list is a destination with no columns, which
nothing matches -- the conflation that once rejected a valid file as a header
mismatch against nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rey_lib.data.errors import DataStructureError
from rey_lib.files.data_file import DataFile
from rey_lib.files.file import File, file_format
from rey_lib.files.data_file.keyed import JsonFile, JsonlFile
from rey_lib.errors.error_utils import ConfigError


def _jsonl(tmp_path: Path, *records: dict, name: str = "source.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _csv(tmp_path: Path, text: str, name: str = "source.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestTheRegistry:
    """Adding a format is a module, not an edit to a central list (#1133: File's)."""

    def test_every_data_format_registers_itself(self) -> None:
        """Discovery imports the submodules; no module lists them."""
        assert set(DataFile.formats()) == {
            "CSV", "DELIMITED_HEADER", "DELIMITED_NO_HEADER",
            "JSON", "JSONL", "NDJSON",
            "XLS", "XLSB", "XLSM", "XLSX",
        }

    def test_the_recognised_formats_with_no_records_are_files_not_data_files(self) -> None:
        """PYTHON, YAML and TEXT are File formats; DataFile.formats() leaves them out."""
        everything = set(File.formats())

        assert {"PYTHON", "YAML", "TEXT"} <= everything
        assert everything - set(DataFile.formats()) == {"PYTHON", "YAML", "TEXT"}

    def test_a_new_format_needs_no_change_to_any_existing_module(
        self, tmp_path: Path
    ) -> None:
        """THE INVARIANT, asserted rather than asserted-in-prose.

        Registering a subtype here -- in a test module, touching nothing --
        makes it constructible. That is the property the hierarchy exists for:
        a format is added by declaring one, never by editing a branch.
        """

        @file_format("TEST_ONLY_FORMAT")
        class _Throwaway(DataFile):
            @property
            def file_type(self) -> str:
                return "TEST_ONLY_FORMAT"

            def source_structure(self) -> list[str]:
                return ["a"]

            def read(self) -> list[dict]:
                return [{"a": 1}]

            def validate(self, expected_columns=None) -> None:
                return None

            def read_validated(self, expected_columns=None) -> list[dict]:
                return self.read()

        built = DataFile.for_path(tmp_path / "x.anything", file_type="TEST_ONLY_FORMAT")

        assert isinstance(built, _Throwaway)
        assert built.read() == [{"a": 1}]

    def test_aliases_register_together(self, tmp_path: Path) -> None:
        """JSONL and NDJSON are one format under two names.

        Registered together rather than normalised somewhere else, so there is
        no second place deciding they are the same thing.
        """
        path = _jsonl(tmp_path, {"a": 1})

        assert type(File.for_path(path, file_type="NDJSON")) is type(
            File.for_path(path, file_type="JSONL")
        )


class TestConstruction:
    """A declared type wins; a suffix answers when nothing was declared."""

    def test_the_declared_type_beats_the_suffix(self, tmp_path: Path) -> None:
        """Configuration knows things a filename does not."""
        path = _csv(tmp_path, "a,b\n1,x\n", name="misnamed.jsonl")

        assert File.for_path(path, file_type="CSV").file_type == "CSV"

    def test_the_suffix_answers_when_nothing_is_declared(
        self, tmp_path: Path
    ) -> None:
        """The direct case: someone names one file and that is the whole input."""
        assert File.for_path(_jsonl(tmp_path, {"a": 1})).file_type == "JSONL"
        assert File.for_path(_csv(tmp_path, "a\n1\n")).file_type == "CSV"

    @pytest.mark.parametrize("name, kind, token", [
        ("module.py", "PythonFile", "PYTHON"),
        ("config.yaml", "YamlFile", "YAML"),
        ("config.yml", "YamlFile", "YAML"),
        ("notes.txt", "TextFile", "TEXT"),
    ])
    def test_a_recognised_format_with_no_records_is_its_own_file(
        self, tmp_path: Path, name: str, kind: str, token: str,
    ) -> None:
        """Not a DataFile: a .py or .yaml file never pretends to hold records."""
        resolved = File.for_path(tmp_path / name)

        assert type(resolved).__name__ == kind
        assert resolved.file_type == token
        assert not isinstance(resolved, DataFile)

    def test_a_suffix_that_names_nothing_is_a_plain_file(self, tmp_path: Path) -> None:
        """Not defaulted to CSV and not a pretend DataFile: just a File.

        It can still be moved, kicked out and governed; asked for as a
        DataFile it is refused by name, never given a guessed reader.
        """
        path = tmp_path / "mystery.dat"
        path.write_text("a,b\n", encoding="utf-8")

        resolved = File.for_path(path)

        assert type(resolved) is File
        assert resolved.file_type == ""
        with pytest.raises(ConfigError) as raised:
            DataFile.for_path(path)
        assert ".dat" in str(raised.value)

    def test_a_file_with_no_records_is_refused_as_a_data_file_by_name(
        self, tmp_path: Path,
    ) -> None:
        """DataFile.for_path resolves through File and refuses what has no records."""
        with pytest.raises(ConfigError) as raised:
            DataFile.for_path(tmp_path / "module.py")

        assert "PYTHON" in str(raised.value)

    def test_an_unknown_declared_type_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        """A declared token no subclass registers is named in the refusal."""
        assert type(File.for_path(tmp_path / "book.parquet", file_type="PARQUET")) is File
        with pytest.raises(ConfigError) as raised:
            DataFile.for_path(tmp_path / "book.parquet", file_type="PARQUET")
        assert "PARQUET" in str(raised.value)

    def test_a_plain_file_carries_its_governed_facts(self, tmp_path: Path) -> None:
        """Every physical file is a File, with every fact a File carries."""
        plain = File.for_path(
            tmp_path / "notes.unknown", file_manifest_id=3, file_mutation_id=7,
            base_path="/b", inbox=str(tmp_path), original_path="/o/notes.unknown",
            original_mutation_id=5, data_profile_key="feed|notes",
        )

        assert plain.governed_facts() == {
            "file_manifest_id": 3, "file_mutation_id": 7, "classification": None,
            "base_path": "/b", "inbox": str(tmp_path),
            "original_path": "/o/notes.unknown", "original_mutation_id": 5,
            "data_profile_key": "feed|notes",
        }


class TestTheSuffixMapCannotDrift:
    """Every suffix File recognises names a format File registers."""

    def test_every_suffix_maps_to_a_registered_format(self) -> None:
        """The estate already has several file-type vocabularies that
        disagree. This asserts the suffix map is not another: every token it
        can yield is one a registered File subclass answers to.
        """
        from rey_lib.files.file import _SUFFIX_FILE_TYPES

        registered = set(File.formats())
        orphans = {suffix: token for suffix, token in _SUFFIX_FILE_TYPES.items()
                   if token not in registered}

        assert orphans == {}

    def test_json_is_a_document_file_not_a_line_reader(
        self, tmp_path: Path
    ) -> None:
        """The case that moved the rule, stated so it cannot be lost.

        ``.json`` resolves through the registry to JsonFile. Were it ever read
        a line at a time, a document would yield nothing -- the original
        failure.
        """
        assert File.type_for_suffix(".json") == "JSON"
        assert "JSON" in set(DataFile.formats())

        resolved = File.for_path(tmp_path / "doc.json")
        assert isinstance(resolved, JsonFile)
        assert not isinstance(resolved, JsonlFile)

    def test_the_suffix_is_read_forgivingly(self) -> None:
        """Case and a missing dot are not the caller's problem."""
        assert File.type_for_suffix(".JSONL") == "JSONL"
        assert File.type_for_suffix("csv") == "CSV"
        assert File.type_for_suffix("") == ""
        assert File.type_for_suffix(".nope") == ""


class TestDelimitedHeaderFile:
    """Columns named once, in order, before any row is read."""

    def test_source_structure_is_the_header(self, tmp_path: Path) -> None:
        assert DataFile.for_path(
            _csv(tmp_path, "a,b,c\n1,2,3\n")
        ).source_structure() == ["a", "b", "c"]

    def test_a_matching_header_passes(self, tmp_path: Path) -> None:
        DataFile.for_path(_csv(tmp_path, "a,b\n1,x\n")).validate(["a", "b"])

    def test_column_ORDER_matters_against_a_destination(
        self, tmp_path: Path
    ) -> None:
        """A header is an ordered artifact and the insert relies on the order.

        The deliberate difference from a keyed source, whose key order is
        incidental.
        """
        with pytest.raises(DataStructureError):
            DataFile.for_path(_csv(tmp_path, "b,a\n1,x\n")).validate(["a", "b"])

    def test_with_no_destination_only_the_header_must_exist(
        self, tmp_path: Path
    ) -> None:
        """Nothing to compare against, so nothing is invented to compare to."""
        DataFile.for_path(_csv(tmp_path, "a,b\n1,x\n")).validate(None)

    def test_a_file_with_no_header_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(DataStructureError) as raised:
            DataFile.for_path(_csv(tmp_path, "\n\n")).validate(None)

        assert "no header" in str(raised.value)

    def test_it_validates_before_reading(self, tmp_path: Path) -> None:
        """The cheap order, and the reason this format has its own class.

        A wrong file costs one line, not a parse. Asserted by spying on the
        reader: it must never run for a header that does not match.
        """
        source = DataFile.for_path(_csv(tmp_path, "a,b\n1,x\n"))
        source.read = lambda: pytest.fail("read before validation")  # type: ignore[method-assign]

        with pytest.raises(DataStructureError):
            source.read_validated(["WRONG"])


class TestJsonlFile:
    """Columns named on every record, so the check comes after the read."""

    def test_source_structure_is_the_first_records_keys(
        self, tmp_path: Path
    ) -> None:
        assert DataFile.for_path(
            _jsonl(tmp_path, {"a": 1, "b": "x"})
        ).source_structure() == ["a", "b"]

    def test_values_keep_their_json_types(self, tmp_path: Path) -> None:
        """This format carries types; discarding them is a loss."""
        records = DataFile.for_path(_jsonl(tmp_path, {"a": 1, "b": True})).read()

        assert records[0]["a"] == 1 and not isinstance(records[0]["a"], str)
        assert records[0]["b"] is True

    def test_key_order_does_not_matter(self, tmp_path: Path) -> None:
        """A JSON object's key order is incidental, unlike a CSV header's."""
        path = _jsonl(tmp_path, {"b": "x", "a": 1}, {"a": 2, "b": "y"})

        assert len(DataFile.for_path(path).read_validated(["a", "b"])) == 2

    @pytest.mark.parametrize(
        "records,why",
        [
            (({"a": 1, "b": "x"}, {"a": 2}), "a record missing a column"),
            (({"a": 1, "b": "x", "c": 9}, {"a": 2, "b": "y"}), "extra on the FIRST"),
            (({"a": 1, "b": "x"}, {"a": 2, "b": "y", "c": 9}), "extra on a LATER"),
        ],
    )
    def test_equality_against_the_destination_not_coverage(
        self, tmp_path: Path, records, why: str
    ) -> None:
        """A superset is as wrong as a subset, and here is why.

        Nothing projects records onto the destination: the column list comes
        from the first record, so an extra key BECOMES a column against a
        table that has none.
        """
        with pytest.raises(DataStructureError):
            DataFile.for_path(_jsonl(tmp_path, *records)).read_validated(["a", "b"])

    def test_with_no_destination_the_FIRST_record_sets_the_contract(
        self, tmp_path: Path
    ) -> None:
        """Internal consistency: the file's claim about itself, held to.

        This is the check the create path has never had -- today an
        inconsistent file has its table created and then fails inside the
        insert, reporting a database error for a file defect.
        """
        path = _jsonl(tmp_path, {"a": 1, "b": "x"}, {"a": 2, "c": "y"})

        with pytest.raises(DataStructureError) as raised:
            DataFile.for_path(path).read_validated(None)

        assert "record 2" in str(raised.value)
        assert "first record" in str(raised.value)

    def test_a_self_consistent_file_needs_no_destination(
        self, tmp_path: Path
    ) -> None:
        """What the create path asks: is this file coherent? Yes."""
        path = _jsonl(tmp_path, {"a": 1, "b": "x"}, {"b": "y", "a": 2})

        assert len(DataFile.for_path(path).read_validated(None)) == 2

    def test_the_source_is_parsed_exactly_once(self, tmp_path: Path) -> None:
        """read_validated must not reread to validate.

        A second parse would be invisible to every other test here and would
        cost a full pass over every file on every load. This is the assertion
        that catches a later "tidy-up" reintroducing one.
        """
        path = _jsonl(tmp_path, {"a": 1}, {"a": 2})
        source = DataFile.for_path(path)
        reads: list[int] = []
        original = source.read

        def _counting_read():
            reads.append(1)
            return original()

        source.read = _counting_read  # type: ignore[method-assign]
        source.read_validated(["a"])

        assert len(reads) == 1


class TestWhatTheObjectDeliberatelyDoesNotKnow:
    """The boundaries that let other consumers adopt this later."""

    def test_the_package_imports_nothing_from_the_database_layer(self) -> None:
        """A file must not know it is being loaded into anything.

        This is the property that lets the analysis framework and the
        Console's viewer reuse DataFile without inheriting a database
        contract. Asserted structurally, because it is easy to break with one
        convenient import.
        """
        import pkgutil
        from pathlib import Path as _Path

        import rey_lib.files.data_file as package

        for module in pkgutil.iter_modules([str(_Path(package.__file__).parent)]):
            source = (
                _Path(package.__file__).parent / f"{module.name}.py"
            ).read_text(encoding="utf-8")
            assert "rey_lib.db" not in source, (
                f"{module.name}.py imports the database layer"
            )

    def test_the_package_does_not_depend_on_the_module_it_came_from(
        self
    ) -> None:
        """The migration scaffold is gone.

        While the subtypes were being extracted they delegated back to
        file_loader's validators, so the OLD checks provably ran through the
        NEW object before anything moved. That import was temporary, and a
        subtype still reaching back into the module it was extracted from
        would leave the boundary unfinished -- DataTransform and DataLoader
        are built on top of this one.
        """
        import pkgutil
        from pathlib import Path as _Path

        import rey_lib.files.data_file as package

        for module in pkgutil.iter_modules([str(_Path(package.__file__).parent)]):
            source = (
                _Path(package.__file__).parent / f"{module.name}.py"
            ).read_text(encoding="utf-8")
            assert "file_loader" not in source, (
                f"{module.name}.py still reaches back into file_loader"
            )

    def test_no_subtype_declares_a_logical_schema(self) -> None:
        """What the produced records contain is a transform's answer.

        One configured feed declares 56 output columns over a 47-field file,
        9 of them with no physical source at all. A file cannot describe them,
        so it does not try.
        """
        assert not hasattr(DataFile, "logical_schema")
        assert not hasattr(DataFile, "load")


class TestDelimitedNoHeaderFile:
    """Every line is data, and that is the whole difference."""

    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "positional.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_first_row_is_data_and_is_not_eaten(
        self, tmp_path: Path
    ) -> None:
        """THE DEFECT THIS FORMAT EXISTS TO FIX, pinned exactly.

        The shared delimited reader takes the first line as the column names
        whatever the declared type, so a genuinely headerless file lost its
        first row -- silently, keyed by the values of the row that vanished:

            '1,x\\n2,y\\n'  ->  [{'1': '2'}]      two rows in, ONE row out

        Two rows in, two rows out, is the assertion.
        """
        source = self._write(tmp_path, "1,x\n2,y\n")

        rows = DataFile.for_path(source, file_type="DELIMITED_NO_HEADER").read()

        assert rows == [
            {"col001": "1", "col002": "x"},
            {"col001": "2", "col002": "y"},
        ]

    def test_it_does_not_go_through_the_reader_that_caused_it(
        self, tmp_path: Path
    ) -> None:
        """Structural, not incidental.

        The header format takes the first line as a header, as it must. This
        format is correct because it does not read that way -- so if someone
        routes it back there, the first row starts disappearing again.
        """
        source = self._write(tmp_path, "1,x\n2,y\n")

        as_a_header_file = DataFile.for_path(source, file_type="CSV", encoding="utf-8").read()

        assert len(as_a_header_file) == 1                # the defect it would bring back
        assert len(DataFile.for_path(
            source, file_type="DELIMITED_NO_HEADER"
        ).read()) == 2

    def test_columns_are_positional_and_sort_in_order(
        self, tmp_path: Path
    ) -> None:
        """col001, not column_1.

        Zero-padded so a wide file sorts the way it reads: col002 before
        col010, which column_2 and column_10 do not.
        """
        source = self._write(tmp_path, ",".join(str(n) for n in range(12)) + "\n")
        names = DataFile.for_path(source, file_type="DELIMITED_NO_HEADER").source_structure()

        assert names[:3] == ["col001", "col002", "col003"]
        assert names[-1] == "col012"
        assert names == sorted(names)

    def test_declared_names_replace_the_positional_ones(
        self, tmp_path: Path
    ) -> None:
        """A caller that knows the columns says so, as a format setting.

        `read` still knows nothing of any destination -- the names arrive at
        construction, the way a delimiter does.
        """
        source = self._write(tmp_path, "1,x\n")

        built = DataFile.for_path(
            source, file_type="DELIMITED_NO_HEADER", columns=["id", "name"],
        )

        assert built.source_structure() == ["id", "name"]
        assert built.read() == [{"id": "1", "name": "x"}]

    def test_a_ragged_row_is_refused_rather_than_truncated(
        self, tmp_path: Path
    ) -> None:
        """Position is the only identity here, so width is the only check.

        Zipping a short row to the names drops its last columns silently --
        the same class of loss the format was migrated to stop.
        """
        source = self._write(tmp_path, "1,x\n2\n")

        with pytest.raises(DataStructureError) as raised:
            DataFile.for_path(source, file_type="DELIMITED_NO_HEADER").validate()

        assert "row 2" in str(raised.value)

    def test_a_width_that_disagrees_with_the_destination_is_refused(
        self, tmp_path: Path
    ) -> None:
        source = self._write(tmp_path, "1,x\n")

        with pytest.raises(DataStructureError) as raised:
            DataFile.for_path(
                source, file_type="DELIMITED_NO_HEADER"
            ).read_validated(["a", "b", "c"])

        assert raised.value.validation_name == "load_header"

    def test_an_empty_file_has_no_columns_and_no_rows(
        self, tmp_path: Path
    ) -> None:
        """Consistent with the header file, which answers [] the same way."""
        source = self._write(tmp_path, "")
        built = DataFile.for_path(source, file_type="DELIMITED_NO_HEADER")

        assert built.source_structure() == []
        assert built.read() == []
        built.validate()                       # nothing to disagree with


class TestDelimitedHeaderDiscovery:
    """Which line is the header is a CSV setting (#1133): first, exact, or located."""

    def test_by_default_the_located_header_is_used(self, tmp_path: Path) -> None:
        source = _csv(tmp_path, "\na,b\n1,2\n")

        assert DataFile.for_path(source).read() == [{"a": "1", "b": "2"}]

    def test_a_required_header_is_located_below_a_preamble(self, tmp_path: Path) -> None:
        """The line holding the named column is the header, wherever it sits."""
        source = _csv(tmp_path, "Report of trades\nexported today\nid,trade_date\n1,2026-01-02\n")

        rows = DataFile.for_path(source, required_header=["trade_date"]).read()

        assert rows == [{"id": "1", "trade_date": "2026-01-02"}]

    def test_a_none_delimiter_is_detected(self, tmp_path: Path) -> None:
        source = _csv(tmp_path, "id;trade_date\n1;2026-01-02\n")

        rows = DataFile.for_path(source, delimiter=None, required_header=["trade_date"]).read()

        assert rows == [{"id": "1", "trade_date": "2026-01-02"}]


class TestTheDateRangeRuleReadsThroughTheCsvFile:
    """The movement rule decides the name; the CSV file reads the records (#1133)."""

    def test_it_finds_the_range_below_a_preamble(self, tmp_path: Path) -> None:
        from datetime import datetime

        from rey_lib.files.file_utils import _read_date_range_from_column

        source = _csv(tmp_path, "Report\nid;day\n1;2026-01-05\n2;2026-01-02\n3;bad\n", name="feed.csv")

        assert _read_date_range_from_column(source, "day", "%Y-%m-%d") == (
            datetime(2026, 1, 2), datetime(2026, 1, 5))

    def test_a_missing_column_is_refused_by_name(self, tmp_path: Path) -> None:
        from rey_lib.data.errors import DataStructureError
        from rey_lib.files.file_utils import _read_date_range_from_column

        source = _csv(tmp_path, "id,other\n1,x\n", name="feed.csv")

        with pytest.raises(DataStructureError, match="Column 'day' not found"):
            _read_date_range_from_column(source, "day", "%Y-%m-%d")


class TestText:
    """What any file shows when it is looked at: its text, bounded and display-safe."""

    def test_a_file_no_format_claims_still_shows_its_text(self, tmp_path: Path) -> None:
        source = tmp_path / "module.py"
        source.write_text("x = 1\n", encoding="utf-8")

        shown = File.for_path(source).text(1_000)

        assert shown == {
            "name": "module.py", "size_bytes": 6, "truncated": False,
            "content": "x = 1\n", "blocked_reason": "",
        }

    def test_the_text_is_bounded_and_says_so(self, tmp_path: Path) -> None:
        source = _csv(tmp_path, "a,b\n1,2\n")

        shown = File.for_path(source).text(4)

        assert shown["content"] == "a,b\n"
        assert shown["truncated"] is True
        assert shown["size_bytes"] == 8

    def test_a_secret_file_shows_no_content(self, tmp_path: Path) -> None:
        source = tmp_path / "server.key"
        source.write_text("secret", encoding="utf-8")

        shown = File.for_path(source).text(1_000)

        assert shown["content"] == ""
        assert shown["blocked_reason"] == "Sensitive file preview is blocked."

    def test_a_binary_file_shows_no_content(self, tmp_path: Path) -> None:
        source = tmp_path / "blob.bin"
        source.write_bytes(b"ab\x00cd")

        shown = File.for_path(source).text(1_000)

        assert shown["content"] == ""
        assert shown["blocked_reason"] == "Binary file preview is not supported."

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

from rey_lib.files.data_file import (
    DataFile,
    DataFileStructureError,
    data_file_for,
    registered_formats,
)
from rey_lib.files.file_utils import (
    _SUFFIX_FILE_TYPES,
    file_type_for_suffix,
    get_reader,
)


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
    """Adding a format is a module, not an edit to a central list."""

    def test_every_format_registers_itself(self) -> None:
        """Discovery imports the submodules; no module lists them."""
        assert set(registered_formats()) == {
            "CSV", "DELIMITED_HEADER", "JSONL", "NDJSON",
        }

    def test_a_new_format_needs_no_change_to_any_existing_module(
        self, tmp_path: Path
    ) -> None:
        """THE INVARIANT, asserted rather than asserted-in-prose.

        Registering a subtype here -- in a test module, touching nothing --
        makes it constructible. That is the property the hierarchy exists for:
        a format is added by declaring one, never by editing a branch.
        """
        from rey_lib.files.data_file import data_file

        @data_file("TEST_ONLY_FORMAT")
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

        built = data_file_for(tmp_path / "x.anything",
                              file_type="TEST_ONLY_FORMAT")

        assert isinstance(built, _Throwaway)
        assert built.read() == [{"a": 1}]

    def test_aliases_register_together(self, tmp_path: Path) -> None:
        """JSONL and NDJSON are one format under two names.

        Registered together rather than normalised somewhere else, so there is
        no second place deciding they are the same thing.
        """
        path = _jsonl(tmp_path, {"a": 1})

        assert type(data_file_for(path, file_type="NDJSON")) is type(
            data_file_for(path, file_type="JSONL")
        )


class TestConstruction:
    """A declared type wins; a suffix answers when nothing was declared."""

    def test_the_declared_type_beats_the_suffix(self, tmp_path: Path) -> None:
        """Configuration knows things a filename does not."""
        path = _csv(tmp_path, "a,b\n1,x\n", name="misnamed.jsonl")

        assert data_file_for(path, file_type="CSV").file_type == "CSV"

    def test_the_suffix_answers_when_nothing_is_declared(
        self, tmp_path: Path
    ) -> None:
        """The direct case: someone names one file and that is the whole input."""
        assert data_file_for(_jsonl(tmp_path, {"a": 1})).file_type == "JSONL"
        assert data_file_for(_csv(tmp_path, "a\n1\n")).file_type == "CSV"

    def test_a_suffix_that_names_nothing_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        """Refused, not defaulted to CSV.

        Guessing would pick a reader that fails later and somewhere else,
        reporting a parse error for what is a configuration mistake.
        """
        path = tmp_path / "mystery.dat"
        path.write_text("a,b\n", encoding="utf-8")

        with pytest.raises(ValueError) as raised:
            data_file_for(path)

        assert ".dat" in str(raised.value)

    def test_an_unknown_declared_type_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        """Including XLSX, which is deliberately not in this hierarchy yet."""
        with pytest.raises(ValueError) as raised:
            data_file_for(tmp_path / "book.xlsx")

        assert "XLSX" in str(raised.value)


class TestTheSuffixMapCannotDrift:
    """It feeds get_reader, so it must speak get_reader's vocabulary."""

    def test_every_suffix_maps_to_a_token_get_reader_dispatches(
        self, tmp_path: Path
    ) -> None:
        """The estate already has several file-type vocabularies that
        disagree. This asserts the suffix map is not another: every token it
        can yield is one the reader actually accepts.
        """
        path = _csv(tmp_path, "a\n1\n", name="probe.csv")

        for suffix, token in _SUFFIX_FILE_TYPES.items():
            try:
                list(get_reader(path, file_type=token))
            except ValueError as exc:            # the "unsupported" refusal
                pytest.fail(f"{suffix} -> {token}: {exc}")
            except Exception:                    # noqa: BLE001
                pass                             # wrong CONTENT is fine here

    def test_the_suffix_is_read_forgivingly(self) -> None:
        """Case and a missing dot are not the caller's problem."""
        assert file_type_for_suffix(".JSONL") == "JSONL"
        assert file_type_for_suffix("csv") == "CSV"
        assert file_type_for_suffix("") == ""
        assert file_type_for_suffix(".nope") == ""


class TestDelimitedHeaderFile:
    """Columns named once, in order, before any row is read."""

    def test_source_structure_is_the_header(self, tmp_path: Path) -> None:
        assert data_file_for(
            _csv(tmp_path, "a,b,c\n1,2,3\n")
        ).source_structure() == ["a", "b", "c"]

    def test_a_matching_header_passes(self, tmp_path: Path) -> None:
        data_file_for(_csv(tmp_path, "a,b\n1,x\n")).validate(["a", "b"])

    def test_column_ORDER_matters_against_a_destination(
        self, tmp_path: Path
    ) -> None:
        """A header is an ordered artifact and the insert relies on the order.

        The deliberate difference from a keyed source, whose key order is
        incidental.
        """
        with pytest.raises(DataFileStructureError):
            data_file_for(_csv(tmp_path, "b,a\n1,x\n")).validate(["a", "b"])

    def test_with_no_destination_only_the_header_must_exist(
        self, tmp_path: Path
    ) -> None:
        """Nothing to compare against, so nothing is invented to compare to."""
        data_file_for(_csv(tmp_path, "a,b\n1,x\n")).validate(None)

    def test_a_file_with_no_header_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(DataFileStructureError) as raised:
            data_file_for(_csv(tmp_path, "\n\n")).validate(None)

        assert "no header" in str(raised.value)

    def test_it_validates_before_reading(self, tmp_path: Path) -> None:
        """The cheap order, and the reason this format has its own class.

        A wrong file costs one line, not a parse. Asserted by spying on the
        reader: it must never run for a header that does not match.
        """
        source = data_file_for(_csv(tmp_path, "a,b\n1,x\n"))
        source.read = lambda: pytest.fail("read before validation")  # type: ignore[method-assign]

        with pytest.raises(DataFileStructureError):
            source.read_validated(["WRONG"])


class TestJsonlFile:
    """Columns named on every record, so the check comes after the read."""

    def test_source_structure_is_the_first_records_keys(
        self, tmp_path: Path
    ) -> None:
        assert data_file_for(
            _jsonl(tmp_path, {"a": 1, "b": "x"})
        ).source_structure() == ["a", "b"]

    def test_values_keep_their_json_types(self, tmp_path: Path) -> None:
        """This format carries types; discarding them is a loss."""
        records = data_file_for(_jsonl(tmp_path, {"a": 1, "b": True})).read()

        assert records[0]["a"] == 1 and not isinstance(records[0]["a"], str)
        assert records[0]["b"] is True

    def test_key_order_does_not_matter(self, tmp_path: Path) -> None:
        """A JSON object's key order is incidental, unlike a CSV header's."""
        path = _jsonl(tmp_path, {"b": "x", "a": 1}, {"a": 2, "b": "y"})

        assert len(data_file_for(path).read_validated(["a", "b"])) == 2

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
        with pytest.raises(DataFileStructureError):
            data_file_for(_jsonl(tmp_path, *records)).read_validated(["a", "b"])

    def test_with_no_destination_the_FIRST_record_sets_the_contract(
        self, tmp_path: Path
    ) -> None:
        """Internal consistency: the file's claim about itself, held to.

        This is the check the create path has never had -- today an
        inconsistent file has its table created and then fails inside the
        insert, reporting a database error for a file defect.
        """
        path = _jsonl(tmp_path, {"a": 1, "b": "x"}, {"a": 2, "c": "y"})

        with pytest.raises(DataFileStructureError) as raised:
            data_file_for(path).read_validated(None)

        assert "record 2" in str(raised.value)
        assert "first record" in str(raised.value)

    def test_a_self_consistent_file_needs_no_destination(
        self, tmp_path: Path
    ) -> None:
        """What the create path asks: is this file coherent? Yes."""
        path = _jsonl(tmp_path, {"a": 1, "b": "x"}, {"b": "y", "a": 2})

        assert len(data_file_for(path).read_validated(None)) == 2

    def test_the_source_is_parsed_exactly_once(self, tmp_path: Path) -> None:
        """read_validated must not reread to validate.

        A second parse would be invisible to every other test here and would
        cost a full pass over every file on every load. This is the assertion
        that catches a later "tidy-up" reintroducing one.
        """
        path = _jsonl(tmp_path, {"a": 1}, {"a": 2})
        source = data_file_for(path)
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

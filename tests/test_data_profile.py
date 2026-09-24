"""What some data IS, described apart from the thing describing it.

A profile DESCRIBES a data object and is not owned by one, so nothing here
asks a file for its profile -- `profile_for` is the boundary, and it stays
the boundary when a stored profile becomes reachable.

The distinction these tests exist to hold is between two questions that look
like one:

    can I describe the shape?                  structure_complete
    can I rely on it instead of revalidating?  structure_validated
"""

from __future__ import annotations

import json
from pathlib import Path

from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.data.data_profile import (
    DataProfile,
    FieldProfile,
    ProfileField,
    profile_for,
)


def _csv(tmp_path: Path, text: str = "a,b\n1,2\n") -> Path:
    path = tmp_path / "source.csv"
    path.write_text(text, encoding="utf-8")
    return path


def _jsonl(tmp_path: Path, *records: dict) -> Path:
    path = tmp_path / "source.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _positional(tmp_path: Path, text: str = "1,2\n") -> Path:
    path = tmp_path / "source.txt"
    path.write_text(text, encoding="utf-8")
    return path


class TestTheStructureItDescribes:
    """Fields, in order, with the names the source states."""

    def test_a_header_becomes_ordered_fields(self, tmp_path: Path) -> None:
        profile = profile_for(data_file_for(_csv(tmp_path)))

        assert profile.fields == (
            ProfileField(name="a", ordinal=1),
            ProfileField(name="b", ordinal=2),
        )

    def test_ordinals_start_at_one_and_follow_the_source(
        self, tmp_path: Path
    ) -> None:
        """Ordinal is position in the SOURCE, which is what a reading binds to."""
        profile = profile_for(data_file_for(_csv(tmp_path, "z,y,x\n1,2,3\n")))

        assert [(f.name, f.ordinal) for f in profile.fields] == [
            ("z", 1), ("y", 2), ("x", 3),
        ]

    def test_a_source_with_no_fields_profiles_empty(self, tmp_path: Path) -> None:
        """An answer, not a failure."""
        profile = profile_for(data_file_for(_csv(tmp_path, "")))

        assert profile.fields == ()
        assert profile.structure_complete is False


class TestTheFlagsDescribeEvidenceNotCapability:
    """THE distinction. A routine a source owns but did not run is not proof."""

    def test_a_declared_structure_is_complete(self, tmp_path: Path) -> None:
        """A header names every column, in order, before a row is read."""
        assert profile_for(data_file_for(_csv(tmp_path))).structure_complete is True

    def test_an_inferred_structure_is_not_complete(self, tmp_path: Path) -> None:
        """Even though this source CAN validate every record.

        `KeyedFile` checks every record's key set when it validates -- and
        that routine did not run here. A capability is not evidence, so the
        profile must not claim the stronger answer.
        """
        source = data_file_for(_jsonl(tmp_path, {"a": 1, "b": 2}))
        profile = profile_for(source)

        assert profile.structure_complete is False
        assert profile.structure_validated is False
        # The capability really does exist -- that is the point.
        assert hasattr(source, "validate")

    def test_positional_names_are_not_a_declaration(self, tmp_path: Path) -> None:
        """col001..colNNN come from the first row's width, which can change."""
        source = data_file_for(
            _positional(tmp_path), file_type="DELIMITED_NO_HEADER",
        )

        assert profile_for(source).structure_complete is False

    def test_columns_a_caller_named_ARE_a_declaration(
        self, tmp_path: Path
    ) -> None:
        """Declared names say what every field is, in order, reading nothing."""
        source = data_file_for(
            _positional(tmp_path),
            file_type="DELIMITED_NO_HEADER",
            columns=["x", "y"],
        )

        assert profile_for(source).structure_complete is True

    def test_nothing_this_resolver_produces_is_validated(
        self, tmp_path: Path
    ) -> None:
        """THE LIMIT OF THIS INCREMENT, recorded rather than assumed.

        `profile_for` performs structural discovery only. It never reads the
        data to check it against the structure, so it can never claim it was
        checked -- and no profile it returns licenses skipping validation.
        """
        sources = [
            data_file_for(_csv(tmp_path)),
            data_file_for(_jsonl(tmp_path, {"a": 1})),
            data_file_for(_positional(tmp_path), file_type="DELIMITED_NO_HEADER"),
            data_file_for(
                _positional(tmp_path),
                file_type="DELIMITED_NO_HEADER",
                columns=["x", "y"],
            ),
        ]

        assert not any(profile_for(one).structure_validated for one in sources)

    def test_incomplete_but_validated_is_never_produced(
        self, tmp_path: Path
    ) -> None:
        """An incoherent state: whole-source validation ESTABLISHES structure.

        If the data was checked against a definition, that definition is
        known. So the two flags rise together or the stronger one stays down.
        """
        sources = [
            data_file_for(_csv(tmp_path)),
            data_file_for(_jsonl(tmp_path, {"a": 1})),
            data_file_for(_positional(tmp_path), file_type="DELIMITED_NO_HEADER"),
        ]

        for source in sources:
            profile = profile_for(source)
            assert not (profile.structure_validated and not profile.structure_complete)


class TestAConsumerNeedsOnlyTheProfile:
    """The point of the boundary: no consumer branches on a source's type."""

    def test_the_decision_reads_flags_and_never_the_source(
        self, tmp_path: Path
    ) -> None:
        """A consumer deciding whether it may skip reading asks ONE question.

        `structure_validated`, not `structure_complete` -- asking the other
        would trust a header, which declares columns without proving any
        later row has that width.
        """
        def _may_skip_reading(profile: DataProfile) -> bool:
            return profile.structure_validated

        for source in (
            data_file_for(_csv(tmp_path)),
            data_file_for(_jsonl(tmp_path, {"a": 1})),
        ):
            assert _may_skip_reading(profile_for(source)) is False


class TestItDescribesRatherThanBeingOwned:
    """No data object gains profile semantics."""

    def test_no_data_file_has_a_profile_accessor(self, tmp_path: Path) -> None:
        """The accessor was deliberately not added.

        A `profile()` on the file would make it responsible for profile
        lookup and creation, and would not generalise to a database-backed
        data object.
        """
        assert not hasattr(DataFile, "profile")
        assert not hasattr(data_file_for(_csv(tmp_path)), "profile")

    def test_the_source_supplies_a_primitive_not_a_profile(
        self, tmp_path: Path
    ) -> None:
        """`declares_structure` says what the file knows about ITSELF.

        The resolver decides what that means for a profile. The file looks
        nothing up, creates nothing and persists nothing.
        """
        assert data_file_for(_csv(tmp_path)).declares_structure is True
        assert data_file_for(_jsonl(tmp_path, {"a": 1})).declares_structure is False


class TestTheWholeProfileIsOneObject:
    """Structure and its readings, which is how the estate already stores it."""

    def test_readings_are_declared_and_empty(self, tmp_path: Path) -> None:
        """So the migration adds CONTENT, not shape.

        Clear and redacted readings are written under ONE profile identity
        today, differing only in the values their fields carry. Modelling
        them here means no second object appears when they are populated.
        """
        profile = profile_for(data_file_for(_csv(tmp_path)))

        assert profile.clear == ()
        assert profile.redacted == ()

    def test_a_reading_binds_to_its_field_by_name_and_ordinal(self) -> None:
        """Not by position against `fields`.

        A positional convention breaks silently the first time a reading
        covers a subset of the fields.
        """
        reading = FieldProfile(name="b", ordinal=2)

        assert (reading.name, reading.ordinal) == ("b", 2)

    def test_a_profile_with_no_readings_is_legitimate(self) -> None:
        assert DataProfile().clear == ()


class TestAReadingCarriesWhatWasMeasured:
    """The statistics the docstring promised would arrive with this work."""

    #: Every fact a reading can carry beyond its identity.
    _STATISTICS = (
        "detected_type", "blank_count", "min_length", "max_length",
        "min_decimal_places", "max_decimal_places", "min_numeric",
        "max_numeric", "min_date", "max_date", "sample_values",
        "null_like_values", "constant_value",
    )

    def test_a_reading_that_measured_nothing_is_still_a_reading(self) -> None:
        """Identity alone remains valid -- this ADDS, it does not require."""
        reading = FieldProfile(name="b", ordinal=2)

        for name in self._STATISTICS:
            assert getattr(reading, name) is None, name

    def test_absent_is_not_zero(self) -> None:
        """THE DISTINCTION THIS SHAPE EXISTS TO KEEP.

        `blank_count=0` found no blanks. `blank_count=None` did not look.
        A profiler that measured nothing could otherwise claim a clean
        column, which is why the store's columns are nullable too.
        """
        measured = FieldProfile(name="b", ordinal=1, blank_count=0)
        unmeasured = FieldProfile(name="b", ordinal=1)

        assert measured.blank_count == 0
        assert unmeasured.blank_count is None
        assert measured != unmeasured

    def test_a_date_reading_keeps_the_text_the_source_carried(self) -> None:
        """Not parsed to a date: the FORMAT is itself a fact about the source."""
        reading = FieldProfile(name="d", ordinal=1, min_date="2024-01-15")

        assert reading.min_date == "2024-01-15"

    def test_a_reading_is_frozen_like_the_rest(self) -> None:
        assert FieldProfile.__dataclass_params__.frozen


class TestTheProfileDescribesTheDelivery:
    """Descriptive facts, and the two kinds deliberately kept out."""

    def test_field_count_is_not_stored_beside_the_fields(self) -> None:
        """One answer, not two that can disagree.

        The stored one would be the one that disagreed -- which is exactly
        the defect where a 19-field header was recorded as a field_count
        of 2.
        """
        assert not hasattr(DataProfile(), "field_count")

        profile = DataProfile(fields=(ProfileField(name="a", ordinal=1),))
        assert len(profile.fields) == 1

    def test_identity_is_not_part_of_what_data_is(self) -> None:
        """Which stored profile this is belongs to the lifecycle, not here.

        A description carrying its own identity could not describe a source
        that has none.
        """
        profile = DataProfile()

        for name in ("data_profile_key", "file_manifest_id",
                     "installation_id", "run_id", "profile_key"):
            assert not hasattr(profile, name), name

    def test_row_count_is_the_delivery_not_the_structure(self) -> None:
        """Two deliveries of one layout differ here and are the same shape."""
        first = DataProfile(header_definition="a,b", row_count=5)
        second = DataProfile(header_definition="a,b", row_count=9_000)

        assert first.header_definition == second.header_definition
        assert first.row_count != second.row_count

    def test_a_profile_that_describes_nothing_still_constructs(self) -> None:
        profile = DataProfile()

        assert profile.header_definition == ""
        assert profile.row_count is None
        assert profile.distribution is None


class TestComparison:
    """Described content is comparable; provenance is not part of it."""

    def test_two_profiles_of_the_same_shape_describe_the_same_thing(self) -> None:
        fields = (ProfileField(name="a", ordinal=1),)

        assert DataProfile(fields=fields).fields == DataProfile(fields=fields).fields

    def test_drift_is_NOT_dataclass_equality(self) -> None:
        """The trap this records.

        Two profiles can describe identical data and differ on the evidence
        flags, because they were established differently -- a stored profile
        against a freshly derived one. Dataclass equality includes those
        flags, so it would report drift when nothing about the data changed.

        The projection that expresses content-only comparison arrives with
        drift detection. What matters here is that `__eq__` is not quietly
        adopted as meaning it.
        """
        fields = (ProfileField(name="a", ordinal=1),)
        derived = DataProfile(fields=fields, structure_complete=True)
        stored = DataProfile(
            fields=fields, structure_complete=True, structure_validated=True,
        )

        assert derived != stored                 # equality disagrees ...
        assert derived.fields == stored.fields   # ... while the content does not

    def test_fields_are_frozen(self) -> None:
        """Immutable, so a profile cannot be edited into disagreeing with
        whatever established it."""
        import dataclasses

        assert dataclasses.is_dataclass(DataProfile)
        assert DataProfile.__dataclass_params__.frozen
        assert ProfileField.__dataclass_params__.frozen
        assert FieldProfile.__dataclass_params__.frozen


class TestTheResolverIsAnAdapter:
    """`profile_for` uses what a data object already exposes.

    It is not a second implementation of file inspection. Every subtype
    already answered its structural question before this existed, so the
    resolver adapts those answers rather than reading files itself:

        DelimitedHeaderFile    source_structure() -> the header line
        JsonlFile / JsonFile   source_structure() -> first record's keys
        DelimitedNoHeaderFile  source_structure() -> declared or positional

    A subtype that lacked the right primitive would be a GAP TO RECORD, never
    a reason to add a parser here.
    """

    def test_it_reads_only_the_primitives_the_source_publishes(
        self, tmp_path: Path
    ) -> None:
        """Asserted by substitution: an object that is not a file at all,
        exposing only the two primitives, profiles correctly.

        If the resolver did any inspection of its own, this could not work --
        there is nothing here to inspect.
        """
        class _NotAFile:
            declares_structure = True

            def source_structure(self) -> list[str]:
                return ["a", "b"]

        profile = profile_for(_NotAFile())

        assert [f.name for f in profile.fields] == ["a", "b"]
        assert profile.structure_complete is True

    def test_it_depends_on_no_reader_parser_or_profiler(self) -> None:
        """THE SEMANTIC BOUNDARY, guarded over dependencies.

            may depend on   domain types and interfaces
            must not on     concrete file readers or parsers
            must not on     profiling implementation

        Named modules rather than a ban on `rey_lib.files` wholesale: a
        legitimate shared primitive could move into that package later, and a
        package-wide ban would forbid a dependency that was never the
        problem.

        Dependency-shaped rather than a scan for words -- a guard that read
        prose would fire on the docstrings explaining it.
        """
        from pathlib import Path as _Path

        import rey_lib.data.data_profile as module

        source = _Path(module.__file__).read_text(encoding="utf-8")
        imports = "\n".join(
            line for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        )

        forbidden = (
            "rey_lib.profiling",        # profiling implementation
            "rey_lib.files.csv",        # concrete delimited reader
            "rey_lib.files.json",       # concrete document reader
            "rey_lib.files.jsonl",
            "rey_lib.files.file_utils", # get_reader and the suffix map
            "rey_lib.files.primitive_file_io",
        )
        for name in forbidden:
            assert name not in imports, f"{name} -- this is an adapter"

"""The profiler and the store share ONE representation of what data is.

`_canonical_profile_structure` used to return a dict that was a second
`DataProfile` in everything but name -- fields, a clear reading and a
redacted one. Two representations of one concept drift, so it now returns
the shared object and `_persist_profile` reads it.

WHAT THIS IS NOT. The enriched profile dict that `enrich_csv_profile` builds
is untouched: it is the `.profile.json` artifact contract and carries hints
this object has no opinion about. Only the representation BETWEEN the
profiler and the store changed.

THE ACCEPTANCE CRITERION for that change is that nothing reaching the store
moved, so most of what is here compares arguments rather than objects.

Copied from the legacy file_operator tests and pointed at the Loader-owned
profiling, rey_lib.load.profile (row 589, step 5).
"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.data.data_profile import DataProfile, FieldProfile, ProfileField

from rey_lib.load import profile as workflow
from rey_lib.load.layouts.delimited import (
    build_clean_single_file_profile,
    build_csv_parts,
)

from tests.support.selecting_control import SelectingControl

#: Facts a redacted reading INVENTS. `redact_profile` draws them from
#: `secrets`, so they are unrepeatable by design and are asserted as present
#: rather than equal -- unlike the column facts, which redaction never touches.
_INVENTED_BY_REDACTION = (
    "min_numeric", "max_numeric", "min_date", "max_date",
    "sample_values", "null_like_values", "constant_value",
)

_HEADER = "asset_id,name,amount,opened_on,note,ratio,flag"


def _args(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        delimiter=",", encoding="utf-8", max_sample_rows=None,
        max_sample_values_per_column=None, redact_all=False,
        redact_columns=[], redact_masks={}, header_contains=None,
        skip_blank_lines=False, feed=None, profile_scope="single_file",
        source_files=[name], file_count=1,
    )


@pytest.fixture()
def profile(tmp_path: Path) -> DataProfile:
    """One profiled delimited file, as the persistence path builds it."""
    source = tmp_path / "asset.csv"
    source.write_text(
        f"{_HEADER}\n"
        "1,Alpha,100.50,2024-01-15,first note,0.125,Y\n"
        "2,Beta,-20.25,2024-02-20,,1.5,N\n"
        "3,Gamma,0,2024-03-01,third,-0.75,Y\n",
        encoding="utf-8",
    )
    # Sampling draws row order; seeded so a failure is about the code.
    random.seed(20260924)
    parts = build_csv_parts(source, _args(source.name), redact_rows=False)
    return workflow._canonical_profile_structure(
        build_clean_single_file_profile(parts, _args(source.name))
    )


class TestItIsTheSharedObject:
    """Not a dict that looks like one."""

    def test_the_profiler_returns_a_data_profile(self, profile) -> None:
        assert isinstance(profile, DataProfile)

    def test_its_parts_are_the_shared_types(self, profile) -> None:
        assert all(isinstance(one, ProfileField) for one in profile.fields)
        assert all(isinstance(one, FieldProfile) for one in profile.clear)
        assert all(isinstance(one, FieldProfile) for one in profile.redacted)


class TestStructureComesFromTheHeader:
    """The row that DECLARES the structure, not the rows that exhibit it."""

    def test_the_fields_are_the_declared_columns(self, profile) -> None:
        assert [one.name for one in profile.fields] == _HEADER.split(",")
        assert [one.ordinal for one in profile.fields] == list(range(1, 8))

    def test_the_field_count_is_the_header_s(self, profile) -> None:
        """THE DEFECT THIS PROTECTS: a 19-field header counted as 2.

        `clear` is built over the SAMPLE, so a short or ragged first data row
        sets its length. `fields` is built over the header, so the count
        derived from it describes the same structure header_definition does
        -- and both are profile identity.
        """
        assert len(profile.fields) == len(_HEADER.split(","))
        assert profile.header_definition == _HEADER

    def test_a_declared_header_is_a_complete_structure(self, profile) -> None:
        assert profile.structure_complete is True

    def test_a_uniform_file_records_that_its_rows_were_checked(
        self, profile
    ) -> None:
        """Every row carried the header's width, over the whole source."""
        assert profile.structure_validated is True


class TestBothReadingsAreOneProfile:
    """They differ only in the values their fields carry."""

    def test_the_two_readings_cover_the_same_fields(self, profile) -> None:
        assert [one.name for one in profile.clear] == \
               [one.name for one in profile.redacted]
        assert [one.ordinal for one in profile.clear] == \
               [one.ordinal for one in profile.redacted]

    def test_column_facts_are_identical_across_readings(self, profile) -> None:
        """Redaction changes VALUES, never the column's measured shape."""
        for clear, redacted in zip(profile.clear, profile.redacted):
            assert clear.detected_type == redacted.detected_type
            assert clear.blank_count == redacted.blank_count
            assert clear.min_length == redacted.min_length
            assert clear.max_length == redacted.max_length

    def test_a_reading_carries_the_statistics_it_measured(self, profile) -> None:
        first = profile.clear[0]

        assert first.name == "asset_id"
        assert first.detected_type == "integer"
        assert first.max_length is not None
        assert first.sample_values


class TestNothingTranslatesTwice:
    """One adapter boundary, and the store is on the far side of it."""

    def test_the_reading_already_speaks_the_store_s_names(self, profile) -> None:
        """So persisting is attribute reads, not a second vocabulary.

        Asserted against the mapping table's own right-hand side, which is
        what the store's parameters are named.
        """
        target_names = (
            set(workflow._FIELD_FROM_COLUMN.values())
            | set(workflow._FIELD_FROM_SAMPLE.values())
        )
        for name in target_names:
            assert hasattr(profile.clear[0], name), name

    def test_the_profiler_s_own_words_do_not_survive_the_boundary(
        self, profile
    ) -> None:
        """`type` becomes `detected_type` once, and never travels further."""
        assert not hasattr(profile.clear[0], "type")
        assert workflow._FIELD_FROM_COLUMN["type"] == "detected_type"

    def test_each_reading_carries_the_header_prepare_will_write(
        self, tmp_path: Path,
    ) -> None:
        """prepared_name is the observed name through prepare's own
        normalization; the observed name itself is kept as it was."""
        source = tmp_path / "trades.csv"
        source.write_text(
            "Run Date,Account,Security Description\n"
            "01/02/2024,Brokerage,Apple Inc\n",
            encoding="utf-8",
        )
        random.seed(20260924)
        parts = build_csv_parts(source, _args(source.name), redact_rows=False)
        built = workflow._canonical_profile_structure(
            build_clean_single_file_profile(parts, _args(source.name))
        )

        for reading in (built.clear, built.redacted):
            assert [one.name for one in reading] == [
                "Run Date", "Account", "Security Description",
            ]
            assert [one.prepared_name for one in reading] == [
                "run_date", "account", "security_description",
            ]

class TestWhatReachesTheStore:
    """THE ACCEPTANCE CRITERION: the arguments, not the objects."""

    @staticmethod
    def _persisted(profile: DataProfile) -> SelectingControl:
        control = SelectingControl()
        workflow._persist_profile(
            control, "key_under_test",
            {
                "profile_schema_version": 1,
                "source_hash": "abc123",
                "size_bytes": 239,
                "eligible_population_rows": 3,
                "profiler": {"application": "file_operator",
                             "application_version": "9.9.9"},
            },
            profile, 4242, ("asset_id",),
        )
        return control

    def test_one_profile_row_carries_the_header_and_its_count(
        self, profile
    ) -> None:
        row = self._persisted(profile).data_profiles[0]

        assert row["field_count"] == len(profile.fields)
        assert row["header_definition"] == _HEADER
        assert row["distribution"] == profile.distribution

    def test_both_readings_are_written_under_that_one_identity(
        self, profile
    ) -> None:
        control = self._persisted(profile)
        rows = control.data_profile_fields
        one_id = control.data_profiles[0]["data_profile_id"]

        assert {row["data_profile_id"] for row in rows} == {one_id}
        assert sorted({row["data_profile_field_type"] for row in rows}) == \
               ["clear", "redacted"]
        assert len(rows) == len(profile.clear) + len(profile.redacted)

    def test_every_measured_fact_crosses_under_its_own_name(
        self, profile
    ) -> None:
        """Each fact into a column of its own, which is what the store takes."""
        rows = self._persisted(profile).data_profile_fields
        clear = next(r for r in rows if r["data_profile_field_type"] == "clear")
        source = profile.clear[0]

        assert clear["field_name"] == source.name
        assert clear["ordinal"] == source.ordinal
        assert clear["detected_type"] == source.detected_type
        assert clear["blank_count"] == source.blank_count
        assert clear["min_length"] == source.min_length
        assert clear["max_length"] == source.max_length
        assert clear["sample_values"] == source.sample_values

    def test_the_prepared_name_crosses_into_the_store(self, profile) -> None:
        rows = self._persisted(profile).data_profile_fields
        clear = next(r for r in rows if r["data_profile_field_type"] == "clear")

        assert clear["field_name"] == "asset_id"
        assert clear["prepared_name"] == profile.clear[0].prepared_name == "asset_id"

    def test_the_profiled_type_is_asked_for_its_transform_definition(
        self, profile
    ) -> None:
        """Profiling leaves a file type that HAS a definition.

        The ask is what this layer owns. What the routine then writes -- one
        passthrough column per profile field, a disabled query row -- belongs to
        control.p_transform_maintain and is proved against the database, not
        reproduced here.
        """
        control = self._persisted(profile)
        stamped = control.file_types[control.data_profiles[0]["data_profile_id"]]

        assert control.transform_maintenance == [
            {"file_type_id": stamped, "mode": "ensure"},
        ]

    def test_re_profiling_still_asks_though_it_writes_no_fields(
        self, profile
    ) -> None:
        """The path self-healing exists for, and the one an early return lost.

        A second profile of the same group finds its field rows already
        complete and writes none. It must still ask, because that is exactly
        when a definition can be missing -- the file type was stamped by an
        earlier run that had no transform tables, or a row was deleted since.
        """
        control = self._persisted(profile)
        fields_after_first = len(control.data_profile_fields)

        workflow._persist_profile(
            control, "key_under_test",
            {
                "profile_schema_version": 1,
                "source_hash": "abc123",
                "size_bytes": 239,
                "eligible_population_rows": 3,
                "profiler": {"application": "file_operator",
                             "application_version": "9.9.9"},
            },
            profile, 4243, ("asset_id",),
        )

        assert len(control.data_profile_fields) == fields_after_first
        assert len(control.transform_maintenance) == 2
        assert {call["mode"] for call in control.transform_maintenance} == {"ensure"}

    def test_profiling_never_resets_a_definition(self, profile) -> None:
        """reset is destructive and is an administrative act, never a profile one."""
        control = self._persisted(profile)

        assert all(call["mode"] == "ensure"
                   for call in control.transform_maintenance)

    def test_a_redacted_row_carries_every_fact_it_should(
        self, profile
    ) -> None:
        """Present, not equal -- redaction invents these from `secrets`."""
        rows = self._persisted(profile).data_profile_fields
        redacted = next(
            r for r in rows if r["data_profile_field_type"] == "redacted"
        )

        for name in _INVENTED_BY_REDACTION:
            assert name in redacted, name

    def test_a_field_with_no_sample_still_writes_its_column_facts(self) -> None:
        """A real case, and why the statistics default to None rather than 0.

        The column was measured; no sample entry named it. Its measured
        shape still crosses; the value-bearing facts are absent, not zero.
        """
        readings = workflow._readings_of(
            [{"name": "solo", "type": "text", "blank_count": 2,
              "min_length": 1, "max_length": 4}],
            [],                                  # nothing names this column
        )

        assert len(readings) == 1
        assert readings[0].detected_type == "text"
        assert readings[0].blank_count == 2
        assert readings[0].sample_values is None
        assert readings[0].min_numeric is None


class TestASampleIsMatchedByNameNotPosition:
    """Two lists agreeing today is not a reason to depend on their order."""

    def test_a_reordered_sample_list_still_reaches_its_column(self) -> None:
        columns = [
            {"name": "a", "type": "integer"},
            {"name": "b", "type": "text"},
        ]
        samples = [                              # deliberately reversed
            {"column": "b", "constant_value": "B"},
            {"column": "a", "constant_value": "A"},
        ]

        readings = workflow._readings_of(columns, samples)

        assert [(one.name, one.constant_value) for one in readings] == \
               [("a", "A"), ("b", "B")]

    def test_ordinals_follow_the_columns_not_the_samples(self) -> None:
        readings = workflow._readings_of(
            [{"name": "a"}, {"name": "b"}],
            [{"column": "b"}, {"column": "a"}],
        )

        assert [(one.name, one.ordinal) for one in readings] == \
               [("a", 1), ("b", 2)]

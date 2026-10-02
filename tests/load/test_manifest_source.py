"""A governed file becomes a source by POPULATING objects that already exist.

Every test here is one of row 434's invariants, and each one is a SILENT
failure if it breaks: a second database call, a collapsed duplicate, a
re-derived mutation, a refused untyped manifest, a clear-reading assumption, a
transform invented from an empty declaration, a file read, a DTO handed out in
place of an object, a dropped persistence id, an owned secret, or a writable
file fact.

THE SHAPE OF THE FIXTURES IS THE CONTRACT'S. One row per profile field x
transform column, with parent values repeating -- so a 2-field, 2-column
context is four rows. That repetition is the agreed shape and the object
materialises it; it is not a defect the fixtures avoid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pytest

from rey_lib.data.column_transform import ColumnTransform, TransformPersistence
from rey_lib.data.data_profile import DataProfile, FieldProfile, ProfileField
from rey_lib.data.errors import DataStructureError
from rey_lib.files.data_file import DataFile

from rey_lib.load.manifest_source import ManifestSource
from rey_lib.errors.error_utils import ConfigError


class CountingReader:
    """The source-context contract, answered from rows, counting the calls.

    Substituted by PASSING IT IN, which is the whole reason the reader is
    injected: no ambient state, and the call count is observable -- which is
    what makes "exactly one call" a test rather than a claim.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = list(rows)
        self.calls: list[dict[str, Any]] = []

    def file_source_context(
        self,
        file_manifest_id: Optional[int] = None,
        file_mutation_id: Optional[int] = None,
        required: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append({
            "file_manifest_id": file_manifest_id,
            "file_mutation_id": file_mutation_id,
        })
        return self.rows


def _row(**overrides: Any) -> dict[str, Any]:
    """One context row, with every column the contract declares.

    Defaults describe a typed, profiled, transformed file. A test states only
    what it is about, so a failure names the thing under test rather than the
    fixture.
    """
    row: dict[str, Any] = {
        "file_manifest_id": 10,
        "file_mutation_id": 25,
        "file_type_id": 4,
        "data_profile_id": 7,
        "data_profile_field_id": 101,
        "transform_id": 20,
        "transform_column_id": 301,
        "installation_id": 1,
        "path": "/data/incoming/asset.txt",
        "base_path": "/data/incoming",
        "file_name": "asset.txt",
        "file_extension": ".txt",
        "checksum_sha256": "abc123",
        "size_bytes": 239,
        "source_name": "inbound",
        "record_type": "file",
        "action": "profiled",
        "status": "SUCCEEDED",
        "result": None,
        "mutated_ts": None,
        "classification": None,
        "is_classified": True,
        "file_type_key": "asset_csv",
        "file_type_name": "Asset CSV",
        "layout": "DELIMITED_HEADER",
        "signature": None,
        "type_header_definition": "asset_id,amount",
        "key_fields": None,
        "filename_match": None,
        "data_profile_key": "asset_csv:v1",
        "profile_field_count": 2,
        "profile_row_count": 3,
        "profile_header_definition": "asset_id,amount",
        "field_name": "asset_id",
        "field_ordinal": 1,
        "field_detected_type": "integer",
        "field_is_nullable": False,
        "field_type": "redacted",
        "transform_is_default": True,
        "transform_row_filter": None,
        "column_name": "asset_id",
        "source_column": "asset_id",
        "column_ordinal": 1,
        "column_datatype": "integer",
        "transform_type": "passthrough",
        "transform_config": None,
        "column_is_exported": True,
    }
    row.update(overrides)
    return row


#: TWO fields x TWO columns = FOUR rows, which is the contract's own shape.
def _joined_rows() -> list[dict[str, Any]]:
    fields = [
        {"data_profile_field_id": 101, "field_name": "asset_id",
         "field_ordinal": 1, "field_detected_type": "integer"},
        {"data_profile_field_id": 102, "field_name": "amount",
         "field_ordinal": 2, "field_detected_type": "decimal"},
    ]
    columns = [
        {"transform_column_id": 301, "column_name": "asset_id",
         "source_column": "asset_id", "column_ordinal": 1,
         "column_datatype": "integer"},
        {"transform_column_id": 302, "column_name": "amount",
         "source_column": "amount", "column_ordinal": 2,
         "column_datatype": "decimal"},
    ]
    return [_row(**field, **column) for field in fields for column in columns]


@pytest.fixture()
def reader() -> CountingReader:
    return CountingReader(_joined_rows())


@pytest.fixture()
def source(reader: CountingReader) -> ManifestSource:
    return ManifestSource.create(
        reader, file_manifest_id=10, file_type_id=4,
    )


class TestOneCallAndNoSecondLookup:
    """Invariants 1 and 6."""

    def test_construction_makes_exactly_one_call(self, reader) -> None:
        ManifestSource.create(reader, file_manifest_id=10, file_type_id=4)

        assert len(reader.calls) == 1

    def test_using_every_accessor_makes_no_further_call(
        self, source, reader
    ) -> None:
        """THE POINT OF ONE JOINED DATASET. Everything below is materialisation."""
        source.data_file()
        source.data_profile()
        source.column_transform()

        assert len(reader.calls) == 1

    def test_neither_identity_is_refused_before_any_call(self, reader) -> None:
        """Refused here as well as by the routine, so no call is made that
        cannot succeed."""
        with pytest.raises(ConfigError, match="file_manifest_id"):
            ManifestSource.create(reader)

        assert reader.calls == []


class TestTheResolvedQueryIsKeptAsReturned:
    """The governed read's resolved SQL is held unchanged, from the one call."""

    def test_the_returned_value_is_exposed_unchanged(self) -> None:
        resolved = (
            "SELECT * FROM read_csv('/data/incoming/asset.txt', header = true)\n"
            "  WHERE  amount > 0 "
        )
        reader = CountingReader([
            {**row, "resolved_query_sql": resolved} for row in _joined_rows()
        ])

        source = ManifestSource.create(reader, file_manifest_id=10, file_type_id=4)

        assert source.resolved_query_sql is resolved
        assert len(reader.calls) == 1

    def test_a_read_without_it_holds_none(self, source) -> None:
        assert source.resolved_query_sql is None


class TestTheRepeatedRowsMaterialise:
    """Invariant 2: parents repeat by design; children are distinct by ID."""

    def test_four_rows_are_two_fields_and_two_columns(self, source) -> None:
        profile = source.data_profile()
        transform = source.column_transform()

        assert len(profile.redacted) == 2
        assert len(transform.declaration["columns"]) == 2

    def test_a_repeated_ordinal_does_not_collapse_two_fields(self) -> None:
        """DEDUPED BY ID, NOT BY ORDINAL. data_profile_field.ordinal is nullable
        and unique only within a narrower key, so two fields may share one --
        and collapsing them would lose a column of the file.
        """
        rows = [
            _row(data_profile_field_id=101, field_name="a", field_ordinal=1),
            _row(data_profile_field_id=102, field_name="b", field_ordinal=1),
        ]
        source = ManifestSource.create(
            CountingReader(rows), file_manifest_id=10, file_type_id=4,
        )

        assert [one.name for one in source.data_profile().fields] == ["a", "b"]


class TestTheIdentitiesKeepTheirMeanings:
    """Invariants 3 and 4, and the supplied-versus-persisted rule."""

    def test_an_explicit_mutation_is_preserved(self) -> None:
        """The contract guarantees it; this records which identity opened it
        rather than re-deriving one."""
        rows = [_row(file_mutation_id=25)]
        source = ManifestSource.create(
            CountingReader(rows), file_mutation_id=25,
        )

        assert source.file_mutation_id == 25
        assert source.opened_by == "mutation"

    def test_a_manifest_entry_records_that_it_was_one(self, source) -> None:
        assert source.opened_by == "manifest"

    def test_an_untyped_manifest_is_a_valid_working_source(self) -> None:
        """A file with no type still opens. Not a default, and not the last
        type seen."""
        rows = [_row(
            file_type_id=None, data_profile_id=None,
            data_profile_field_id=None, transform_id=None,
            transform_column_id=None, file_type_key=None, layout=None,
        )]
        source = ManifestSource.create(CountingReader(rows), file_manifest_id=10)

        assert source.working_file_available is True
        assert source.persisted_file_type_id is None
        assert source.governing_scope_available is False

    def test_a_disagreeing_manifest_is_refused_naming_both(self) -> None:
        with pytest.raises(DataStructureError, match="99"):
            ManifestSource.create(
                CountingReader([_row(file_manifest_id=10)]),
                file_manifest_id=99,
            )

    def test_a_scope_the_file_does_not_belong_to_is_refused(self) -> None:
        """Supplied X against persisted Y. Refused rather than resolved in
        favour of either: an edit under the wrong scope reaches files nobody
        chose."""
        with pytest.raises(DataStructureError, match="file type 9"):
            ManifestSource.create(
                CountingReader([_row(file_type_id=4)]),
                file_manifest_id=10, file_type_id=9,
            )


class TestASuppliedNullScopeIsNeverPromoted:
    """THE SINGLE ASSERTION THAT CATCHES SILENT ACQUISITION.

    The contract returns the persisted type's transformation rows as FACTS even
    here. A caller that deliberately named no governing scope must not acquire
    one because the file happens to have a type.
    """

    @pytest.fixture()
    def unscoped(self) -> ManifestSource:
        return ManifestSource.create(
            CountingReader(_joined_rows()), file_manifest_id=10,
        )

    def test_the_source_opens(self, unscoped) -> None:
        assert unscoped.working_file_available is True

    def test_the_persisted_type_is_still_reported_as_a_fact(
        self, unscoped
    ) -> None:
        """Returned, not hidden -- the comparison is what it is returned for."""
        assert unscoped.persisted_file_type_id == 4

    def test_but_no_governing_scope_is_available(self, unscoped) -> None:
        assert unscoped.governing_scope_available is False
        assert unscoped.type_configuration_editable is False

    def test_and_no_transform_is_constructed_at_all(self, unscoped) -> None:
        """Not an empty one, and not the persisted one: None."""
        assert unscoped.column_transform() is None


class TestNoTransformIsAssumed:
    """Invariant 7, against the shape manifest 2 really has."""

    def test_a_type_and_profile_with_no_transform_is_ordinary(self) -> None:
        """VERIFIED LIVE on manifest 2: 17 redacted fields, zero transform
        columns. An empty transform is not an error."""
        rows = [_row(transform_id=None, transform_column_id=None)]
        source = ManifestSource.create(
            CountingReader(rows), file_manifest_id=10, file_type_id=4,
        )

        assert source.column_transform() is None
        assert len(source.data_profile().redacted) == 1


class TestItNeverReadsTheFile:
    """Invariant 8, asserted BY ABSENCE.

    Reading is DataFile's and is proved by the suites it already has.
    """

    def test_a_context_over_a_missing_path_still_resolves(self) -> None:
        """The regression test for this object growing a file read: nothing
        here touches the filesystem, so a path that does not exist is fine."""
        rows = [_row(path="/nowhere/at/all/absent.txt")]
        source = ManifestSource.create(
            CountingReader(rows), file_manifest_id=10, file_type_id=4,
        )

        assert source.working_file_available is True
        assert source.data_profile() is not None
        assert isinstance(source.data_file(), DataFile)


class TestNoDuplicateModel:
    """Invariant 9, asserted BY TYPE rather than by shape.

    A test accepting a look-alike would permit exactly the parallel model this
    object exists to avoid, and one accepting a declaration dict would permit
    the DTO.
    """

    def test_the_file_is_a_data_file(self, source) -> None:
        assert isinstance(source.data_file(), DataFile)

    def test_the_profile_is_a_data_profile(self, source) -> None:
        profile = source.data_profile()

        assert isinstance(profile, DataProfile)
        assert all(isinstance(one, ProfileField) for one in profile.fields)
        assert all(isinstance(one, FieldProfile) for one in profile.redacted)

    def test_the_transform_is_a_column_transform(self, source) -> None:
        assert isinstance(source.column_transform(), ColumnTransform)


class TestTheLayoutBeatsTheSuffix:
    """As a configured load's declared file_type does."""

    def test_a_declared_layout_overrides_what_the_suffix_says(self) -> None:
        """THE PATH SAYS .csv AND THE ESTATE SAYS JSONL, and the estate wins.

        Proved against the same suffix twice, so the difference can only come
        from the declared token: a `.csv` path read as JSONL because the file
        type declares it, and the same path read as CSV when it declares
        nothing.
        """
        declared = ManifestSource.create(
            CountingReader([_row(layout="JSONL", path="/data/x.csv")]),
            file_manifest_id=10, file_type_id=4,
        ).data_file()
        by_suffix = ManifestSource.create(
            CountingReader([_row(layout=None, path="/data/x.csv")]),
            file_manifest_id=10, file_type_id=4,
        ).data_file()

        assert type(declared) is not type(by_suffix)

    def test_no_layout_falls_back_to_the_suffix(self) -> None:
        """Which is data_file_for's own stated rule, not a second one here."""
        source = ManifestSource.create(
            CountingReader([_row(layout=None, path="/data/x.csv")]),
            file_manifest_id=10, file_type_id=4,
        )

        assert isinstance(source.data_file(), DataFile)


class TestTheProfileIsTheRedactedReading:
    """Invariant 5. The contract filters to it; this names it as such."""

    def test_redacted_is_populated_and_clear_is_empty(self, source) -> None:
        """The honest statement of what was read. Not a gap to fill from
        elsewhere -- the clear reading carries values from the file itself."""
        profile = source.data_profile()

        assert len(profile.redacted) == 2
        assert profile.clear == ()

    def test_the_field_count_is_not_copied_across(self, source) -> None:
        """DataProfile deliberately has no such attribute: it is len(fields),
        and a stored second answer is the one that would disagree."""
        profile = source.data_profile()

        assert not hasattr(profile, "field_count")
        assert len(profile.fields) == 2

    def test_a_stored_profile_never_claims_its_data_was_validated(
        self, source
    ) -> None:
        """Nothing in this path checked the data against the structure."""
        assert source.data_profile().structure_validated is False


class TestTheDeclarationIsTheExistingShape:
    """The stored columns map one-for-one onto what ColumnTransform reads."""

    def test_each_stored_column_becomes_its_declaration_entry(
        self, source
    ) -> None:
        first = source.column_transform().declaration["columns"][0]

        assert first["name"] == "asset_id"
        assert first["source"] == "asset_id"
        assert first["datatype"] == "integer"
        assert first["transform"] == {"type": "passthrough"}

    def test_the_operator_comes_from_the_column_not_the_json(self) -> None:
        """transform_type is NOT NULL in the store and IS the operator. A type
        inside the nullable config must not override the schema's answer."""
        rows = [_row(
            transform_type="numeric",
            transform_config={"type": "date", "strip_chars": ","},
        )]
        transform = ManifestSource.create(
            CountingReader(rows), file_manifest_id=10, file_type_id=4,
        ).column_transform()

        rule = transform.declaration["columns"][0]["transform"]
        assert rule["type"] == "numeric"
        assert rule["strip_chars"] == ","

    def test_absent_export_means_exported(self, source) -> None:
        """Which is what is_exported already means, so a column that never had
        an opinion is unchanged."""
        assert "export" not in source.column_transform().declaration["columns"][0]

    def test_a_stored_false_export_is_stated(self) -> None:
        rows = [_row(column_is_exported=False)]
        transform = ManifestSource.create(
            CountingReader(rows), file_manifest_id=10, file_type_id=4,
        ).column_transform()

        assert transform.declaration["columns"][0]["export"] is False

    def test_a_row_filter_survives_a_definition_with_no_columns(self) -> None:
        """Read from the parent row, not off a child: a transform may have a
        filter and no columns yet, and reading it off the children would lose
        it in exactly that case."""
        rows = [_row(
            transform_column_id=None,
            transform_row_filter={"type": "not_blank", "column": "asset_id"},
        )]
        transform = ManifestSource.create(
            CountingReader(rows), file_manifest_id=10, file_type_id=4,
        ).column_transform()

        assert transform.declaration["row_filter"]["type"] == "not_blank"


class TestTheHydratedTransformExecutes:
    """The proof the declaration is the shape ColumnTransform actually READS.

    Assembling something with the right keys is not the same as assembling the
    declaration, and only running it tells the two apart.
    """

    def test_a_seeded_passthrough_definition_returns_values_unchanged(
        self, source
    ) -> None:
        """THE CHAIN PROFILING OPENED, CLOSED. p_transform_maintain seeds
        `passthrough` with no config; it hydrates to {"type": "passthrough"};
        and that operator returns the value as it came -- no trim, no
        conversion, no coercion.
        """
        transform = source.column_transform()

        produced = transform.transform([
            {"asset_id": "  1  ", "amount": "100.50"},
        ])

        assert produced == [{"asset_id": "  1  ", "amount": "100.50"}]

    def test_the_declared_columns_are_the_output(self, source) -> None:
        transform = source.column_transform()

        assert transform.columns == ["asset_id", "amount"]


class TestEveryPersistenceIdSurvives:
    """Invariant 10. An id dropped here is only recoverable by reading again."""

    def test_the_definition_and_scope_identities_travel(self, source) -> None:
        held = source.column_transform().persistence

        assert isinstance(held, TransformPersistence)
        assert held.transform_id == 20
        assert held.file_type_id == 4

    def test_each_column_carries_its_own_row_id(self, source) -> None:
        held = source.column_transform().persistence

        assert held.column_ids == (301, 302)

    def test_a_rename_does_not_lose_the_id(self, source) -> None:
        """THE SHARPEST FORM OF THE RULE.

        A name is exactly what an edit may change, so an id found BY name would
        be lost by the most ordinary edit there is -- and the write would insert
        a second row and orphan the first. The id travels positionally with the
        column, so renaming it changes nothing about which row it is.
        """
        transform = source.column_transform()
        columns = transform.declaration["columns"]

        columns[0]["name"] = "gross_asset_id"

        assert transform.persistence.column_ids[0] == 301

    def test_a_column_with_no_stored_row_is_the_insert_case(self) -> None:
        """An absent id is legitimate and carries meaning: a column the author
        just added has none yet."""
        rows = [
            _row(transform_column_id=301, column_name="asset_id"),
            _row(transform_column_id=None, column_name="added_later"),
        ]
        transform = ManifestSource.create(
            CountingReader(rows), file_manifest_id=10, file_type_id=4,
        ).column_transform()

        # The unsaved column is not a stored child, so it is not materialised
        # from the joined rows -- there is nothing to dedupe it by. What matters
        # here is that the stored one keeps its id.
        assert transform.persistence.column_ids == (301,)


class TestPersistenceIsADeclaredCapability:
    """Invariant 13. Asked, never inferred from a null."""

    def test_a_db_hydrated_transform_can_persist(self, source) -> None:
        assert source.column_transform().is_persistable is True

    def test_a_yaml_declared_transform_cannot_and_does_not_raise(self) -> None:
        """The estate's most common case. Never a stored definition, so it has
        nowhere to be written back to -- an ANSWER, not a failure."""
        from_yaml = ColumnTransform({"columns": [
            {"name": "asset_id", "source": "asset_id",
             "transform": {"type": "passthrough"}},
        ]})

        assert from_yaml.is_persistable is False
        assert from_yaml.persistence is None

    def test_both_apply_their_rules_identically(self, source) -> None:
        """PERSISTENCE CAPABILITY, NOT TRANSFORM BEHAVIOUR. One object serves
        both declaration sources and nothing about applying rules reads the
        identities."""
        declaration = source.column_transform().declaration
        record = {"asset_id": "1", "amount": "2"}

        stored = source.column_transform().transform([record])
        yaml_like = ColumnTransform(declaration).transform([record])

        assert stored == yaml_like


class TestRuntimeDependenciesArePassedThroughNotOwned:
    """Invariant 11. A declaration describes work; it is not a credential."""

    def test_what_is_supplied_is_what_the_object_carries(self, source) -> None:
        context = object()
        transform = source.column_transform(
            context=context, secrets={"KEY_A": "value"},
        )

        assert transform.context is context
        assert transform.secrets == {"KEY_A": "value"}

    def test_supplying_neither_defaults_neither(self, source) -> None:
        """ManifestSource holds no secrets map and invents none."""
        transform = source.column_transform()

        assert transform.secrets == {}
        assert transform.context is None

    def test_an_encrypt_rule_keeps_its_key_NAME_and_no_value(self) -> None:
        """Which is why this is the right line to draw: the declaration can
        fully describe encryption while this object cannot perform it."""
        rows = [_row(
            transform_type="encrypt",
            transform_config={"key_env": "ASSET_KEY"},
        )]
        transform = ManifestSource.create(
            CountingReader(rows), file_manifest_id=10, file_type_id=4,
        ).column_transform()

        rule = transform.declaration["columns"][0]["transform"]
        assert rule == {"type": "encrypt", "key_env": "ASSET_KEY"}
        assert transform.secrets == {}


class TestEverythingElseIsReadOnly:
    """Invariant 12, asserted by ABSENCE of a write path."""

    @pytest.mark.parametrize("name", [
        "save", "save_file", "save_profile", "update_path", "set_layout",
        "save_manifest", "save_mutation", "save_profile_fields",
    ])
    def test_no_write_method_exists_to_be_called_by_mistake(
        self, source, name
    ) -> None:
        """File, mutation and profile state are owned by profiling, inventory
        and conversion. This object is a reader of those."""
        assert not hasattr(source, name)

    def test_the_file_facts_are_present_as_context(self, source) -> None:
        assert source.file_facts["file_name"] == "asset.txt"
        assert source.file_facts["checksum_sha256"] == "abc123"


class TestTheSeamForOtherOrigins:
    """Invariant 14. Nothing here assumes the database as the origin.

    Future YAML support extends this object rather than forking a second source
    or Loader path -- backlog row 446.
    """

    def test_the_transform_object_is_the_same_class_either_way(
        self, source
    ) -> None:
        """So no consumer branches on where a declaration came from."""
        stored = source.column_transform()
        from_yaml = ColumnTransform(stored.declaration)

        assert type(stored) is type(from_yaml)

    def test_only_the_capability_distinguishes_them(self, source) -> None:
        stored = source.column_transform()
        from_yaml = ColumnTransform(stored.declaration)

        assert stored.is_persistable != from_yaml.is_persistable
        assert stored.declaration == from_yaml.declaration


class TestAnEmptyReadIsNotAnEmptyConfiguration:
    """The contract raises for every no-such-file case."""

    def test_no_rows_is_refused_rather_than_read_as_no_configuration(
        self,
    ) -> None:
        """"This file has no current state" and "this file has no
        transformation configuration" are different answers."""
        with pytest.raises(DataStructureError, match="no rows"):
            ManifestSource.create(CountingReader([]), file_manifest_id=10)


class TestAdoptingThePersistedType:
    """A caller that could not carry a type may govern by the one the file has.

    Opt-in, and from the rows already read -- so it costs no second read, and a
    caller that did not ask for it keeps today's supplied-only rule.
    """

    def test_by_default_no_supplied_type_means_no_transform(self) -> None:
        source = ManifestSource.create(CountingReader([_row()]), file_manifest_id=10)

        assert source.governing_scope_available is False
        assert source.column_transform() is None

    def test_adopting_it_governs_by_the_persisted_type_in_one_read(self) -> None:
        reader = CountingReader([_row()])

        source = ManifestSource.create(
            reader, file_manifest_id=10, adopt_persisted_type=True,
        )

        assert len(reader.calls) == 1
        assert source.requested_file_type_id == 4
        transform = source.column_transform()
        assert transform is not None
        assert transform.persistence.file_type_id == 4

    def test_a_supplied_type_still_wins_and_is_still_validated(self) -> None:
        with pytest.raises(DataStructureError, match="governed by 4"):
            ManifestSource.create(
                CountingReader([_row()]), file_manifest_id=10, file_type_id=9,
                adopt_persisted_type=True,
            )

    def test_a_file_with_no_persisted_type_stays_ungoverned(self) -> None:
        source = ManifestSource.create(
            CountingReader([_row(file_type_id=None)]), file_manifest_id=10,
            adopt_persisted_type=True,
        )

        assert source.governing_scope_available is False
        assert source.column_transform() is None


class TestWhetherTheTransformIsTheDefault:
    """A fact the contract returns and the source reports, applying it nowhere."""

    @pytest.mark.parametrize("default", [True, False])
    def test_it_is_read_from_the_row(self, default: bool) -> None:
        source = ManifestSource.create(
            CountingReader([_row(transform_is_default=default)]),
            file_manifest_id=10, file_type_id=4,
        )

        assert source.transform_is_default is default

    def test_a_transform_nobody_selected_and_that_is_not_the_default_is_unresolved(
        self,
    ) -> None:
        """No default: nothing is selected, and no lowest id is taken."""
        source = ManifestSource.create(
            CountingReader([_row(transform_is_default=False)]),
            file_manifest_id=10, file_type_id=4,
        )

        assert source.selected_transform_id is None
        assert source.column_transform() is None

    def test_a_selected_transform_is_returned_as_stored_default_or_not(self) -> None:
        source = ManifestSource.create(
            CountingReader([_row(transform_is_default=False)]),
            file_manifest_id=10, file_type_id=4,
        )

        source.select_transform(20)

        assert source.selected_transform_id == 20
        assert source.column_transform() is not None

    def test_selecting_reads_nothing(self) -> None:
        reader = CountingReader([_row(transform_is_default=False)])
        source = ManifestSource.create(reader, file_manifest_id=10, file_type_id=4)

        source.select_transform(20)
        source.select_transform(None)

        assert len(reader.calls) == 1


class TestTheMutationChoicesAreKeptAsReturned:

    def test_they_are_exposed_as_returned(self) -> None:
        choices = [{"file_mutation_id": 25, "result": "inventoried",
                    "path": "/a", "resolved_query_sql": "SELECT 1"}]
        reader = CountingReader([
            {**row, "mutation_choices": choices} for row in _joined_rows()
        ])

        source = ManifestSource.create(reader, file_manifest_id=10, file_type_id=4)

        assert source.mutation_choices == choices
        assert len(reader.calls) == 1

    def test_a_read_without_them_holds_none(self, source) -> None:
        assert source.mutation_choices == []

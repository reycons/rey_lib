"""Tests for pure source-file classification lifecycle serialization.

Copied from the legacy file_operator tests and pointed at the Loader-owned
implementation, rey_lib.load.classify (row 589, step 2).
"""

from __future__ import annotations

from dataclasses import replace

from rey_lib.load.classify import (
    SourceClassificationCandidate,
    SourceClassificationOutcome,
    classify_source_candidates,
    resolve_classification_source_configs,
    serialize_source_file_classification_record,
)


def _candidate(
    *,
    classification_type: str,
    value: str = "Example.csv",
) -> SourceClassificationCandidate:
    entry = {
        "name": "generic",
        "enabled": True,
        "classification_type": classification_type,
        "file_selection": {
            "procedure": "classification_candidates",
            "source_field": "file.file_name",
        },
        "variables": [
            {
                "name": "captured_name",
                "source": "classification",
                "required": True,
            }
        ],
        "classification": {
            "path_regex": r"(?P<captured_name>.+)[.]csv",
        },
    }
    config = resolve_classification_source_configs({"sources": [entry]})[0]
    return SourceClassificationCandidate(
        classification=config,
        source_record_type="source_file_inventory",
        # A row as the selection routine returns it: the governed identity and
        # the file's current path, flat.
        manifest_record={
            "file_manifest_id": 7,
            "file_mutation_id": 7,
            "path": f"/data/bny/inbox/{value}",
        },
        value=value,
        file_id=7,
        status="ready",
    )


def test_classified_record_copies_generic_configured_classification_type() -> None:
    candidate = _candidate(classification_type="another_classification_mechanism")
    outcome = classify_source_candidates((candidate,)).outcomes[0]

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=123,
        recorded_at="2026-07-30T07:15:00.000Z",
        # The producer is the recording application, the runtime context's.
        application="rey_loader",
    )

    assert record == {
        "file_id": 7,
        "recorded_at": "2026-07-30T07:15:00.000Z",
        "record_type": "source_file_classification",
        "status": "success",
        "result": "classified",
        "evidence": {
            "run_log_id": 123,
        },
        "file": {
            "path": "/data/bny/inbox/Example.csv",
            "file_name": "Example.csv",
            "base_name": "Example",
            "file_extension": "csv",
        },
        "lineage": {"source_record_id": 7},
        "classification": {
            "type": "another_classification_mechanism",
            "source_field": "file.file_name",
            "values": {"captured_name": "Example"},
        },
        "producer": {
            "application": "rey_loader",
            "operation": "classification",
        },
    }
    assert "record_id" not in record
    # Flat identity leads every governed record; its objects follow.
    assert list(record) == [
        "file_id",
        "recorded_at",
        "record_type",
        "status",
        "evidence",
        "file",
        "lineage",
        "classification",
        "result",
        "producer",
    ]


def test_record_carries_no_legacy_field_names() -> None:
    """Every field the canonical layout groups is gone from the record root."""
    candidate = _candidate(classification_type="file_name_regex")
    outcome = classify_source_candidates((candidate,)).outcomes[0]

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )

    for legacy in (
        "schema_version",
        "classification_type",
        "source_field",
        "values",
        "file_extension",
        "application_name",
        "reason_code",
        "reason",
        "source_record_type",
        "source_record_id",
    ):
        assert legacy not in record, legacy


def test_current_configured_value_is_serialized_without_special_case() -> None:
    candidate = _candidate(classification_type="file_name_regex")
    outcome = classify_source_candidates((candidate,)).outcomes[0]

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )

    assert record["classification"]["type"] == "file_name_regex"


def test_file_extension_is_normalized_final_suffix_not_a_regex_variable() -> None:
    cases = [
        ("report.xlsx", "xlsx"),
        ("trades.CSV", "csv"),
        ("archive.tar.gz", "gz"),
        ("README", ""),
    ]

    for value, expected in cases:
        candidate = _candidate(
            classification_type="file_name_regex",
            value=value,
        )
        outcome = SourceClassificationOutcome(
            candidate=candidate,
            status="rejected",
            values={},
            reason_code="path_regex_mismatch",
            reason="Configured classification regex did not match the source field.",
        )

        record = serialize_source_file_classification_record(
            outcome,
            run_log_id=1,
            recorded_at="2026-07-30T07:15:00.000Z",
        )

        assert record["file"]["file_extension"] == expected
        assert "file_extension" not in outcome.values
        assert "file_type" not in record
        assert [variable.name for variable in candidate.classification.variables] == [
            "captured_name"
        ]


def test_file_extension_falls_back_to_configured_source_path() -> None:
    candidate = _candidate(
        classification_type="file_name_regex",
        value="/incoming/trades.CSV",
    )
    candidate = replace(
        candidate,
        manifest_record={
            "file_manifest_id": 7,
            "file_mutation_id": 7,
            "path": "/incoming/trades.CSV",
        },
    )
    outcome = SourceClassificationOutcome(
        candidate=candidate,
        status="rejected",
        values={},
        reason_code="path_regex_mismatch",
        reason="Configured classification regex did not match the source field.",
    )

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )

    assert record["file"]["file_extension"] == "csv"


def test_business_record_type_remains_separate_from_lifecycle_record_type() -> None:
    entry = {
        "name": "generic",
        "enabled": True,
        "classification_type": "file_name_regex",
        "file_selection": {
            "procedure": "classification_candidates",
            "source_field": "file.file_name",
        },
        "variables": [
            {
                "name": "record_type",
                "source": "classification",
                "required": True,
                "values": ["Hold", "Tran"],
            }
        ],
        "classification": {
            "path_regex": r"Example(?P<record_type>Hold|Tran)[.]xlsx",
        },
    }
    config = resolve_classification_source_configs({"sources": [entry]})[0]
    candidate = SourceClassificationCandidate(
        classification=config,
        source_record_type="source_file_inventory",
        manifest_record={
            "file_manifest_id": 7,
            "file_mutation_id": 7,
            "path": "/data/bny/inbox/ExampleHold.xlsx",
        },
        value="ExampleHold.xlsx",
        file_id=7,
        status="ready",
    )
    outcome = classify_source_candidates((candidate,)).outcomes[0]

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )

    assert record["record_type"] == "source_file_classification"
    assert record["classification"]["type"] == "file_name_regex"
    assert record["file"]["file_extension"] == "xlsx"
    assert record["classification"]["values"] == {"record_type": "Hold"}
    assert "file_type" not in record


def test_rejected_record_also_copies_configured_classification_type() -> None:
    candidate = _candidate(classification_type="generic_mechanism")
    outcome = SourceClassificationOutcome(
        candidate=candidate,
        status="rejected",
        values={},
        reason_code="path_regex_mismatch",
        reason="Configured classification regex did not match the source field.",
    )

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=2,
        recorded_at="2026-07-30T07:15:00.000Z",
    )

    assert record["classification"]["type"] == "generic_mechanism"
    # status is execution state; the outcome is the result, as text. A
    # rejection produced nothing, so it carries no result and writes no
    # mutation -- its reason_code and prose live on the evidence record, which
    # is their only home.
    assert record["status"] == "failed"
    assert "result" not in record
    assert "values" not in record["classification"]


def test_lineage_identifies_the_exact_source_manifest_record() -> None:
    """file_id is the file occurrence; lineage is the record it came from."""
    candidate = _candidate(classification_type="file_name_regex")
    outcome = classify_source_candidates((candidate,)).outcomes[0]

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )

    assert record["lineage"] == {"source_record_id": 7}
    # record_id is globally unique, so the reference carries no record type.
    assert "source_record_type" not in record["lineage"]
    assert record["file_id"] == candidate.manifest_record["file_manifest_id"]


def test_classification_record_carries_the_selected_inventory_path() -> None:
    """The path is copied from the selected row, not derived or rebuilt."""
    candidate = _candidate(classification_type="file_name_regex")
    inventory_path = candidate.manifest_record["path"]
    outcome = classify_source_candidates((candidate,)).outcomes[0]

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )

    assert record["file"]["path"] == inventory_path
    assert record["file"]["file_extension"] == "csv"
    # Lineage stays evidence: the path is readable without following it.
    assert record["lineage"] == {"source_record_id": 7}


def test_a_rejected_record_still_carries_the_path() -> None:
    """A consumer addresses the file the same way whatever the outcome."""
    candidate = _candidate(classification_type="file_name_regex", value="odd.csv")
    outcome = SourceClassificationOutcome(
        candidate=candidate,
        status="rejected",
        values={},
        reason_code="path_regex_mismatch",
        reason="Configured classification regex did not match the source field.",
    )

    record = serialize_source_file_classification_record(
        outcome,
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )

    assert record["file"]["path"] == "/data/bny/inbox/odd.csv"


def test_both_reducer_inputs_expose_the_same_path_address() -> None:
    """A classified source and a derived CSV are read through one address."""
    from rey_lib.files import serialize_source_file_mutation

    candidate = _candidate(classification_type="file_name_regex")
    classification = serialize_source_file_classification_record(
        classify_source_candidates((candidate,)).outcomes[0],
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )
    mutation = serialize_source_file_mutation(
        action="create",
        status="success",
        destination_path="/data/northern_trust/work/converted_csv/PREPATran.csv",
        run_log_id=2,
        application_name="file_operator",
        file_id=7,
        conversion={"operator": "excel_conversion", "name": "all"},
    )

    def resolve(record: dict, address: str) -> object:
        current: object = record
        for part in address.split("."):
            assert isinstance(current, dict), address
            current = current[part]
        return current

    assert [resolve(r, "file.path") for r in (classification, mutation)] == [
        "/data/bny/inbox/Example.csv",
        "/data/northern_trust/work/converted_csv/PREPATran.csv",
    ]
    assert [resolve(r, "file.file_extension") for r in (classification, mutation)] == [
        "csv",
        "csv",
    ]


def test_base_name_is_carried_forward_unchanged_by_both_record_types() -> None:
    """Classification and mutation report the inventoried base name verbatim."""
    from rey_lib.files import serialize_source_file_mutation

    file_name = "account.positions.May26.csv"
    candidate = _candidate(classification_type="file_name_regex", value=file_name)
    classification = serialize_source_file_classification_record(
        classify_source_candidates((candidate,)).outcomes[0],
        run_log_id=1,
        recorded_at="2026-07-30T07:15:00.000Z",
    )
    mutation = serialize_source_file_mutation(
        action="move",
        status="success",
        source_path=f"/data/bny/inbox/{file_name}",
        destination_path=f"/data/bny/processing/{file_name}",
        run_log_id=2,
    )

    for record in (classification, mutation):
        assert record["file"]["file_name"] == file_name
        assert record["file"]["base_name"] == "account.positions.May26"
        assert record["file"]["file_extension"] == "csv"

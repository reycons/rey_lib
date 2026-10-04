"""Checkpoint 3 tests for generic in-memory source classification.

Copied from the legacy file_operator tests and pointed at the Loader-owned
implementation, rey_lib.load.classify (row 589, step 2).
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.load.classify import (
    FileClassificationSourceConfig,
    SourceClassificationCandidate,
    classify_source_candidates,
    resolve_classification_source_configs,
)


def _variable(
    name: str,
    *,
    required: bool = True,
    values: list[str] | None = None,
    format: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "name": name,
        "source": "classification",
        "required": required,
    }
    if values is not None:
        result["values"] = values
    if format is not None:
        result["format"] = format
    return result


def _config(
    regex: str,
    variables: list[dict[str, object]],
) -> FileClassificationSourceConfig:
    entry = {
        "name": "generic",
        "enabled": True,
        "classification_type": "configured_test_mechanism",
        # Which routine names this source's work, and which field of the row
        # it returns holds the file.
        "file_selection": {
            "operation": "file_classification",
            "source_field": "path",
        },
        "variables": variables,
        "classification": {"path_regex": regex},
    }
    return resolve_classification_source_configs({"sources": [entry]})[0]


def _candidate(
    config: FileClassificationSourceConfig,
    value: str | None,
    *,
    status: str = "ready",
    reason_code: str | None = None,
    reason: str | None = None,
    record_id: int = 1,
) -> SourceClassificationCandidate:
    return SourceClassificationCandidate(
        classification=config,
        source_record_type="source_file_inventory",
        manifest_record={
            "record_id": record_id,
            "record_type": "source_file_inventory",
            config.source_field: value,
        },
        value=value,
        file_id=record_id,
        status=status,
        reason_code=reason_code,
        reason=reason,
    )


def test_named_groups_are_extracted_in_declared_variable_order(run_log) -> None:
    config = _config(
        (
            r"(?P<source>[^/]+)/(?P<kind>[^/]+)/"
            r"(?P<period>[A-Za-z]{3}[0-9]{2})(?P<suffix>.*)"
        ),
        [
            _variable("kind", values=["Hold", "Tran"]),
            _variable("source"),
            _variable("period", format="mmmyy"),
            _variable("suffix", required=False),
        ],
    )

    result = classify_source_candidates((_candidate(config, "bank/Hold/May26_tail"),))

    outcome = result.outcomes[0]
    assert outcome.status == "classified"
    assert list(outcome.values) == ["kind", "source", "period", "suffix"]
    assert outcome.values == {
        "kind": "Hold",
        "source": "bank",
        "period": "May26",
        "suffix": "_tail",
    }


def test_regex_mismatch_is_a_rejected_outcome(run_log) -> None:
    config = _config(r"(?P<value>accepted)", [_variable("value")])
    outcome = classify_source_candidates((_candidate(config, "different"),)).outcomes[0]

    assert outcome.status == "rejected"
    assert outcome.reason_code == "path_regex_mismatch"
    assert outcome.values == {}


def test_required_absent_capture_is_rejected(run_log) -> None:
    config = _config(
        r"(?:(?P<prefix>prefix)-)?(?P<value>value)",
        [_variable("prefix"), _variable("value")],
    )
    outcome = classify_source_candidates((_candidate(config, "value"),)).outcomes[0]

    assert outcome.status == "rejected"
    assert outcome.reason_code == "missing_required_variable"
    assert outcome.values == {"prefix": None, "value": None}


def test_required_empty_capture_is_rejected(run_log) -> None:
    config = _config(r"(?P<value>.*)", [_variable("value")])
    outcome = classify_source_candidates((_candidate(config, ""),)).outcomes[0]

    assert outcome.status == "rejected"
    assert outcome.reason_code == "missing_required_variable"


def test_optional_absent_and_empty_captures_are_accepted(run_log) -> None:
    config = _config(
        r"(?P<value>base)(?:/(?P<suffix>.*))?",
        [_variable("value"), _variable("suffix", required=False)],
    )
    result = classify_source_candidates(
        (
            _candidate(config, "base", record_id=1),
            _candidate(config, "base/", record_id=2),
        )
    )

    assert [outcome.status for outcome in result.outcomes] == [
        "classified",
        "classified",
    ]
    assert [outcome.values["suffix"] for outcome in result.outcomes] == [None, ""]


def test_disallowed_value_is_rejected(run_log) -> None:
    config = _config(
        r"(?P<kind>[^/]+)",
        [_variable("kind", values=["Hold", "Tran"])],
    )
    outcome = classify_source_candidates((_candidate(config, "Other"),)).outcomes[0]

    assert outcome.status == "rejected"
    assert outcome.reason_code == "value_not_allowed"
    assert outcome.values == {"kind": None}


def test_invalid_format_is_rejected(run_log) -> None:
    config = _config(
        r"(?P<period>[^/]+)",
        [_variable("period", format="mmmyy")],
    )
    outcome = classify_source_candidates((_candidate(config, "202605"),)).outcomes[0]

    assert outcome.status == "rejected"
    assert outcome.reason_code == "format_invalid"
    assert outcome.values == {"period": None}


def test_candidate_rejection_is_preserved_without_regex_execution(run_log) -> None:
    config = _config(r"(?P<value>will-not-match)", [_variable("value")])
    candidate = _candidate(
        config,
        "anything",
        status="rejected",
        reason_code="missing_source_field",
        reason="missing",
    )
    outcome = classify_source_candidates((candidate,)).outcomes[0]

    assert outcome.status == "rejected"
    assert outcome.reason_code == "missing_source_field"
    assert outcome.reason == "missing"


def test_aggregate_counts_and_outcome_order_are_deterministic(run_log) -> None:
    config = _config(r"(?P<value>yes)", [_variable("value")])
    candidates = (
        _candidate(config, "yes", record_id=1),
        _candidate(config, "no", record_id=2),
        _candidate(config, "yes", record_id=3),
    )

    result = classify_source_candidates(candidates)

    assert result.candidates == 3
    assert result.classified == 2
    assert result.rejected == 1
    assert [outcome.candidate.file_id for outcome in result.outcomes] == [
        1,
        2,
        3,
    ]


def test_empty_candidate_batch_has_zero_counts() -> None:
    result = classify_source_candidates(())
    assert result.outcomes == ()
    assert result.candidates == 0
    assert result.classified == 0
    assert result.rejected == 0


def test_generic_engine_contains_no_installation_field_names() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "rey_lib" / "load"
        / "classify.py"
    ).read_text(encoding="utf-8")
    for field_name in ("client_alias", "file_type", "data_date"):
        assert field_name not in source

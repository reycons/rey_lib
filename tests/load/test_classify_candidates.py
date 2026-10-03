"""Focused tests for manifest-backed source-classification candidates.

Copied from the legacy file_operator tests and pointed at the Loader-owned
implementation, rey_lib.load.classify (row 589, step 2).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rey_lib.load.classify import ClassificationError
from rey_lib.load.classify import (
    SourceClassificationCandidate,
    load_source_classification_candidates,
)
from rey_lib.load import classify as source_classification

from tests.support.selecting_control import SelectingControl


def _entry(
    *,
    name: str = "file_manifest",
    procedure: str = "classification_candidates",
    source_field: str = "file_name",
    regex: str = r"(?P<value>never-matches-this-literal)",
    retry_rejects: object | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "enabled": True,
        "classification_type": "file_name_regex",
        # The routine that names this source's work, and the field of the row
        # it returns that holds the file. Which records are the work -- the
        # record type, and excluding what is already classified -- is the
        # routine's answer and is not filtered again here.
        "file_selection": {
            "procedure": procedure,
            "source_field": source_field,
        },
        "variables": [
            {
                "name": "value",
                "source": "classification",
                "required": True,
            }
        ],
        "classification": {"path_regex": regex},
    }
    if retry_rejects is not None:
        entry["retry_rejects"] = retry_rejects
    return entry


def _process(*entries: object) -> dict[str, object]:
    return {"sources": list(entries)}


def _ctx(*rows: object) -> SimpleNamespace:
    """A context that reaches the control database, with its selector primed.

    The governed manifest is a table there, so a context reaching it carries
    the Control it is reached through -- and the rows are whatever this
    source's routine returns.
    """
    control = SelectingControl()
    control.selected = [dict(row) for row in rows]
    return SimpleNamespace(shared_control=control)


def _selector_row(file_manifest_id: object, **fields: object) -> dict[str, object]:
    """One row as the selection routine returns it.

    The governed identity, and whichever field the source names. There is no
    record_type filtering here: the routine returned this row, so it is work.
    """
    return {"file_manifest_id": file_manifest_id, **fields}



def test_the_rows_the_routine_returned_become_candidates(run_log) -> None:
    """Every returned row is work; nothing is filtered again here.

    The routine decides which records are this source's work -- the record
    type, the lifecycle link, and excluding what is already classified. Tests
    for that filtering were retired with the filtering: it moved into the
    database, and asserting it here would be a second opinion about a decision
    this module does not make.
    """
    first = _selector_row(2, record_type="source_file_inventory", file_name="First.csv")
    second = _selector_row(4, record_type="source_file_inventory", file_name="Second.csv")

    candidates = load_source_classification_candidates(
        _ctx(first, second), run_log, _process(_entry()),
    )

    assert [candidate.manifest_record for candidate in candidates] == [first, second]
    assert [candidate.value for candidate in candidates] == ["First.csv", "Second.csv"]
    assert [candidate.file_id for candidate in candidates] == [2, 4]
    assert [candidate.status for candidate in candidates] == ["ready", "ready"]
    assert [candidate.source_record_type for candidate in candidates] == [
        "source_file_inventory", "source_file_inventory",
    ]


def test_each_source_asks_its_own_routine_by_name(run_log) -> None:
    """The binding configuration named, and the source's own name with it."""
    control = SelectingControl()
    control.selected = [
        _selector_row(1, record_type="source_file_inventory", file_name="A.csv"),
    ]
    asked: list[tuple[str, dict]] = []
    original = control.call_rows

    def record(binding, variables=None, required=True):
        asked.append((binding, dict(variables or {})))
        return original(binding, variables, required)

    control.call_rows = record  # type: ignore[method-assign]
    ctx = SimpleNamespace(shared_control=control)

    load_source_classification_candidates(
        ctx, run_log,
        _process(
            _entry(name="inbox", procedure="inbox_candidates"),
            _entry(name="archive", procedure="archive_candidates"),
        ),
    )

    assert asked == [
        ("inbox_candidates", {"source_name": "inbox"}),
        ("archive_candidates", {"source_name": "archive"}),
    ]


def test_missing_source_field_rejects_only_that_record(run_log) -> None:
    candidates = load_source_classification_candidates(
        _ctx(
            _selector_row(1, record_type="source_file_inventory"),
            _selector_row(2, record_type="source_file_inventory", file_name="Valid.csv"),
        ),
        run_log, _process(_entry()),
    )

    assert candidates[0].status == "rejected"
    assert candidates[0].reason_code == "missing_source_field"
    assert candidates[0].file_id == 1
    assert candidates[1].status == "ready"
    assert candidates[1].value == "Valid.csv"


@pytest.mark.parametrize("value", [None, "", "   ", 42, False])
def test_invalid_source_value_is_a_candidate_rejection(run_log, value: object) -> None:
    candidate = load_source_classification_candidates(
        _ctx(_selector_row(1, record_type="source_file_inventory", file_name=value)),
        run_log, _process(_entry()),
    )[0]

    assert candidate.status == "rejected"
    assert candidate.reason_code == "invalid_source_field"
    assert candidate.value is None


@pytest.mark.parametrize("file_manifest_id", [None, 0, -1, "1", True])
def test_an_ungoverned_identity_is_a_candidate_rejection(
    run_log, file_manifest_id: object,
) -> None:
    """A governed file id is a positive integer the database minted."""
    candidate = load_source_classification_candidates(
        _ctx(_selector_row(file_manifest_id,
                           record_type="source_file_inventory",
                           file_name="Valid.csv")),
        run_log, _process(_entry()),
    )[0]

    assert candidate.status == "rejected"
    assert candidate.reason_code == "missing_file_id"
    assert candidate.value == "Valid.csv"
    assert candidate.file_id is None


def test_candidate_is_immutable(run_log) -> None:
    candidate = load_source_classification_candidates(
        _ctx(_selector_row(1, record_type="source_file_inventory", file_name="Valid.csv")),
        run_log, _process(_entry()),
    )[0]

    assert isinstance(candidate, SourceClassificationCandidate)
    with pytest.raises(Exception):
        candidate.status = "changed"  # type: ignore[misc]


def test_all_configuration_is_validated_before_any_selection(run_log) -> None:
    """One broken source stops the run before any routine is asked."""
    control = SelectingControl()
    asked: list[str] = []
    control.call_rows = lambda *a, **k: asked.append(a[0]) or []  # type: ignore[method-assign]
    broken = _entry(name="broken")
    broken["variables"] = []

    with pytest.raises(ClassificationError, match="no manifest was read"):
        load_source_classification_candidates(
            SimpleNamespace(shared_control=control), run_log,
            _process(_entry(), broken),
        )

    assert asked == []


def test_a_selection_failure_surfaces_as_a_file_operator_error(run_log) -> None:
    """The routine's failure is reported as this source's, with its cause kept."""
    from rey_lib.errors.error_utils import DatabaseError

    control = SelectingControl()

    def fail(*_args: object, **_kwargs: object):
        raise DatabaseError("the routine is not there")

    control.call_rows = fail  # type: ignore[method-assign]

    with pytest.raises(ClassificationError) as excinfo:
        load_source_classification_candidates(
            SimpleNamespace(shared_control=control), run_log, _process(_entry()),
        )

    assert "classification_candidates" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


def test_a_context_without_a_control_is_refused(run_log) -> None:
    """The manifest is in the control database; a context reaching it holds one."""
    with pytest.raises(ClassificationError, match="control database"):
        load_source_classification_candidates(
            SimpleNamespace(), run_log, _process(_entry()),
        )


def test_regex_is_not_applied_when_candidates_are_loaded(run_log) -> None:
    candidate = load_source_classification_candidates(
        _ctx(_selector_row(1, record_type="source_file_inventory",
                           file_name="does-not-match-configured-regex.csv")),
        run_log, _process(_entry()),
    )[0]

    assert candidate.status == "ready"
    assert candidate.value == "does-not-match-configured-regex.csv"


def test_retry_rejects_requires_boolean(run_log) -> None:
    with pytest.raises(ClassificationError, match="retry_rejects must be a boolean"):
        load_source_classification_candidates(
            _ctx(), run_log, _process(_entry(retry_rejects="yes")),
        )

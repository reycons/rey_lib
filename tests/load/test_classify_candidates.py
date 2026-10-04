"""Focused tests for manifest-backed source-classification candidates.

Copied from the legacy file_operator tests and pointed at the Loader-owned
implementation, rey_lib.load.classify (row 589, step 2). Each source asks
ManifestSource.get_for_operation once, scoped to its own name, and each
candidate is prepared from the governed object's facts (backlog 619).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rey_lib.load.classify import ClassificationError
from rey_lib.load.classify import (
    SourceClassificationCandidate,
    load_source_classification_candidates,
    resolve_classification_source_configs,
)
from rey_lib.load import classify as source_classification

from tests.support.manifest_rows import manifest_row
from tests.support.selecting_control import SelectingControl


def _entry(
    *,
    name: str = "file_manifest",
    source_field: str = "file_name",
    regex: str = r"(?P<value>never-matches-this-literal)",
    retry_rejects: object | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "enabled": True,
        "classification_type": "file_name_regex",
        # The operation this source's work is retrieved for, and the field of
        # the governed file that holds what is classified. Which records are the
        # work -- the record type, and excluding what is already classified --
        # is the routine's answer and is not filtered again here.
        "file_selection": {
            "operation": "file_classification",
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
    """A context that reaches the control database, with its retrieval primed.

    The governed manifest is a table there, so a context reaching it carries
    the Control it is reached through -- and the rows are whatever the manifest
    retrieval routine returns for this source.
    """
    control = SelectingControl()
    control.selected = [dict(row) for row in rows]
    return SimpleNamespace(shared_control=control)


def _inventoried(file_manifest_id: int, file_name: str) -> dict[str, object]:
    """One inventoried governed file, as the routine returns it.

    The file's name is its working path's last segment, which is where the
    governed object reads it from. There is no record_type filtering here: the
    routine returned this file, so it is work.
    """
    return manifest_row(
        file_manifest_id, 100 + file_manifest_id, f"/data/feed/source/inbox/{file_name}",
        record_type="source_file_inventory",
    )


def _config(**overrides: object):
    return resolve_classification_source_configs(_process(_entry(**overrides)))[0]


def test_the_files_the_routine_returned_become_candidates(run_log) -> None:
    """Every returned file is work; nothing is filtered again here.

    The routine decides which records are this source's work -- the record
    type, the lifecycle link, and excluding what is already classified. Tests
    for that filtering were retired with the filtering: it moved into the
    database, and asserting it here would be a second opinion about a decision
    this module does not make.
    """
    candidates = load_source_classification_candidates(
        _ctx(_inventoried(2, "First.csv"), _inventoried(4, "Second.csv")),
        run_log, _process(_entry()),
    )

    assert [candidate.value for candidate in candidates] == ["First.csv", "Second.csv"]
    assert [candidate.file_id for candidate in candidates] == [2, 4]
    assert [candidate.status for candidate in candidates] == ["ready", "ready"]
    assert [candidate.source_record_type for candidate in candidates] == [
        "source_file_inventory", "source_file_inventory",
    ]
    # Each carries its governed object, at the mutation the routine selected.
    assert [candidate.governed.file_mutation_id for candidate in candidates] == [102, 104]
    assert [candidate.manifest_record["file_mutation_id"] for candidate in candidates] == [
        102, 104,
    ]


def test_each_source_asks_once_scoped_to_its_own_name(run_log) -> None:
    """One read per source; the database narrows to the source, never this module."""
    ctx = _ctx(_inventoried(1, "A.csv"))

    load_source_classification_candidates(
        ctx, run_log, _process(_entry(name="inbox"), _entry(name="archive")),
    )

    assert ctx.shared_control.manifest_requests == [
        {"installation_id": 1, "operation": "file_classification", "source_name": "inbox"},
        {"installation_id": 1, "operation": "file_classification", "source_name": "archive"},
    ]


def test_a_source_field_the_object_does_not_carry_is_a_rejection(run_log) -> None:
    candidates = load_source_classification_candidates(
        _ctx(_inventoried(1, "Valid.csv")),
        run_log, _process(_entry(source_field="file.path")),
    )

    assert candidates[0].status == "rejected"
    assert candidates[0].reason_code == "missing_source_field"
    assert candidates[0].file_id == 1


# The guards below are _candidate's own. A governed object cannot carry these
# values -- the database mints a positive integer id, and the name comes from
# the working path -- so they are exercised on _candidate directly rather than
# through a selection that can no longer produce them.

def test_missing_source_field_rejects_only_that_record() -> None:
    config = _config()
    missing = source_classification._candidate(
        config, {"file_manifest_id": 1, "record_type": "source_file_inventory"})
    valid = source_classification._candidate(
        config, {"file_manifest_id": 2, "record_type": "source_file_inventory",
                 "file_name": "Valid.csv"})

    assert missing.status == "rejected"
    assert missing.reason_code == "missing_source_field"
    assert missing.file_id == 1
    assert valid.status == "ready"
    assert valid.value == "Valid.csv"


@pytest.mark.parametrize("value", [None, "", "   ", 42, False])
def test_invalid_source_value_is_a_candidate_rejection(value: object) -> None:
    candidate = source_classification._candidate(
        _config(), {"file_manifest_id": 1, "record_type": "source_file_inventory",
                    "file_name": value})

    assert candidate.status == "rejected"
    assert candidate.reason_code == "invalid_source_field"
    assert candidate.value is None


@pytest.mark.parametrize("file_manifest_id", [None, 0, -1, "1", True])
def test_an_ungoverned_identity_is_a_candidate_rejection(file_manifest_id: object) -> None:
    """A governed file id is a positive integer the database minted."""
    candidate = source_classification._candidate(
        _config(), {"file_manifest_id": file_manifest_id,
                    "record_type": "source_file_inventory", "file_name": "Valid.csv"})

    assert candidate.status == "rejected"
    assert candidate.reason_code == "missing_file_id"
    assert candidate.value == "Valid.csv"
    assert candidate.file_id is None


def test_an_empty_working_path_is_a_candidate_rejection(run_log) -> None:
    """Through the step: a governed file whose working path is blank has no name."""
    row = _inventoried(1, "x.csv")
    row["path"] = ""

    (candidate,) = load_source_classification_candidates(
        _ctx(row), run_log, _process(_entry()),
    )

    assert candidate.status == "rejected"
    assert candidate.reason_code == "invalid_source_field"


def test_candidate_is_immutable(run_log) -> None:
    candidate = load_source_classification_candidates(
        _ctx(_inventoried(1, "Valid.csv")),
        run_log, _process(_entry()),
    )[0]

    assert isinstance(candidate, SourceClassificationCandidate)
    with pytest.raises(Exception):
        candidate.status = "changed"  # type: ignore[misc]


def test_all_configuration_is_validated_before_any_selection(run_log) -> None:
    """One broken source stops the run before any manifest is read."""
    control = SelectingControl()
    broken = _entry(name="broken")
    broken["variables"] = []

    with pytest.raises(ClassificationError, match="no manifest was read"):
        load_source_classification_candidates(
            SimpleNamespace(shared_control=control), run_log,
            _process(_entry(), broken),
        )

    assert control.manifest_requests == []


def test_a_selection_failure_surfaces_as_a_file_operator_error(run_log) -> None:
    """The routine's failure is reported as this source's, with its cause kept."""
    from rey_lib.errors.error_utils import DatabaseError

    control = SelectingControl()

    def fail(**_filters: object):
        raise DatabaseError("the routine is not there")

    control.get_file_manifests = fail  # type: ignore[method-assign]

    with pytest.raises(ClassificationError) as excinfo:
        load_source_classification_candidates(
            SimpleNamespace(shared_control=control), run_log, _process(_entry()),
        )

    assert "file_classification" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


def test_a_retired_procedure_key_is_refused_by_name(run_log) -> None:
    entry = _entry()
    entry["file_selection"] = {"procedure": "classification_candidates",
                               "source_field": "file_name"}

    with pytest.raises(ClassificationError, match="procedure is retired"):
        load_source_classification_candidates(_ctx(), run_log, _process(entry))


def test_a_context_without_a_control_is_refused(run_log) -> None:
    """The manifest is in the control database; a context reaching it holds one."""
    with pytest.raises(ClassificationError, match="control database"):
        load_source_classification_candidates(
            SimpleNamespace(), run_log, _process(_entry()),
        )


def test_regex_is_not_applied_when_candidates_are_loaded(run_log) -> None:
    candidate = load_source_classification_candidates(
        _ctx(_inventoried(1, "does-not-match-configured-regex.csv")),
        run_log, _process(_entry()),
    )[0]

    assert candidate.status == "ready"
    assert candidate.value == "does-not-match-configured-regex.csv"


def test_retry_rejects_requires_boolean(run_log) -> None:
    with pytest.raises(ClassificationError, match="retry_rejects must be a boolean"):
        load_source_classification_candidates(
            _ctx(), run_log, _process(_entry(retry_rejects="yes")),
        )

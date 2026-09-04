"""Focused contracts for semantic governed-file routing."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import make_run_log
from unittest.mock import patch

import pytest

from rey_lib.files import (
    CollisionPolicy,
    FileRoutingContext,
    FileRoutingError,
    FileRoutingEvidenceError,
    FileRoutingRole,
    GovernedFileReference,
    SourceFileMutationEvidenceError,
    SourceFileMutationEvidenceFailurePhase,
)
from rey_lib.files import file_routing


def _context(
    root: Path,
    *,
    routes: dict[FileRoutingRole, str | Path | None] | None = None,
    dry_run: bool = False,
    destination_name: str | None = None,
) -> FileRoutingContext:
    return FileRoutingContext(
        state_ctx=SimpleNamespace(),
        run_log=make_run_log(root, path=str(root / "run.jsonl")),
        application_name="test_app",
        operation="test_operation",
        routes=(
            routes
            if routes is not None
            else {
                FileRoutingRole.PROCESSING: root / "processing",
                FileRoutingRole.KICKOUTS: root / "kickouts",
                FileRoutingRole.FAILED: root / "failed",
                FileRoutingRole.ARCHIVE: root / "archive",
            }
        ),
        governed_roots=(root,),
        dry_run=dry_run,
        destination_name=destination_name,
        collision_policy=CollisionPolicy.OVERWRITE,
        file_operation_metadata={"surface": "file_operation"},
        mutation_run_log_fields={"surface": "mutation"},
        pipeline_name="pipeline-a",
    )


def _file(root: Path) -> tuple[GovernedFileReference, dict]:
    source = root / "inbox" / "source.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    classification = {
        "type": "configured",
        "source_field": "file.path",
        "values": {"feed": "alpha", "nested": {"kept": [1, 2]}},
    }
    return GovernedFileReference(7, source, classification), classification


@pytest.mark.parametrize(
    ("wrapper", "role"),
    [
        (file_routing.move_to_processing, FileRoutingRole.PROCESSING),
        (file_routing.move_to_kickouts, FileRoutingRole.KICKOUTS),
        (file_routing.move_to_failed, FileRoutingRole.FAILED),
        (file_routing.move_to_archive, FileRoutingRole.ARCHIVE),
    ],
)
def test_public_wrappers_only_dispatch_their_fixed_role(
    tmp_path: Path,
    wrapper,
    role: FileRoutingRole,
) -> None:
    governed, _ = _file(tmp_path)
    expected = object()
    context = _context(tmp_path)
    with patch.object(file_routing, "_move_to_role", return_value=expected) as engine:
        assert wrapper(context, governed) is expected
    engine.assert_called_once_with(context, governed, destination_role=role)


def test_private_engine_is_not_exported() -> None:
    assert "_move_to_role" not in file_routing.__all__
    import rey_lib.files as files

    assert not hasattr(files, "_move_to_role")


@pytest.mark.parametrize(
    ("wrapper", "role", "message"),
    [
        (file_routing.move_to_processing, FileRoutingRole.PROCESSING, "processing"),
        (file_routing.move_to_kickouts, FileRoutingRole.KICKOUTS, "kickouts"),
        (file_routing.move_to_failed, FileRoutingRole.FAILED, "failed"),
        (file_routing.move_to_archive, FileRoutingRole.ARCHIVE, "archive"),
    ],
)
def test_each_role_uses_shared_move_and_evidence_contract(
    tmp_path: Path,
    wrapper,
    role: FileRoutingRole,
    message: str,
) -> None:
    governed, supplied_classification = _file(tmp_path)
    original_snapshot = deepcopy(supplied_classification)
    ctx = _context(tmp_path)
    destination = tmp_path / role.value / governed.current_path.name
    with patch.object(file_routing, "move_file", return_value=destination) as move, patch.object(
        file_routing, "log_source_file_mutation", return_value=41
    ) as evidence:
        result = wrapper(ctx, governed)

    move.assert_called_once_with(
        governed.current_path,
        tmp_path / role.value,
        None,
        state_ctx=ctx.state_ctx,
        run_log=ctx.run_log,
        app="test_app",
        pipeline="pipeline-a",
        reason=role.value,
        original_source=governed.current_path,
        metadata={"surface": "file_operation"},
    )
    assert evidence.call_args.kwargs["file_id"] == 7
    logged_classification = evidence.call_args.kwargs["classification"]
    assert supplied_classification == logged_classification
    assert supplied_classification == original_snapshot
    # The evidence reason names the destination, not just the role: a mutation
    # reading "moved" does not say where it went, and the hierarchy's
    # presentation table is keyed by these.
    assert evidence.call_args.kwargs["reason"] == f"moved_to_{role.value}"
    assert evidence.call_args.kwargs["message"] == (
        f"Governed file moved to {message}."
    )
    assert evidence.call_args.kwargs["run_log_fields"] == {"surface": "mutation"}
    assert result.status == "moved"
    assert result.file_id == 7
    assert result.file_manifest_record_id == 41
    assert result.complete_evidence_acknowledged is True
    assert result.mutation_run_log_committed is True
    assert result.rollback_information is not None
    assert result.rollback_information.canonical_rollback_acknowledged is True


def test_route_template_and_destination_name_are_resolved_by_shared_engine(
    tmp_path: Path,
) -> None:
    governed, _ = _file(tmp_path)
    ctx = _context(
        tmp_path,
        routes={
            FileRoutingRole.PROCESSING: (
                tmp_path / "<classification.values.feed>" / "processing"
            )
        },
        destination_name="renamed.csv",
    )
    expected = tmp_path / "alpha" / "processing" / "renamed.csv"
    with patch.object(file_routing, "move_file", return_value=expected) as move, patch.object(
        file_routing, "log_source_file_mutation", return_value=5
    ):
        result = file_routing.move_to_processing(ctx, governed)

    assert move.call_args.args[:3] == (
        governed.current_path,
        tmp_path / "alpha" / "processing",
        "renamed.csv",
    )
    assert result.resulting_path == expected


def test_apply_uses_move_file_directory_creation_and_preserves_name(
    tmp_path: Path,
) -> None:
    governed, _ = _file(tmp_path)
    destination_dir = tmp_path / "not-created" / "processing"
    ctx = _context(
        tmp_path,
        routes={FileRoutingRole.PROCESSING: destination_dir},
    )
    with patch.object(file_routing, "log_source_file_mutation", return_value=5):
        result = file_routing.move_to_processing(ctx, governed)

    assert result.resulting_path == destination_dir / "source.csv"
    assert result.resulting_path.read_text(encoding="utf-8") == "a,b\n1,2\n"
    assert not governed.current_path.exists()


@pytest.mark.parametrize(
    ("wrapper", "role"),
    [
        (file_routing.move_to_processing, FileRoutingRole.PROCESSING),
        (file_routing.move_to_kickouts, FileRoutingRole.KICKOUTS),
        (file_routing.move_to_failed, FileRoutingRole.FAILED),
        (file_routing.move_to_archive, FileRoutingRole.ARCHIVE),
    ],
)
@pytest.mark.parametrize("route_present", [False, True])
def test_missing_route_fails_before_filesystem_access(
    tmp_path: Path,
    wrapper,
    role: FileRoutingRole,
    route_present: bool,
) -> None:
    governed, _ = _file(tmp_path)
    routes = {role: "   "} if route_present else {}
    ctx = _context(tmp_path, routes=routes)
    with patch.object(file_routing, "move_file") as move, patch.object(
        file_routing, "log_source_file_mutation"
    ) as evidence:
        with pytest.raises(
            FileRoutingError,
            match=rf"configured {role.value} route",
        ) as raised:
            wrapper(ctx, governed)
    assert raised.value.result.destination_role == role.value
    move.assert_not_called()
    evidence.assert_not_called()


def test_missing_file_id_fails_before_path_normalization() -> None:
    # The identity is a database-minted integer, so the empty string is not a
    # missing id in the old sense -- it is the retired string model.
    with pytest.raises(ValueError, match="file id"):
        GovernedFileReference("", object())  # type: ignore[arg-type]


def test_source_must_exist_before_same_path_handling(tmp_path: Path) -> None:
    missing = GovernedFileReference(1, tmp_path / "inbox" / "gone.csv")
    existing_destination = tmp_path / "processing" / "gone.csv"
    existing_destination.parent.mkdir()
    existing_destination.write_text("already there", encoding="utf-8")
    ctx = _context(
        tmp_path,
        routes={FileRoutingRole.PROCESSING: tmp_path / "processing"},
    )
    with pytest.raises(FileRoutingError, match="Source file not found"):
        file_routing.move_to_processing(ctx, missing)


def test_same_path_returns_unchanged_without_primitive_calls(tmp_path: Path) -> None:
    governed, _ = _file(tmp_path)
    ctx = _context(
        tmp_path,
        routes={FileRoutingRole.PROCESSING: governed.current_path.parent},
    )
    with patch.object(file_routing, "move_file") as move, patch.object(
        file_routing, "log_source_file_mutation"
    ) as evidence:
        result = file_routing.move_to_processing(ctx, governed)
    assert result.status == "unchanged"
    assert result.filesystem_applied is False
    move.assert_not_called()
    evidence.assert_not_called()


def test_dry_run_creates_nothing_and_records_no_evidence(tmp_path: Path) -> None:
    governed, _ = _file(tmp_path)
    destination = tmp_path / "new" / "processing"
    ctx = _context(
        tmp_path,
        routes={FileRoutingRole.PROCESSING: destination},
        dry_run=True,
    )
    with patch.object(file_routing, "move_file") as move, patch.object(
        file_routing, "log_source_file_mutation"
    ) as evidence:
        result = file_routing.move_to_processing(ctx, governed)
    assert result.status == "planned"
    assert not destination.exists()
    move.assert_not_called()
    evidence.assert_not_called()


def test_overwrite_collision_is_reported_and_delegated(tmp_path: Path) -> None:
    governed, _ = _file(tmp_path)
    destination = tmp_path / "processing" / governed.current_path.name
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    ctx = _context(tmp_path)
    with patch.object(file_routing, "move_file", return_value=destination), patch.object(
        file_routing, "log_source_file_mutation", return_value=9
    ):
        result = file_routing.move_to_processing(ctx, governed)
    assert result.destination_existed is True
    assert result.collision_policy is CollisionPolicy.OVERWRITE


def test_non_file_collision_fails_before_mutation(tmp_path: Path) -> None:
    governed, _ = _file(tmp_path)
    destination = tmp_path / "processing" / governed.current_path.name
    destination.mkdir(parents=True)
    with patch.object(file_routing, "move_file") as move:
        with pytest.raises(FileRoutingError, match="not a regular file"):
            file_routing.move_to_processing(_context(tmp_path), governed)
    move.assert_not_called()


@pytest.mark.parametrize("failure", [FileNotFoundError("gone"), OSError("blocked")])
def test_physical_failures_are_normalized_and_recorded_as_failed(
    tmp_path: Path,
    failure: OSError,
) -> None:
    """A move that did not happen is still a mutation, recorded as failed.

    This used to assert no evidence at all. A failed move is evidence -- the
    hierarchy's presentation table carries a failure label for every reason for
    exactly this -- so what matters is that the record says the move failed,
    not that nothing was written.
    """
    governed, _ = _file(tmp_path)
    with patch.object(file_routing, "move_file", side_effect=failure), patch.object(
        file_routing, "log_source_file_mutation"
    ) as evidence:
        with pytest.raises(FileRoutingError) as raised:
            file_routing.move_to_processing(_context(tmp_path), governed)

    assert raised.value.result.filesystem_applied is False
    recorded = evidence.call_args.kwargs
    assert recorded["status"] == "failed"
    assert recorded["reason"] == "moved_to_processing"


def test_non_oserror_from_move_is_not_hidden(tmp_path: Path) -> None:
    governed, _ = _file(tmp_path)
    with patch.object(
        file_routing, "move_file", side_effect=RuntimeError("unexpected")
    ):
        with pytest.raises(RuntimeError, match="unexpected"):
            file_routing.move_to_processing(_context(tmp_path), governed)


@pytest.mark.parametrize(
    ("phase", "record_id", "committed"),
    [
        (SourceFileMutationEvidenceFailurePhase.RUN_LOG_NOT_COMMITTED, None, False),
        (
            SourceFileMutationEvidenceFailurePhase.RUN_LOG_COMMITTED_COMPLETE_EVIDENCE_NOT_ACKNOWLEDGED,
            37,
            True,
        ),
    ],
)
def test_evidence_failure_preserves_only_provable_state(
    tmp_path: Path,
    phase: SourceFileMutationEvidenceFailurePhase,
    record_id: int | None,
    committed: bool,
) -> None:
    governed, _ = _file(tmp_path)
    destination = tmp_path / "processing" / governed.current_path.name
    failure = SourceFileMutationEvidenceError(
        "evidence failed",
        phase=phase,
        run_log_id=record_id,
    )
    with patch.object(file_routing, "move_file", return_value=destination), patch.object(
        file_routing, "log_source_file_mutation", side_effect=failure
    ):
        with pytest.raises(FileRoutingEvidenceError) as raised:
            file_routing.move_to_processing(_context(tmp_path), governed)

    result = raised.value.result
    assert result.filesystem_applied is True
    assert result.complete_evidence_acknowledged is False
    assert result.mutation_run_log_committed is committed
    assert result.mutation_run_log_id == record_id
    assert result.evidence_phase is phase
    assert result.rollback_information is not None
    assert result.rollback_information.file_manifest_record_id is None
    assert (
        result.rollback_information.canonical_rollback_acknowledged is False
    )


def test_paths_outside_governed_roots_fail_before_mutation(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-routing-test" / "source.csv"
    governed = GovernedFileReference(1, outside)
    with patch.object(file_routing, "move_file") as move:
        with pytest.raises(FileRoutingError, match="outside"):
            file_routing.move_to_processing(_context(tmp_path), governed)
    move.assert_not_called()

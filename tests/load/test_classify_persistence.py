"""Classification through the classify Transform (row 589, step 2).

Adapted from the legacy file_operator persistence and orchestration tests: each
evidence-first guarantee the legacy persistence made is asserted against the
Loader-owned ClassifyTransform that now owns it.

    selected row
      |- record_type (a DataFile fact) ---------------------> run-log evidence
      `- file_mutation_id -> ManifestSource -> DataFile -> ClassifyTransform
            no match -> evidence, the append (no mutation), ()
            match    -> evidence -> plan -> record -> move -> verify
                        -> data_profile_key -> (DataFile in processing,)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from rey_lib.files import CollisionPolicy, FileRoutingError, FileRoutingResult
from rey_lib.files import file_routing
from rey_lib.files.data_file import data_file_for
from rey_lib.load import classify as source_classification
from rey_lib.load.classify import (
    ClassificationError,
    ClassificationRejected,
    ClassifyTransform,
    SourceClassificationCandidate,
    resolve_classification_source_configs,
    run_source_file_classification,
)
from rey_lib.load.transform import Transform
from rey_lib.logs import FileManifestError, bind_step, clear_step
from rey_lib.workflow import RunContext

from rey_lib.load.manifest_source import ManifestSource
from tests.support.manifest_rows import manifest_row
from tests.support.selecting_control import SelectingControl

_RECORD_TYPE = "source_file_inventory"


def _entry(classification_type: str = "configured_mechanism", **overrides) -> dict:
    """A classification source declaration matching ``<captured_name>.csv``."""
    entry = {
        "name": "generic",
        "enabled": True,
        "classification_type": classification_type,
        "file_selection": {
            "operation": "file_classification",
            "source_field": "file_name",
        },
        "variables": [
            {"name": "captured_name", "source": "classification", "required": True}
        ],
        "classification": {"path_regex": r"(?P<captured_name>.+)[.]csv"},
    }
    entry.update(overrides)
    return entry


def _routed_entry(tmp_path: Path, **overrides) -> dict:
    """A declaration classifying by path and routing to processing."""
    entry = {
        "name": "generic",
        "enabled": True,
        "classification_type": "file_name_regex",
        "file_selection": {
            "operation": "file_classification",
            "source_field": "file.path",
        },
        "processing": str(tmp_path / "<classification.values.feed>" / "processing"),
        "variables": [{"name": "feed", "source": "classification", "required": True}],
        "classification": {"path_regex": r".*/(?P<feed>[^/]+)/inbox/[^/]+"},
    }
    entry.update(overrides)
    return entry


def _ctx(tmp_path: Path) -> tuple[SimpleNamespace, SelectingControl]:
    """A context that persists through the control database it carries."""
    control = SelectingControl()
    ctx = SimpleNamespace(
        app_name="rey_loader",
        paths=SimpleNamespace(resolve=lambda name: tmp_path),
        shared_control=control,
    )
    return ctx, control


def _file(path: Path, file_id: int = 7, *, create: bool = False):
    """The governed file at its selected state, as ManifestSource builds it."""
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a,b\n1,2\n", encoding="utf-8")
    return data_file_for(path, file_manifest_id=file_id, file_mutation_id=file_id,
                         record_type=_RECORD_TYPE)


def _inbox(tmp_path: Path, name: str = "Example7.csv"):
    return _file(tmp_path / "alpha" / "inbox" / name, create=True)


def _classify(ctx, entry, data_file):
    return Transform(values={"source": entry}, selected="classify").resolve(ctx).apply(data_file)


# -- the outcome -------------------------------------------------------------

def test_classify_is_resolved_through_the_one_resolver(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)

    resolved = Transform(values={"source": _entry()}, selected="classify").resolve(ctx)

    assert isinstance(resolved, ClassifyTransform)
    assert "classify" not in Transform.kinds()


def test_a_classified_file_is_recorded_and_returned(run_log, tmp_path: Path) -> None:
    ctx, control = _ctx(tmp_path)

    with patch.object(source_classification, "log_run_record", return_value=123) as evidence:
        (classified,) = _classify(ctx, _entry("file_name_regex"),
                                  _file(Path("/data/alpha/inbox/Example7.csv")))

    rows = control.mutations
    assert [row["record_type"] for row in rows] == ["source_file_classification"]
    assert rows[0]["classification"]["type"] == "file_name_regex"
    assert rows[0]["classification"]["values"] == {"captured_name": "Example7"}
    assert rows[0]["file_manifest_id"] == 7
    assert rows[0]["status"] == "success"
    assert rows[0]["result"] == "classified"
    assert evidence.call_count == 1
    assert classified.file_manifest_id == 7
    assert classified.classification["values"] == {"captured_name": "Example7"}


def test_a_rejected_file_writes_evidence_only_and_returns_nothing(
    run_log, tmp_path: Path,
) -> None:
    """A source that retries its rejects records the rejection and keeps the file."""
    ctx, control = _ctx(tmp_path)

    with patch.object(source_classification, "log_run_record", return_value=124) as evidence:
        result = _classify(ctx, _entry(retry_rejects=True),
                           _file(Path("/data/alpha/inbox/Example7.json")))

    # A rejection produces no result and so writes no mutation -- its reason
    # lives on the evidence record, which is its only home.
    assert result == ()
    assert control.mutations == []
    assert evidence.call_args.kwargs["status"] == "rejected"
    assert evidence.call_args.kwargs["reason_code"] == "path_regex_mismatch"


def test_a_terminal_rejection_is_recorded_then_kicked_out_to_its_inbox(
    run_log, tmp_path: Path,
) -> None:
    """retry_rejects false: the kind raises its file failure after recording it,
    and the common execution path moves the original to <inbox>/kickouts."""
    ctx, control = _ctx(tmp_path)
    source = tmp_path / "alpha" / "inbox" / "Example7.json"
    source.parent.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")
    rejected = data_file_for(
        source, file_manifest_id=7, file_mutation_id=7, inbox=str(source.parent),
        original_path=str(source), original_mutation_id=7,
    )

    order: list[str] = []

    def _evidence(*_a, **_k):
        order.append("evidence")
        return 124

    def _moved(*_a, **_k):
        order.append("move")
        return 456

    bind_step(step_id="classify_source_files")
    try:
        with patch.object(source_classification, "log_run_record",
                          side_effect=_evidence) as evidence, \
             patch.object(file_routing, "log_source_file_mutation",
                          side_effect=_moved) as mutation:
            with pytest.raises(ClassificationRejected, match="path_regex_mismatch"):
                _classify(ctx, _entry(), rejected)
    finally:
        clear_step()

    # Rule 75: kicked out first, then the rejection is recorded.
    assert order == ["move", "evidence"]
    assert evidence.call_args.kwargs["reason_code"] == "path_regex_mismatch"
    assert control.mutations == []
    assert (source.parent / "kickouts" / source.name).exists()
    assert mutation.call_args.kwargs["reason"] == "moved_to_kickouts"


# -- record, then move -------------------------------------------------------

def test_the_record_is_written_before_the_file_moves(run_log, tmp_path: Path) -> None:
    data_file = _inbox(tmp_path)
    source = data_file.path
    ctx, control = _ctx(tmp_path)

    # Where the file was AT THE MOMENT the record was appended. The end state
    # cannot distinguish the two orderings, so it is observed while it happens.
    inbox_when_recorded: list[bool] = []
    real_append = source_classification.log_file_manifest_record

    def _watch_append(ctx_: object, record: dict) -> object:
        inbox_when_recorded.append(source.exists())
        return real_append(ctx_, record)

    with patch.object(source_classification, "log_run_record", return_value=123), \
         patch.object(file_routing, "log_source_file_mutation", return_value=456) as mutation, \
         patch.object(source_classification, "log_file_manifest_record",
                      side_effect=_watch_append):
        (moved,) = _classify(ctx, _routed_entry(tmp_path), data_file)

    # THE POINT OF THIS TEST: the file had not moved when its record was written.
    assert inbox_when_recorded == [True]

    destination = tmp_path / "alpha" / "processing" / source.name
    assert destination.exists()
    assert not source.exists()
    # The classification event records no path; the move that follows does.
    row = control.mutations[0]
    assert row["record_type"] == "source_file_classification"
    assert row["path"] is None
    assert mutation.call_args.kwargs["source_path"] == source
    assert mutation.call_args.kwargs["destination_path"] == destination
    assert mutation.call_args.kwargs["reason"] == "moved_to_processing"
    assert mutation.call_args.kwargs["operation"] == "classification"
    assert mutation.call_args.kwargs["file_id"] == 7
    assert mutation.call_args.kwargs["classification"] == {
        "type": "file_name_regex",
        "source_field": "file.path",
        "values": {"feed": "alpha"},
    }
    assert moved.path == destination
    assert moved.file_mutation_id == 456
    assert moved.classification["values"] == {"feed": "alpha"}


def test_the_move_evidence_names_the_selected_mutation(run_log, tmp_path: Path) -> None:
    """Legacy parity (backlog row 610): the M3 move's run-log evidence carries
    source_record_id = the SELECTED row's mutation, not the classification
    record written just before it; the moved file still takes routing's id."""
    source = tmp_path / "alpha" / "inbox" / "Example7.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    selected = data_file_for(source, file_manifest_id=7, file_mutation_id=31)
    ctx, control = _ctx(tmp_path)

    with patch.object(source_classification, "log_run_record", return_value=123), \
         patch.object(file_routing, "log_source_file_mutation", return_value=456) as mutation:
        (moved,) = _classify(ctx, _routed_entry(tmp_path), selected)

    assert control.mutations[0]["record_type"] == "source_file_classification"
    assert mutation.call_args.kwargs["run_log_fields"] == {"source_record_id": 31}
    assert moved.file_mutation_id == 456


def test_no_processing_route_classifies_without_moving(run_log, tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    data_file = _file(Path("/data/alpha/inbox/Example7.csv"))

    with patch.object(file_routing, "move_to_processing") as route, \
         patch.object(source_classification, "log_run_record", return_value=123), \
         patch.object(source_classification, "log_file_manifest_record", return_value=48):
        (classified,) = _classify(ctx, _entry(), data_file)

    route.assert_not_called()
    assert classified.path == data_file.path
    assert classified.file_mutation_id == 48


def test_a_rejected_file_never_routes_when_processing_is_configured(
    run_log, tmp_path: Path,
) -> None:
    ctx, _ = _ctx(tmp_path)
    outside = _file(tmp_path / "alpha" / "elsewhere" / "notes.csv", create=True)

    with patch.object(file_routing, "move_to_processing") as route, \
         patch.object(source_classification, "log_run_record", return_value=123):
        result = _classify(ctx, _routed_entry(tmp_path, retry_rejects=True), outside)

    route.assert_not_called()
    assert result == ()


def test_an_append_failure_leaves_the_file_in_the_inbox(run_log, tmp_path: Path) -> None:
    data_file = _inbox(tmp_path)
    ctx, _ = _ctx(tmp_path)

    with patch.object(source_classification, "log_run_record", return_value=123), \
         patch.object(file_routing, "log_source_file_mutation", return_value=456), \
         patch.object(source_classification, "log_file_manifest_record",
                      side_effect=FileManifestError("classification append blocked")):
        with pytest.raises(ClassificationError, match="classification append blocked"):
            _classify(ctx, _routed_entry(tmp_path), data_file)

    # The file never moved, because nothing recorded that it had (2026-09-18).
    assert data_file.path.exists()
    assert not (tmp_path / "alpha" / "processing" / data_file.path.name).exists()


def test_a_file_that_cannot_be_routed_writes_no_record_and_moves_nothing(
    run_log, tmp_path: Path,
) -> None:
    data_file = _inbox(tmp_path)
    ctx, control = _ctx(tmp_path)

    with patch.object(source_classification, "log_run_record", return_value=123), \
         patch.object(file_routing, "move_to_processing",
                      side_effect=FileRoutingError("outside the governed roots", None)):
        with pytest.raises(ClassificationError, match="could not be routed to processing"):
            _classify(ctx, _routed_entry(tmp_path), data_file)

    assert control.mutations == []
    assert data_file.path.exists()


def _routing_result(data_file, path: Path, dry_run: bool) -> FileRoutingResult:
    return FileRoutingResult(
        file_id=7, source_role=None, destination_role="processing",
        original_path=data_file.path, resulting_path=path,
        canonical_action="move", status="planned" if dry_run else "moved",
        dry_run=dry_run, filesystem_applied=not dry_run,
        complete_evidence_acknowledged=not dry_run,
        mutation_run_log_committed=not dry_run,
        mutation_run_log_id=None if dry_run else 456, evidence_phase=None,
        file_manifest_record_id=None if dry_run else 456,
        collision_policy=CollisionPolicy.OVERWRITE, destination_existed=False,
        rollback_information=None, failure_reason=None,
    )


def test_a_move_landing_somewhere_other_than_planned_fails_loudly(
    run_log, tmp_path: Path,
) -> None:
    data_file = _inbox(tmp_path)
    ctx, _ = _ctx(tmp_path)
    planned = tmp_path / "alpha" / "processing" / data_file.path.name
    elsewhere = tmp_path / "alpha" / "processing" / "Somewhere_Else.csv"

    def _plan_then_divert(routing_ctx, _file):
        return _routing_result(
            data_file, planned if routing_ctx.dry_run else elsewhere, routing_ctx.dry_run)

    with patch.object(source_classification, "log_run_record", return_value=123), \
         patch.object(file_routing, "move_to_processing", side_effect=_plan_then_divert):
        with pytest.raises(ClassificationError, match="no longer agree"):
            _classify(ctx, _routed_entry(tmp_path), data_file)


def test_a_failed_move_is_chained_through_the_classification_error(
    run_log, tmp_path: Path,
) -> None:
    data_file = _inbox(tmp_path)
    ctx, _ = _ctx(tmp_path)
    planned = tmp_path / "alpha" / "processing" / data_file.path.name
    failure = FileRoutingError("blocked", None)

    def _plan_then_fail(routing_ctx, _file):
        if not routing_ctx.dry_run:
            raise failure
        return _routing_result(data_file, planned, True)

    with patch.object(source_classification, "log_run_record", return_value=123), \
         patch.object(file_routing, "move_to_processing", side_effect=_plan_then_fail):
        with pytest.raises(ClassificationError, match="did not move") as raised:
            _classify(ctx, _routed_entry(tmp_path), data_file)

    assert raised.value.__cause__ is failure


# -- evidence ----------------------------------------------------------------

def test_the_evidence_is_the_legacy_evidence_including_the_record_type(
    run_log, tmp_path: Path,
) -> None:
    """Behavioural parity: source_record_type is the selected state's record
    type, now the DataFile's own fact (backlog 630)."""
    ctx, _ = _ctx(tmp_path)

    with patch.object(source_classification, "log_run_record", return_value=123) as evidence, \
         patch.object(source_classification, "log_file_manifest_record", return_value=48):
        _classify(ctx, _entry(), _file(Path("/data/alpha/inbox/Example7.csv")))

    assert evidence.call_args == call(
        None,
        "SOURCE_FILE_CLASSIFICATION",
        message="Source file classification classified for inventory record 7.",
        classification_type="configured_mechanism",
        file_id=7,
        file_extension="csv",
        source_record_type="source_file_inventory",
        source_record_id=7,
        source_field="file_name",
        status="classified",
        values={"captured_name": "Example7"},
    )


def test_evidence_that_did_not_commit_prevents_the_record(run_log, tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)

    with patch.object(source_classification, "log_run_record", return_value=None), \
         patch.object(source_classification, "log_file_manifest_record") as append:
        with pytest.raises(ClassificationError, match="evidence did not commit"):
            _classify(ctx, _entry(), _file(Path("/data/alpha/inbox/Example1.csv")))

    append.assert_not_called()


def test_an_append_failure_uses_the_classification_boundary(run_log, tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)

    with patch.object(source_classification, "log_run_record", return_value=123), \
         patch.object(source_classification, "log_file_manifest_record",
                      side_effect=FileManifestError("append blocked")):
        with pytest.raises(ClassificationError, match="append blocked"):
            _classify(ctx, _entry(), _file(Path("/data/alpha/inbox/Example1.csv")))


# -- the grouping key written alongside the classification -------------------

def _grouping_entry() -> dict:
    """A source declaring feed and record_type as its grouping fields."""
    return _entry(
        variables=[
            {"name": "feed", "source": "classification", "required": True},
            {"name": "record_type", "source": "classification", "required": True},
            {"name": "data_date", "source": "classification", "required": True},
        ],
        classification={
            "path_regex":
                r"(?P<feed>[^_]+)_(?P<record_type>[^_]+)_(?P<data_date>.+)[.]csv",
            "profile": {"key_fields": ["feed", "record_type"]},
        },
    )


def _governed(control, name: str) -> int:
    """One inventoried file, identified as the control database identifies it."""
    return control.inventory_file(
        path=f"/data/alpha/inbox/{name}", file_name=name,
        base_name=name.rsplit(".", 1)[0], file_extension="csv",
        checksum_sha256=name, size_bytes=1,
    )


def _classify_all(ctx, entry, *named: tuple[int, str]) -> None:
    """Classify each governed file through the real path, evidence committing."""
    with patch.object(source_classification, "log_run_record",
                      side_effect=list(range(900, 900 + len(named)))):
        for file_id, name in named:
            _classify(ctx, entry, _file(Path(f"/data/alpha/inbox/{name}"), file_id))


def _key_of(control, file_id: int) -> object:
    row = next(r for r in control.files if r["file_manifest_id"] == file_id)
    return row.get("data_profile_key")


def test_the_classification_mutation_carries_no_key(run_log, tmp_path: Path) -> None:
    ctx, control = _ctx(tmp_path)
    name = "amalgamated_Hold_May26.csv"
    file_id = _governed(control, name)

    _classify_all(ctx, _grouping_entry(), (file_id, name))

    appended = [m for m in control.mutations
                if m.get("record_type") == "source_file_classification"]
    assert len(appended) == 1
    assert "data_profile_key" not in appended[0]


def test_classification_writes_the_derived_key_to_the_manifest(
    run_log, tmp_path: Path,
) -> None:
    ctx, control = _ctx(tmp_path)
    name = "amalgamated_Hold_May26.csv"
    file_id = _governed(control, name)

    _classify_all(ctx, _grouping_entry(), (file_id, name))

    assert _key_of(control, file_id) == "amalgamated|Hold"


def test_two_files_of_one_group_land_on_one_key(run_log, tmp_path: Path) -> None:
    ctx, control = _ctx(tmp_path)
    may, jun = "amalgamated_Hold_May26.csv", "amalgamated_Hold_Jun26.csv"
    first, second = _governed(control, may), _governed(control, jun)

    _classify_all(ctx, _grouping_entry(), (first, may), (second, jun))

    assert _key_of(control, first) == _key_of(control, second)


def test_a_source_declaring_no_grouping_writes_no_key(run_log, tmp_path: Path) -> None:
    ctx, control = _ctx(tmp_path)
    file_id = _governed(control, "Example.csv")

    _classify_all(ctx, _entry(), (file_id, "Example.csv"))

    assert _key_of(control, file_id) is None


def test_a_rejected_file_gets_no_key(run_log, tmp_path: Path) -> None:
    ctx, control = _ctx(tmp_path)
    file_id = _governed(control, "unmatched.csv")

    with pytest.raises(ClassificationRejected):
        _classify_all(ctx, _grouping_entry(), (file_id, "unmatched.csv"))

    assert _key_of(control, file_id) is None


# -- the step ----------------------------------------------------------------

def _candidate(entry: dict, mutation: int | None, path: str) -> SourceClassificationCandidate:
    """A prepared candidate carrying the governed object it was prepared from,
    as load_source_classification_candidates builds it (backlog 619)."""
    governed = (
        None if mutation is None
        else ManifestSource([manifest_row(7, mutation, path, record_type=_RECORD_TYPE)],
                            opened_by="mutation")
    )
    return SourceClassificationCandidate(
        classification=resolve_classification_source_configs({"sources": [entry]})[0],
        manifest_record={"file_manifest_id": 7, "file_mutation_id": mutation,
                         "path": path, "file_name": Path(path).name},
        source_record_type=_RECORD_TYPE,
        value=Path(path).name,
        file_id=7,
        status="ready",
        governed=governed,
    )


def test_an_applied_run_opens_the_selected_mutation_and_carries_the_record_type(
    run_log, tmp_path: Path,
) -> None:
    ctx, _ = _ctx(tmp_path)
    entry = _entry()

    with patch.object(source_classification, "load_source_classification_candidates",
                      return_value=(_candidate(entry, 31, "/data/alpha/inbox/Example7.csv"),)), \
         patch.object(source_classification.ManifestSource, "create",
                      side_effect=AssertionError("opened again through create")), \
         patch.object(source_classification, "Transform") as transform:
        transform.return_value.resolve.return_value.apply.return_value = ("classified",)
        result = run_source_file_classification(
            ctx, run_log, {"sources": [entry]}, RunContext(apply=True))

    transform.assert_called_once_with(
        values={"source": entry}, selected="classify")
    # The DataFile of the candidate's own governed object, at its mutation --
    # hydrated once by get_for_operation, never re-read (backlog 619).
    (opened,) = transform.return_value.resolve.return_value.apply.call_args.args
    assert (opened.path, opened.file_manifest_id, opened.file_mutation_id) == (
        Path("/data/alpha/inbox/Example7.csv"), 7, 31)
    assert (result.candidates, result.classified, result.rejected) == (1, 1, 0)


def test_a_file_with_no_registered_data_file_type_is_a_rejection(
    run_log, tmp_path: Path,
) -> None:
    """An UntypedFile reaches the classify kind, which rejects it terminally;
    the step counts it and goes on (backlog 624)."""
    ctx, _ = _ctx(tmp_path)
    entry = _entry()

    with patch.object(source_classification, "load_source_classification_candidates",
                      return_value=(_candidate(entry, 31, "/data/alpha/inbox/notes.unknown"),)), \
         patch.object(source_classification, "log_run_record", return_value=123) as evidence:
        result = run_source_file_classification(
            ctx, run_log, {"sources": [entry]}, RunContext(apply=True))

    assert (result.candidates, result.classified, result.rejected) == (1, 0, 1)
    assert evidence.call_args.kwargs["reason_code"] == "no_registered_data_file"
    assert evidence.call_args.kwargs["source_record_type"] == _RECORD_TYPE


def test_a_dry_run_classifies_in_memory_and_persists_nothing(
    run_log, tmp_path: Path,
) -> None:
    ctx, _ = _ctx(tmp_path)
    entry = _entry()

    with patch.object(source_classification, "load_source_classification_candidates",
                      return_value=(_candidate(entry, 31, "/data/alpha/inbox/Example7.csv"),)), \
         patch.object(source_classification.ManifestSource, "create") as create, \
         patch.object(source_classification, "log_run_record") as evidence:
        result = run_source_file_classification(
            ctx, run_log, {"sources": [entry]}, RunContext(apply=False))

    create.assert_not_called()
    evidence.assert_not_called()
    assert (result.candidates, result.classified, result.rejected) == (1, 1, 0)


def test_a_candidate_loading_failure_stops_the_step(run_log) -> None:
    with patch.object(source_classification, "load_source_classification_candidates",
                      side_effect=ClassificationError("manifest unavailable")), \
         patch.object(ClassifyTransform, "apply") as apply:
        with pytest.raises(ClassificationError, match="manifest unavailable"):
            run_source_file_classification(
                object(), run_log, {"sources": []}, RunContext(apply=True))

    apply.assert_not_called()

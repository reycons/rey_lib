"""The Loader's record-type profiling step (row 589, step 5).

The legacy step tests (file_operator ``test_record_types``: reporting periods,
sheets, batch isolation, failing the step, unexpected exceptions) asserted on
the legacy StepResult; that text is now the rey_loader handler's, so the same
facts are asserted here on the batch result. After them, the kickout rule
(rule 75, ``a_failed_file_is_kicked_out``): a file that cannot be profiled
moves its ORIGINAL out of processing -- never the sanitized copy -- to the
declared kickouts destination, and the failure is recorded after the move.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, patch

import pytest

from rey_lib.files import file_routing
from rey_lib.files.data_file import data_file_for
from rey_lib.load import profile as workflow
from tests.support.manifest_rows import manifest_row
from tests.support.selecting_control import SelectingControl

PROFILE_CONFIG = {
    "file_selection": {
        "operation": "data_profile",
        "source_field": "path",
    },
}


def _profileable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Name,Amount\n" + "".join(f"Alice,{n}.00\n" for n in range(1, 40)),
        encoding="utf-8",
    )
    return path


def _ctx(tmp_path: Path, *selected: tuple[int, Path],
         data_profile_key: str | None = None) -> SimpleNamespace:
    """A context whose manifest retrieval returns these files, keyed by their
    mutations. The MANIFEST's profile key and the classification's key fields
    are what profiling reads (backlog 619)."""
    control = SelectingControl()
    control.selected = [
        manifest_row(
            mutation_id, mutation_id, path,
            manifest_data_profile_key=data_profile_key or f"group_{mutation_id}",
            classification={"type": "t", "values": {},
                            "key_fields": ["feed", "record_type"]},
        )
        for mutation_id, path in selected
    ]
    return SimpleNamespace(
        app_name="rey_loader",
        paths=SimpleNamespace(resolve=lambda name: tmp_path),
        shared_control=control,
    )


def _run_log(tmp_path: Path, ctx: Any) -> Any:
    """A run log that commits, which is what governed evidence requires."""
    from rey_lib.logs.run_log import RunLog

    return RunLog(
        app="rey_loader",
        run_id="00000000-0000-4000-8000-000000000001",
        run_timestamp="20260822_000000",
        log_dir=str(tmp_path),
        destination="both",
        control=ctx.shared_control,
    )


def _profiles(ctx: Any) -> list[dict]:
    return [row for row in ctx.shared_control.list_file_mutations()
            if row["record_type"] == "source_file_profile"]


def _run(ctx: Any, run_log: Any, config: dict | None = None, *, apply: bool = True):
    """Run the step with its run log bound and its step bound, as the workflow
    coordinator does before any step runs: the profile kind writes through the
    bound run log, and a kickout states the bound step (backlog 624)."""
    from rey_lib.logs import bind_run, bind_step, clear_run, clear_step

    bind_run(run_log)
    bind_step(step_id="profile_csv_record_types")
    try:
        return workflow.run_record_type_profiling(
            ctx, run_log, config or PROFILE_CONFIG, apply=apply, profiler_version="test")
    finally:
        clear_step()
        clear_run()


# -- the legacy step tests, on the batch result --------------------------------

def test_reporting_periods_share_dataset_identity_in_current_object_records(
    tmp_path: Path,
) -> None:
    may = _profileable(tmp_path / "SheetMetalHoldMay26.Portfolio_Holdings.csv")
    june = _profileable(tmp_path / "SheetMetalHoldJun26.Portfolio_Holdings.csv")
    ctx = _ctx(tmp_path, (1, may), (2, june), data_profile_key="SheetMetal|Hold")

    result = _run(ctx, _run_log(tmp_path, ctx))

    assert (result.profiled, result.failures) == (2, ())
    assert {record["source_record_id"] for record in _profiles(ctx)} == {1, 2}
    stored = ctx.shared_control.data_profiles
    assert len(stored) == 1
    assert isinstance(stored[0]["header_definition"], str)
    assert "structural_profile" not in stored[0]


def test_each_mutation_keeps_its_own_current_sheet_profile(tmp_path: Path) -> None:
    first = _profileable(tmp_path / "SheetMetalHoldMay26.Portfolio_Holdings.csv")
    second = _profileable(tmp_path / "SheetMetalHoldMay26.Transactions.csv")
    ctx = _ctx(tmp_path, (1, first), (2, second))

    result = _run(ctx, _run_log(tmp_path, ctx))

    assert (result.profiled, result.failures) == (2, ())
    records = _profiles(ctx)
    assert len(records) == 2
    assert {record["source_record_id"] for record in records} == {1, 2}


def test_one_unprofileable_file_does_not_stop_the_batch(tmp_path: Path) -> None:
    good = _profileable(tmp_path / "good.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("", encoding="utf-8")
    later = _profileable(tmp_path / "later.csv")
    ctx = _ctx(tmp_path, (1, good), (2, bad), (3, later))

    with patch.object(workflow, "log_run_record", return_value=44) as run_logged:
        result = _run(ctx, _run_log(tmp_path, ctx))

    assert {record["source_record_id"] for record in _profiles(ctx)} == {1, 3}
    assert (result.selected, result.profiled) == (3, 2)
    assert len(result.failures) == 1 and result.failures[0].startswith("bad.csv: ")
    errors = [call for call in run_logged.call_args_list if call.args[1] == "ERROR"]
    assert len(errors) == 1
    assert "bad.csv" in errors[0].kwargs["message"]
    assert errors[0].kwargs["path"] == str(bad)
    assert errors[0].kwargs["error_message"] == {"failure_reason": ANY}


def test_a_batch_where_no_file_can_be_profiled_fails_the_step(tmp_path: Path) -> None:
    first = tmp_path / "one.csv"
    first.write_text("", encoding="utf-8")
    second = tmp_path / "two.csv"
    second.write_text("", encoding="utf-8")
    ctx = _ctx(tmp_path, (1, first), (2, second))

    with patch.object(workflow, "log_run_record", return_value=44):
        result = _run(ctx, _run_log(tmp_path, ctx))

    assert (result.selected, result.profiled, len(result.failures)) == (2, 0, 2)
    assert _profiles(ctx) == []


def test_an_unexpected_exception_still_fails_the_step(tmp_path: Path) -> None:
    """Only data-specific failures are isolated; a bug must not be swallowed."""
    source = _profileable(tmp_path / "source.csv")
    ctx = _ctx(tmp_path, (1, source))

    with patch.object(workflow, "build_csv_parts", side_effect=RuntimeError("unexpected")), \
         pytest.raises(RuntimeError, match="unexpected"):
        _run(ctx, _run_log(tmp_path, ctx))


def test_a_dry_run_profiles_and_writes_nothing(tmp_path: Path) -> None:
    source = _profileable(tmp_path / "source.csv")
    ctx = _ctx(tmp_path, (1, source))

    result = _run(ctx, _run_log(tmp_path, ctx), apply=False)

    assert (result.applied, result.selected, result.profiled) == (False, 1, 0)
    assert _profiles(ctx) == []


# -- rule 75: the original is kicked out, then the failure is recorded ---------

def _failing_sanitized_copy(
    tmp_path: Path, where: str = "processing",
) -> tuple[Path, Path, Path, SimpleNamespace]:
    """A sanitized copy that cannot be profiled, and its ORIGINAL in ``where``.

    The row carries the original's facts, as f_file_manifest_get returns them:
    its inventoried path (so its inbox) and where it is now, at which state.
    """
    feed = tmp_path / "alpha"
    inbox = feed / "source" / "inbox"
    original = feed / "source" / where / "Trades.csv"
    original.parent.mkdir(parents=True)
    original.write_text("anything\n", encoding="utf-8")
    sanitized = feed / "work" / "sanitized_csv" / "Trades.csv"
    sanitized.parent.mkdir(parents=True)
    sanitized.write_text("", encoding="utf-8")
    ctx = _ctx(tmp_path, (52, sanitized))
    ctx.shared_control.selected[0].update({
        "file_manifest_id": 7,
        "manifest_path": str(inbox / "Trades.csv"),
        "original_path": str(original),
        "original_mutation_id": 3,
    })
    return original, sanitized, inbox, ctx


def test_a_failed_profile_kicks_out_the_original_then_records_the_failure(
    tmp_path: Path,
) -> None:
    original, sanitized, inbox, ctx = _failing_sanitized_copy(tmp_path)
    order: list[str] = []

    def moved(*_args: Any, **_kw: Any) -> Path:
        order.append("move")
        return inbox / "kickouts" / "Trades.csv"

    def logged(_run_log: Any, record_type: str, **_kw: Any) -> int:
        order.append(record_type)
        return 44

    with patch.object(workflow.ManifestSource, "create",
                      side_effect=AssertionError("no second read of the file")), \
         patch.object(file_routing, "move_file", side_effect=moved) as move, \
         patch.object(file_routing, "log_source_file_mutation", return_value=90) as mutation, \
         patch.object(workflow, "log_run_record", side_effect=logged):
        result = _run(ctx, _run_log(tmp_path, ctx))

    # The ORIGINAL left processing for its inbox's kickouts -- moved by the
    # common execution path; the sanitized copy that was profiled is untouched.
    assert move.call_args.args[:2] == (original, inbox / "kickouts")
    assert mutation.call_args.kwargs["reason"] == "moved_to_kickouts"
    assert mutation.call_args.kwargs["operation"] == "profile_csv_record_types"
    assert mutation.call_args.kwargs["run_log_fields"] == {"source_record_id": 3}
    assert sanitized.exists()
    # Kickout first, then the failure (rule 75); the step still fails.
    assert order == ["move", "ERROR"]
    assert result.failures and result.failures[0].startswith("Trades.csv: ")


def test_an_original_is_kicked_out_from_wherever_it_is(tmp_path: Path) -> None:
    """One rule: an archived original moves too."""
    original, _sanitized, inbox, ctx = _failing_sanitized_copy(tmp_path, where="archive")

    with patch.object(file_routing, "move_file",
                      return_value=inbox / "kickouts" / "Trades.csv") as move, \
         patch.object(file_routing, "log_source_file_mutation", return_value=90), \
         patch.object(workflow, "log_run_record", return_value=44) as run_logged:
        result = _run(ctx, _run_log(tmp_path, ctx))

    assert move.call_args.args[:2] == (original, inbox / "kickouts")
    assert [c.args[1] for c in run_logged.call_args_list].count("ERROR") == 1
    assert len(result.failures) == 1


def test_a_step_declares_no_kickouts(tmp_path: Path) -> None:
    """The destination is intrinsic: a kickouts setting is not read at all."""
    original, _sanitized, inbox, ctx = _failing_sanitized_copy(tmp_path)
    config = {**PROFILE_CONFIG, "kickouts": {"path": "/somewhere/else/<file_name>"}}

    with patch.object(file_routing, "move_file",
                      return_value=inbox / "kickouts" / "Trades.csv") as move, \
         patch.object(file_routing, "log_source_file_mutation", return_value=90), \
         patch.object(workflow, "log_run_record", return_value=44):
        _run(ctx, _run_log(tmp_path, ctx), config)

    assert move.call_args.args[:2] == (original, inbox / "kickouts")


# -- the governed object, not a selector row (backlog 619) --------------------

def test_the_step_asks_once_for_its_operation(tmp_path: Path) -> None:
    source = _profileable(tmp_path / "source.csv")
    ctx = _ctx(tmp_path, (1, source))

    _run(ctx, _run_log(tmp_path, ctx), apply=False)

    assert ctx.shared_control.manifest_requests == [{
        "installation_id": 1, "operation": "data_profile", "source_name": None,
    }]


def test_the_manifests_profile_key_is_profiled_not_the_types(tmp_path: Path) -> None:
    """The object's plain data_profile_key is the file TYPE's profile; profiling
    groups by the MANIFEST's, as the retired selector projected it."""
    source = _profileable(tmp_path / "source.csv")
    ctx = _ctx(tmp_path, (1, source), data_profile_key="Manifest|Group")
    ctx.shared_control.selected[0]["data_profile_key"] = "the-file-types-profile"

    result = _run(ctx, _run_log(tmp_path, ctx))

    assert result.profiled == 1
    assert [row["data_profile_key"] for row in ctx.shared_control.data_profiles] == [
        "Manifest|Group",
    ]


def test_a_retired_procedure_key_is_refused_by_name(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    config = {"file_selection": {"procedure": "get_files_to_profile", "source_field": "path"}}

    with pytest.raises(workflow.ProfilingError, match="procedure is retired"):
        _run(ctx, _run_log(tmp_path, ctx), config)

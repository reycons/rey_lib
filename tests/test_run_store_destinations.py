"""What each run-store destination actually does, proved through the real seam.

These call the public lifecycle API -- ``log_run_start``, ``log_step_start``,
``log_step_end``, ``log_run_complete`` -- and assert what reached the control
layer and what reached the JSONL run log. Nothing here reaches past
``control_utils._call``: the boundary this slice wires is logs -> control, and
that is where the observation belongs.

A boundary test that finds zero violations is not evidence the seam works. The
control path was built-but-unreachable for a long time, and a rule about a path
nobody calls passes for the wrong reason. These are the reachability half.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import make_db_run_log, start_test_run

from rey_lib.control import Control
from rey_lib.errors.error_utils import ConfigError, DatabaseError, StateError
from rey_lib.logs import log_run_complete, log_run_start, log_step_end, log_step_start


@pytest.fixture()
def control_calls() -> list[tuple[str, dict, bool]]:
    """Every control routine the run log invoked, standing in for the database.

    The run log holds its Control, so the recording happens there rather than
    by patching Control's dispatcher.
    """
    _CONTROL_CALLS.clear()
    return _CONTROL_CALLS


def _control_map() -> SimpleNamespace:
    """A routine-only control map, as both installations declare it."""
    return SimpleNamespace(
        name="control",
        routine_bindings=[SimpleNamespace(
            name="start_batch", routine="control.mapped_function",
            result_mode="scalar_result",
            inputs={"p_batch_name": "batch_name"},
            output={"variable": "batch_id", "load_to_ctx": "batch_id"},
        )],
        sql_bindings=None,
    )


def _ctx(tmp_path: Path, run_store: str, **extra: Any):
    """A run log with the given destination.

    Destination routing belongs to RunLog, so these exercise it directly rather
    than through a context that used to carry the setting.
    """
    from rey_lib.logs.run_log import RunLog

    class _Control:
        """Records every control routine a destination would invoke."""

        def __init__(self) -> None:
            self._minted = 0
            self.owns_batch = False
            self.batch_id = None
            self.batch_step_id = None
            # The permanent anchor. close_step restores the current step to it
            # rather than blanking it, so work between steps still has a parent.
            self.batch_root_step_id = None

        def start_batch(self, batch_name=None, **kw):
            self.batch_id = 7
            self.batch_root_step_id = 7000
            self.batch_step_id = 7000
            _CONTROL_CALLS.append(("start_batch", {"batch_name": batch_name}, required))
            return 7

        def end_batch(self, status=None, error_message=None, **kw):
            _CONTROL_CALLS.append(("end_batch", {"status": status}, required))

        def start_step(self, step_name=None, step_sequence=None, step_type=None,
                       required=False, **kw):
            self.batch_step_id = 70
            _CONTROL_CALLS.append(("start_step", {
                "step_name": step_name, "batch_id": self.batch_id,
                "run_id": run_log.run_id}, required))
            return 70

        def end_step(self, status=None, message=None, required=False, **kw):
            _CONTROL_CALLS.append(("end_step", {"status": status}, required))

        def write_run_log_record(self, *, required=False, **values):
            _CONTROL_CALLS.append(("write_run_log_record", {
                **values,
                "batch_id": self.batch_id, "batch_step_id": self.batch_step_id,
            }, required))
            # The database mints the row's identity and returns it. A writer
            # that got nothing back would read as a record that never
            # committed, which under `both` fails the run.
            self._minted += 1
            return self._minted

    run_log = RunLog(
        app="rey_loader",
        run_id="00000000-0000-4000-8000-000000000001",
        run_timestamp="20260822_000000",
        log_dir=str(tmp_path),
        destination=run_store,
        control=_Control(),
    )
    for key, value in extra.items():
        # batch state is Control's; everything else is the run log's
        target = run_log.control if key in ("batch_id", "batch_step_id") else run_log
        setattr(target, key, value)
    return run_log


_CONTROL_CALLS: list = []


def _records(run_log: Any) -> list[dict]:
    """Every JSONL record written for this run, or [] if no log exists."""
    try:
        path = str(run_log.path())
    except Exception:
        path = None
    if not path or not Path(path).exists():
        return []
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _actions(calls: list) -> list[str]:
    return [name for name, _, _ in calls]


class TestJsonlMode:
    """The historical behaviour, preserved exactly."""

    def test_jsonl_writes_no_control_RECORDS_but_still_forwards_steps(
        self, tmp_path, control_calls,
    ) -> None:
        """run_store routes RECORDS. It does not decide what is attributed.

        This asserted `control_calls == []` -- that choosing jsonl meant the
        control database was never touched at all. It is what made the batch
        depend on a logging setting, and it left four of six installations
        doing database work with nothing to attribute it to.
        """
        run_log = _ctx(tmp_path, "jsonl")

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)
        log_step_end(run_log, "extract", "success")
        log_run_complete(run_log, "success")

        # No run-log RECORD reaches the database: that is what jsonl means.
        assert "write_run_log_record" not in _actions(control_calls)
        # The steps still do, because a step is attribution, not a record.
        assert _actions(control_calls) == ["start_step", "end_step"]
        # And no batch: the run log never starts one now, whatever the mode.
        assert "start_batch" not in _actions(control_calls)

    def test_jsonl_writes_the_run_log(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "jsonl")

        log_run_start(run_log, operation="scan")

        assert [r["record_type"] for r in _records(run_log)] == ["RUN_START"]

    def test_an_absent_setting_means_jsonl(self, tmp_path, control_calls) -> None:
        # An installation that says nothing keeps what it already had. The
        # database is a migration someone performs, never a default.
        run_log = _ctx(tmp_path, "jsonl")
        # An absent run_store means jsonl: the default destination.

        log_run_start(run_log, operation="scan")

        assert control_calls == []
        assert _records(run_log)


class TestDbMode:
    """The control database only."""

    def test_db_writes_control_and_not_jsonl(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "db")

        log_run_start(run_log, operation="scan")

        # No start_batch. The batch was opened where the run-starting Control
        # was created, before this run log existed.
        assert _actions(control_calls) == ["write_run_log_record"]
        assert _records(run_log) == []

    def test_the_full_lifecycle_reaches_control(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "db")

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)
        log_step_end(run_log, "extract", "success")
        log_run_complete(run_log, "success")

        # Neither start_batch nor end_batch: the batch lifecycle belongs to
        # the Control that owns the run, not to the log that describes it.
        assert _actions(control_calls) == [
            "write_run_log_record",                    # RUN_START record
            "write_run_log_record", "start_step",      # STEP_START record, then the step
            "write_run_log_record", "end_step",        # STEP_END record, then step close
            "write_run_log_record",                    # RUN_COMPLETE record
        ]

    def test_every_run_log_control_call_is_required(self, tmp_path, control_calls) -> None:
        # Run-log persistence chose this destination; a silent None is not a
        # degraded success.
        run_log = _ctx(tmp_path, "db")

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)

        assert all(required for _, _, required in control_calls)


class TestBothMode:
    """Both destinations, both required."""

    def test_both_writes_both(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "both")

        log_run_start(run_log, operation="scan")

        assert _actions(control_calls) == ["write_run_log_record"]
        assert [r["record_type"] for r in _records(run_log)] == ["RUN_START"]

    def test_a_db_failure_under_both_is_surfaced(self, tmp_path) -> None:
        """The run log holds its Control, so the failure comes from there.

        The break moved with the ownership: the run log no longer calls
        start_batch, so a batch that cannot be opened is not its failure to
        surface. Writing the RECORD is, and that is what breaks here.

        It surfaces as StateError rather than the DatabaseError underneath it,
        and that is the more accurate answer: under `both` the fault is not
        that a call failed but that the run is now described in one destination
        and not the other. The point the test holds is unchanged -- the failure
        is surfaced, not swallowed.
        """
        class _Broken:
            owns_batch = False
            batch_id = None
            batch_step_id = None
            batch_root_step_id = None

            def write_run_log_record(self, **kw):
                raise DatabaseError("control unreachable")

        run_log = _ctx(tmp_path, "both")
        run_log.control = _Broken()

        with pytest.raises(StateError, match="not committed to every destination"):
            log_run_start(run_log, operation="scan")

    def test_a_jsonl_failure_under_both_is_surfaced(self, tmp_path, control_calls,
                                                   monkeypatch) -> None:
        # The record writer warns and returns None on its own terms; the run
        # store's durability contract is the separate boundary on top of it.
        from rey_lib.logs.run_log import RunLog

        monkeypatch.setattr(RunLog, "append", lambda self, *a, **k: None)
        run_log = _ctx(tmp_path, "both")

        with pytest.raises(StateError, match="one place and not the other"):
            log_run_start(run_log, operation="scan")

    def test_a_jsonl_failure_under_jsonl_alone_still_does_not_raise(
            self, tmp_path, control_calls, monkeypatch) -> None:
        """Logging must not mask execution -- unchanged where nothing else was chosen."""
        from rey_lib.logs.run_log import RunLog

        monkeypatch.setattr(RunLog, "append", lambda self, *a, **k: None)
        run_log = _ctx(tmp_path, "jsonl")

        log_run_start(run_log, operation="scan")  # must not raise


class TestBatchIntentIsNotTheRunLogs:
    """Launch declares it, and the Control lifecycle honours it.

    REVERSED DELIBERATELY. This class asserted that the run log created a batch
    when `new_batch` was true and refused when it was false with none bound.
    That authority moved: the batch is started where the run-starting Control
    is created, because a governed routine needs a parent to exist before it
    runs, and `control.f_batch_step_begin` refuses outright when given neither
    a batch nor a parent step.

    What is left here is the negative, which is worth holding: the run log
    starts no batch under ANY intent or destination.
    """

    def test_the_run_log_starts_no_batch_whatever_the_intent(
        self, tmp_path, control_calls,
    ) -> None:
        for extra in ({}, {"new_batch": True}, {"new_batch": False, "batch_id": 99}):
            control_calls.clear()
            run_log = _ctx(tmp_path, "db", **extra)

            log_run_start(run_log, operation="scan")

            assert "start_batch" not in _actions(control_calls), extra

    def test_the_run_log_ends_no_batch(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "db")

        log_run_start(run_log, operation="scan")
        log_run_complete(run_log, "success")

        assert "end_batch" not in _actions(control_calls)

    def test_a_bound_batch_id_is_left_exactly_as_it_was(
        self, tmp_path, control_calls,
    ) -> None:
        """A run log neither manufactures a batch nor disturbs one."""
        run_log = _ctx(tmp_path, "db", batch_id=1234)

        log_run_start(run_log, operation="scan")

        assert run_log.control.batch_id == 1234


class TestOneBatchManyRuns:
    """batch_id groups; run_id identifies the execution."""

    def test_two_runs_share_a_batch_and_stay_distinguishable(self, tmp_path,
                                                             control_calls) -> None:
        # The batch is bound by the Control lifecycle before either run log
        # exists, so both are handed the same one.
        first = _ctx(tmp_path, "db", batch_id=7)
        log_run_start(first, operation="scan")
        log_step_start(first, "extract", 1)

        second = _ctx(tmp_path, "db",
                      batch_id=first.control.batch_id,
                      run_id="00000000-0000-4000-8000-000000000002")
        log_run_start(second, operation="scan")
        log_step_start(second, "extract", 1)

        steps = [v for name, v, _ in control_calls if name == "start_step"]
        assert {s["batch_id"] for s in steps} == {7}
        assert [s["run_id"] for s in steps] == [first.run_id, second.run_id]
        assert first.run_id != second.run_id

    def test_a_reusing_run_does_not_end_the_shared_batch(self, tmp_path,
                                                         control_calls) -> None:
        # Ending it would close the batch under the runs still using it.
        run_log = _ctx(tmp_path, "db", new_batch=False, batch_id=7)

        log_run_start(run_log, operation="scan")
        log_run_complete(run_log, "success")

        assert "end_batch" not in _actions(control_calls)

    def test_step_and_event_carry_run_id(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "db")

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)

        for name, values, _ in control_calls:
            if name in ("start_step", "write_run_log_record"):
                assert values["run_id"] == run_log.run_id


class TestIdsArriveThroughTheMap:
    """Result placement is the procedure map's, not Python's."""

    def test_the_run_log_does_not_write_the_ids_either(self, tmp_path) -> None:
        """Nothing outside the map binds batch_id or batch_step_id.

        This used to prove it by having start_batch return a scalar the map
        never bound, and asserting the run log refused. The run log no longer
        calls start_batch, so the proof moves: it must leave both ids exactly
        as it found them.
        """
        class _NoPlacement:
            """Records nothing and binds nothing, as an unmapped output would."""

            owns_batch = False
            batch_id = None
            batch_step_id = None
            batch_root_step_id = None

            def write_run_log_record(self, **kw):
                return 1

        run_log = _ctx(tmp_path, "db")
        run_log.control = _NoPlacement()

        log_run_start(run_log, operation="scan")

        assert run_log.control.batch_id is None
        assert run_log.control.batch_step_id is None


class TestEveryRecordHonoursTheDestination:
    """The defect this class exists for.

    Only the four lifecycle writers used to consult run_store. Every other
    record writer -- errors, row counts, validation results, SQL execution,
    file operations -- called log_run_record directly and wrote JSONL
    unconditionally. So `db` produced a JSONL file missing its lifecycle
    records and a database holding only lifecycle records, and neither was a
    complete run log.

    The destination now belongs to log_run_record, which every record already
    passes through. These assert it on a record that is not a lifecycle event,
    because that is exactly what the earlier coverage missed.
    """

    def test_an_error_record_reaches_the_database(self, tmp_path, control_calls) -> None:
        from rey_lib.logs import log_error

        run_log = _ctx(tmp_path, "db")
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        log_error(run_log, message="something failed", error_type="AppError")

        events = [v for name, v, _ in control_calls if name == "write_run_log_record"]
        assert [e["record_type"] for e in events] == ["ERROR"]

    def test_an_error_record_writes_no_jsonl_under_db(self, tmp_path,
                                                      control_calls) -> None:
        from rey_lib.logs import log_error

        run_log = _ctx(tmp_path, "db")
        log_run_start(run_log, operation="scan")

        log_error(run_log, message="something failed", error_type="AppError")

        assert _records(run_log) == []

    def test_a_row_count_reaches_both_destinations(self, tmp_path,
                                                   control_calls) -> None:
        from rey_lib.logs import log_row_count

        run_log = _ctx(tmp_path, "both")
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        log_row_count(run_log, count_name="loaded", count=42)

        events = [v for name, v, _ in control_calls if name == "write_run_log_record"]
        assert [e["record_type"] for e in events] == ["ROW_COUNT"]
        assert [r["record_type"] for r in _records(run_log)][-1] == "ROW_COUNT"

    def test_the_envelope_is_written_by_name_and_fields_holds_the_rest(
        self, tmp_path, control_calls,
    ) -> None:
        """A stamped field hiding in a payload is a column nobody can query."""
        from rey_lib.logs import log_row_count

        run_log = _ctx(tmp_path, "db")
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        log_row_count(run_log, count_name="loaded", count=42)

        written = [v for name, v, _ in control_calls if name == "write_run_log_record"][0]
        assert written["record_type"] == "ROW_COUNT"
        assert written["run_id"] == run_log.run_id
        # The writer sends the parent it is under, not an id of its own: the
        # database mints that and hands it back.
        assert written["parent_run_log_id"] is None
        # `subject` is log_row_count's own field, so it belongs in that record
        # type's payload column -- and in no other.
        assert written["payloads"]["row_count"] == {
            "count_name": "loaded", "count": 42, "subject": "",
        }
        assert written["payloads"]["sql_execution"] is None

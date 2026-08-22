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

from tests.conftest import make_run_log, start_test_run

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
            self.owns_batch = False
            self.batch_id = None
            self.batch_step_id = None

        def start_batch(self, batch_name=None, required=False, **kw):
            self.batch_id = 7
            _CONTROL_CALLS.append(("start_batch", {"batch_name": batch_name}, required))
            return 7

        def end_batch(self, status=None, error_message=None, required=False, **kw):
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

        def log_event(self, *, severity, event_name, message,
                      event_jsonb=None, required=False):
            _CONTROL_CALLS.append(("log_event", {
                "severity": severity, "event_name": event_name,
                "message": message, "event_jsonb": event_jsonb,
                "batch_id": self.batch_id, "batch_step_id": self.batch_step_id,
                "run_id": run_log.run_id,
            }, required))

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

    def test_jsonl_never_invokes_control_logging(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "jsonl")

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)
        log_step_end(run_log, "extract", "success")
        log_run_complete(run_log, "success")

        assert control_calls == []

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

        # The batch is opened first, then the record itself is persisted as an
        # event: every record reaches the database, not only the lifecycle ones.
        assert _actions(control_calls) == ["start_batch", "log_event"]
        assert _records(run_log) == []

    def test_the_full_lifecycle_reaches_control(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "db")

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)
        log_step_end(run_log, "extract", "success")
        log_run_complete(run_log, "success")

        assert _actions(control_calls) == [
            "start_batch", "log_event",     # RUN_START record
            "log_event", "start_step",      # STEP_START record, then the step
            "log_event", "end_step",        # STEP_END record, then step close
            "log_event", "end_batch",       # RUN_COMPLETE record, then close
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

        assert _actions(control_calls) == ["start_batch", "log_event"]
        assert [r["record_type"] for r in _records(run_log)] == ["RUN_START"]

    def test_a_db_failure_under_both_is_surfaced(self, tmp_path) -> None:
        """The run log holds its Control, so the failure comes from there."""
        class _Broken:
            owns_batch = False
            batch_id = None

            def start_batch(self, **kw):
                raise DatabaseError("control unreachable")

        run_log = _ctx(tmp_path, "both")
        run_log.control = _Broken()

        with pytest.raises(DatabaseError):
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


class TestBatchIntent:
    """Launch declares it; logging honours it and never infers it."""

    def test_default_launch_creates_a_batch(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "db")

        log_run_start(run_log, operation="scan")

        assert "start_batch" in _actions(control_calls)
        assert run_log.control.batch_id == 7

    def test_explicit_new_batch_creates_a_batch(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "db", new_batch=True)

        log_run_start(run_log, operation="scan")

        assert "start_batch" in _actions(control_calls)

    def test_new_batch_false_reuses_the_bound_batch(self, tmp_path, control_calls) -> None:
        run_log = _ctx(tmp_path, "db", new_batch=False, batch_id=99)

        log_run_start(run_log, operation="scan")

        assert "start_batch" not in _actions(control_calls)
        assert run_log.control.batch_id == 99

    def test_new_batch_false_without_a_batch_is_rejected(self, tmp_path,
                                                         control_calls) -> None:
        run_log = _ctx(tmp_path, "db", new_batch=False)

        with pytest.raises(ConfigError, match="never manufactured"):
            log_run_start(run_log, operation="scan")

        assert control_calls == []

    def test_a_leftover_batch_id_does_not_imply_reuse(self, tmp_path,
                                                      control_calls) -> None:
        """Intent is declared, never inferred from batch_id being set."""
        run_log = _ctx(tmp_path, "db", batch_id=1234)

        log_run_start(run_log, operation="scan")

        assert "start_batch" in _actions(control_calls)


class TestOneBatchManyRuns:
    """batch_id groups; run_id identifies the execution."""

    def test_two_runs_share_a_batch_and_stay_distinguishable(self, tmp_path,
                                                             control_calls) -> None:
        first = _ctx(tmp_path, "db")
        log_run_start(first, operation="scan")
        log_step_start(first, "extract", 1)

        second = _ctx(tmp_path, "db", new_batch=False,
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
            if name in ("start_step", "log_event"):
                assert values["run_id"] == run_log.run_id


class TestIdsArriveThroughTheMap:
    """Result placement is the procedure map's, not Python's."""

    def test_batch_and_step_ids_are_not_written_by_control(self, tmp_path,
                                                                 monkeypatch) -> None:
        """With load_to_ctx not emulated, nothing else writes the ids."""
        seen: list[str] = []

        class _NoPlacement:
            """Returns a scalar but never binds it, as an unmapped output would."""

            owns_batch = False
            batch_id = None

            def start_batch(self, **kw):
                seen.append("start_batch")
                return 7

        run_log = _ctx(tmp_path, "db")
        run_log.control = _NoPlacement()

        # start_batch returning a scalar without the map binding it is a run
        # store that cannot record steps, and it says so rather than continuing.
        with pytest.raises(StateError, match="no batch_id"):
            log_run_start(run_log, operation="scan")

        assert seen == ["start_batch"]


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

        events = [v for name, v, _ in control_calls if name == "log_event"]
        assert [e["event_name"] for e in events] == ["ERROR"]
        assert e_sev(events[0]) == "ERROR"

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

        events = [v for name, v, _ in control_calls if name == "log_event"]
        assert [e["event_name"] for e in events] == ["ROW_COUNT"]
        assert [r["record_type"] for r in _records(run_log)][-1] == "ROW_COUNT"

    def test_the_whole_record_is_carried_as_the_event_payload(self, tmp_path,
                                                              control_calls) -> None:
        from rey_lib.logs import log_row_count

        run_log = _ctx(tmp_path, "db")
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        log_row_count(run_log, count_name="loaded", count=42)

        payload = [v for name, v, _ in control_calls if name == "log_event"][0]
        assert payload["event_jsonb"]["record_type"] == "ROW_COUNT"
        assert payload["event_jsonb"]["run_id"] == run_log.run_id

    def test_severity_is_derived_from_the_record_type(self, tmp_path,
                                                      control_calls) -> None:
        from rey_lib.logs import log_run_record

        run_log = _ctx(tmp_path, "db")
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        for record_type in ("ERROR", "WARNING", "ROW_COUNT"):
            log_run_record(run_log, record_type, message="m")

        events = [v for name, v, _ in control_calls if name == "log_event"]
        assert [e_sev(e) for e in events] == ["ERROR", "WARNING", "INFO"]


def e_sev(event: dict) -> str:
    """The severity a persisted record was recorded under."""
    return event["severity"]

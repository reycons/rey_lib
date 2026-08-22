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

from tests.conftest import make_run_log

from rey_lib.control import Control
from rey_lib.errors.error_utils import ConfigError, DatabaseError, StateError
from rey_lib.logs import log_run_complete, log_run_start, log_step_end, log_step_start
from rey_lib.run.identity import establish_run_identity


@pytest.fixture()
def control_calls(monkeypatch) -> list[tuple[str, dict, bool]]:
    """Record every control routine invocation, standing in for the database."""
    calls: list[tuple[str, dict, bool]] = []

    def _fake(self, action_name: str, variables: dict,
              required: bool = False) -> Any:
        calls.append((action_name, variables, required))
        # The map's load_to_ctx binds ids onto Control; emulate that one effect.
        if action_name == "start_batch":
            self.batch_id = 7
            return 7
        if action_name == "start_step":
            self.batch_step_id = 70
            return 70
        return None

    monkeypatch.setattr(Control, "_call", _fake)
    return calls


def _control_map() -> SimpleNamespace:
    """A routine-only control map, as both installations declare it."""
    return SimpleNamespace(
        name="control",
        routine_bindings=[SimpleNamespace(
            name="start_batch", routine="control.f_start_batch",
            result_mode="scalar_result",
            inputs={"p_batch_name": "batch_name"},
            output={"variable": "batch_id", "load_to_ctx": "batch_id"},
        )],
        sql_bindings=None,
    )


def _ctx(tmp_path: Path, run_store: str, **extra: Any) -> SimpleNamespace:
    """A launched context with the given run-store destination."""
    ctx = SimpleNamespace(
        log_file=str(tmp_path / "app.run.jsonl"),
        owner_app_name="rey_loader",
        app_name="rey_loader",
        logging=SimpleNamespace(run_store=run_store, db_connection="control"),
        control=SimpleNamespace(procedure_map="control", enabled=False),
        procedure_maps=[_control_map(), SimpleNamespace(name="rey_loader")],
        **extra,
    )
    establish_run_identity(ctx)
    return ctx


def _records(ctx: Any) -> list[dict]:
    """Every JSONL record written for this run, or [] if no log exists."""
    path = getattr(ctx, "run_log_path", None)
    if not path or not Path(path).exists():
        return []
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _actions(calls: list) -> list[str]:
    return [name for name, _, _ in calls]


class TestJsonlMode:
    """The historical behaviour, preserved exactly."""

    def test_jsonl_never_invokes_control_logging(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "jsonl")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)
        log_step_end(run_log, "extract", "success")
        log_run_complete(run_log, "success")

        assert control_calls == []

    def test_jsonl_writes_the_run_log(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "jsonl")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")

        assert [r["record_type"] for r in _records(ctx)] == ["RUN_START"]

    def test_an_absent_setting_means_jsonl(self, tmp_path, control_calls) -> None:
        # An installation that says nothing keeps what it already had. The
        # database is a migration someone performs, never a default.
        ctx = _ctx(tmp_path, "jsonl")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
        ctx.logging = SimpleNamespace(db_connection="control")

        log_run_start(run_log, operation="scan")

        assert control_calls == []
        assert _records(ctx)


class TestDbMode:
    """The control database only."""

    def test_db_writes_control_and_not_jsonl(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")

        # The batch is opened first, then the record itself is persisted as an
        # event: every record reaches the database, not only the lifecycle ones.
        assert _actions(control_calls) == ["start_batch", "log_event"]
        assert _records(ctx) == []

    def test_the_full_lifecycle_reaches_control(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

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
        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)

        assert all(required for _, _, required in control_calls)


class TestBothMode:
    """Both destinations, both required."""

    def test_both_writes_both(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "both")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")

        assert _actions(control_calls) == ["start_batch", "log_event"]
        assert [r["record_type"] for r in _records(ctx)] == ["RUN_START"]

    def test_a_db_failure_under_both_is_surfaced(self, tmp_path, monkeypatch) -> None:
        def _boom(self, action_name, variables, required=False):
            raise DatabaseError("control unreachable")

        monkeypatch.setattr(Control, "_call", _boom)
        ctx = _ctx(tmp_path, "both")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        with pytest.raises(DatabaseError):
            log_run_start(run_log, operation="scan")

    def test_a_jsonl_failure_under_both_is_surfaced(self, tmp_path, control_calls,
                                                   monkeypatch) -> None:
        # The record writer warns and returns None on its own terms; the run
        # store's durability contract is the separate boundary on top of it.
        from rey_lib.logs import execution_records

        monkeypatch.setattr(execution_records, "log_run_record",
                            lambda *a, **k: None)
        ctx = _ctx(tmp_path, "both")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        with pytest.raises(StateError, match="one place and not the other"):
            log_run_start(run_log, operation="scan")

    def test_a_jsonl_failure_under_jsonl_alone_still_does_not_raise(
            self, tmp_path, control_calls, monkeypatch) -> None:
        """Logging must not mask execution -- unchanged where nothing else was chosen."""
        from rey_lib.logs import execution_records

        monkeypatch.setattr(execution_records, "log_run_record",
                            lambda *a, **k: None)
        ctx = _ctx(tmp_path, "jsonl")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")  # must not raise


class TestBatchIntent:
    """Launch declares it; logging honours it and never infers it."""

    def test_default_launch_creates_a_batch(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")

        assert "start_batch" in _actions(control_calls)
        assert ctx.control_api.batch_id == 7

    def test_explicit_new_batch_creates_a_batch(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "db", new_batch=True)
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")

        assert "start_batch" in _actions(control_calls)

    def test_new_batch_false_reuses_the_bound_batch(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "db", new_batch=False, batch_id=99)
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")

        assert "start_batch" not in _actions(control_calls)
        assert ctx.control_api.batch_id == 99

    def test_new_batch_false_without_a_batch_is_rejected(self, tmp_path,
                                                         control_calls) -> None:
        ctx = _ctx(tmp_path, "db", new_batch=False)
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        with pytest.raises(ConfigError, match="never manufactured"):
            log_run_start(run_log, operation="scan")

        assert control_calls == []

    def test_a_leftover_batch_id_does_not_imply_reuse(self, tmp_path,
                                                      control_calls) -> None:
        """Intent is declared, never inferred from batch_id being set."""
        ctx = _ctx(tmp_path, "db", batch_id=1234)
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")

        assert "start_batch" in _actions(control_calls)


class TestOneBatchManyRuns:
    """batch_id groups; run_id identifies the execution."""

    def test_two_runs_share_a_batch_and_stay_distinguishable(run_log, self, tmp_path,
                                                             control_calls) -> None:
        first = _ctx(tmp_path, "db")
        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)

        second = _ctx(tmp_path, "db", new_batch=False,
                      batch_id=first.control_api.batch_id)
        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)

        steps = [v for name, v, _ in control_calls if name == "start_step"]
        assert {s["batch_id"] for s in steps} == {7}
        assert [s["run_id"] for s in steps] == [first.run_id, second.run_id]
        assert first.run_id != second.run_id

    def test_a_reusing_run_does_not_end_the_shared_batch(self, tmp_path,
                                                         control_calls) -> None:
        # Ending it would close the batch under the runs still using it.
        ctx = _ctx(tmp_path, "db", new_batch=False, batch_id=7)
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")
        log_run_complete(run_log, "success")

        assert "end_batch" not in _actions(control_calls)

    def test_step_and_event_carry_run_id(self, tmp_path, control_calls) -> None:
        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

        log_run_start(run_log, operation="scan")
        log_step_start(run_log, "extract", 1)

        for name, values, _ in control_calls:
            if name in ("start_step", "log_event"):
                assert values["run_id"] == ctx.run_id


class TestIdsArriveThroughTheMap:
    """Result placement is the procedure map's, not Python's."""

    def test_batch_and_step_ids_are_not_written_by_control(self, tmp_path,
                                                                 monkeypatch) -> None:
        """With load_to_ctx not emulated, nothing else writes the ids."""
        seen: list[str] = []

        def _fake(self, action_name, variables, required=False):
            seen.append(action_name)
            return 7  # a scalar returned, but no placement onto Control

        monkeypatch.setattr(Control, "_call", _fake)
        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

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

        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        log_error(run_log, message="something failed", error_type="AppError")

        events = [v for name, v, _ in control_calls if name == "log_event"]
        assert [e["event_name"] for e in events] == ["ERROR"]
        assert e_sev(events[0]) == "ERROR"

    def test_an_error_record_writes_no_jsonl_under_db(self, tmp_path,
                                                      control_calls) -> None:
        from rey_lib.logs import log_error

        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
        log_run_start(run_log, operation="scan")

        log_error(run_log, message="something failed", error_type="AppError")

        assert _records(ctx) == []

    def test_a_row_count_reaches_both_destinations(self, tmp_path,
                                                   control_calls) -> None:
        from rey_lib.logs import log_row_count

        ctx = _ctx(tmp_path, "both")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        log_row_count(run_log, count_name="loaded", count=42)

        events = [v for name, v, _ in control_calls if name == "log_event"]
        assert [e["event_name"] for e in events] == ["ROW_COUNT"]
        assert [r["record_type"] for r in _records(ctx)][-1] == "ROW_COUNT"

    def test_the_whole_record_is_carried_as_the_event_payload(self, tmp_path,
                                                              control_calls) -> None:
        from rey_lib.logs import log_row_count

        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        log_row_count(run_log, count_name="loaded", count=42)

        payload = [v for name, v, _ in control_calls if name == "log_event"][0]
        assert payload["event_jsonb"]["record_type"] == "ROW_COUNT"
        assert payload["event_jsonb"]["run_id"] == ctx.run_id

    def test_severity_is_derived_from_the_record_type(self, tmp_path,
                                                      control_calls) -> None:
        from rey_lib.logs import log_run_record

        ctx = _ctx(tmp_path, "db")
        run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
        log_run_start(run_log, operation="scan")
        control_calls.clear()

        for record_type in ("ERROR", "WARNING", "ROW_COUNT"):
            log_run_record(run_log, record_type, message="m")

        events = [v for name, v, _ in control_calls if name == "log_event"]
        assert [e_sev(e) for e in events] == ["ERROR", "WARNING", "INFO"]


def e_sev(event: dict) -> str:
    """The severity a persisted record was recorded under."""
    return event["severity"]

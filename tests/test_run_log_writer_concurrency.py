"""What the run-log sequence actually guarantees, before an owner is built for it.

The record sequence — ``record_id``, ``parent_record_id``, ``nest_level`` — is
held in a JSON state file beside the run log, read and written per record by
``record_parenting`` and ``nest_level``. Before that mechanism moves inside a
``RunLog`` object, what it currently guarantees has to be established rather
than assumed. An owner built around an unproven sequencing model would preserve
whatever is wrong with it and make it look designed.

Two properties, and they do not both hold.

**Continuity across writers holds.** A second writer on the same run log
continues the sequence rather than restarting it. That is deliberate: pipeline
steps are separate OS processes sharing one run log, and ``run_state.load``
finds and continues the existing file. Any owner must keep this.

**Concurrent allocation does not hold.** The read-modify-write cycle has no
lock and no compare-and-swap, so two writers both read ``last_record_id`` and
both claim the next value. ``pipeline_coordinator`` runs parallel step groups in
a ThreadPoolExecutor sharing one context, which is exactly this shape, and the
coordinator already names the consequence in a comment as a limitation "left
unchanged".

The second test is xfail rather than deleted or inverted. Inverting it would
assert the defect as intended behaviour; deleting it would lose the record. As
xfail it reports the gap now and turns into XPASS the moment sequence
allocation becomes atomic.

No configured pipeline sets ``parallel: true`` today, so this is latent rather
than live.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.logs import log_run_record, log_run_start
from rey_lib.run.identity import establish_run_identity


def _ctx(tmp_path: Path, run_id: str | None = None) -> SimpleNamespace:
    """A writer on the run log in ``tmp_path``.

    Passing ``run_id`` makes a second writer join the same run, which is what a
    pipeline subprocess does when it receives the parent's context.
    """
    ctx = SimpleNamespace(
        log_file=str(tmp_path / "app.run.jsonl"),
        owner_app_name="probe",
        app_name="probe",
    )
    establish_run_identity(ctx)
    ctx.run_timestamp = "20260822_000000"
    if run_id:
        ctx.run_id = run_id
    return ctx


def _rows(ctx: Any) -> list[dict]:
    """Every record on disk for this run."""
    return [json.loads(line)
            for line in Path(ctx.run_log_path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


class TestContinuityAcrossWriters:
    """Holds today, and the owner must keep it."""

    def test_a_second_writer_continues_the_sequence(self, tmp_path: Path) -> None:
        first = _ctx(tmp_path)
        log_run_start(first, operation="first")
        for i in range(3):
            log_run_record(first, "ROW_COUNT", count_name=f"a{i}", count=i)

        second = _ctx(tmp_path, run_id=first.run_id)
        continued = [log_run_record(second, "ROW_COUNT", count_name=f"b{i}", count=i)
                     for i in range(3)]

        # Continues from where the first writer stopped rather than restarting.
        assert continued == [5, 6, 7]

    def test_the_sequence_is_unbroken_on_disk(self, tmp_path: Path) -> None:
        first = _ctx(tmp_path)
        log_run_start(first, operation="first")
        log_run_record(first, "ROW_COUNT", count_name="a", count=1)

        second = _ctx(tmp_path, run_id=first.run_id)
        log_run_record(second, "ROW_COUNT", count_name="b", count=2)

        assert [r["record_id"] for r in _rows(first)] == [1, 2, 3]


class TestConcurrentAllocation:
    """Does not hold today. The reason the sequencing model was not assumed."""

    @pytest.mark.xfail(
        reason="Sequence allocation is an unsynchronised read-modify-write on the "
               "run-state file: concurrent writers both read last_record_id and "
               "both claim the next value. Reachable through a parallel pipeline "
               "step group, which shares one context across threads. Latent today "
               "because no configured pipeline sets parallel: true. Becomes XPASS "
               "when allocation is made atomic inside the run-log owner.",
        strict=False,
    )
    def test_concurrent_writers_get_distinct_record_ids(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        log_run_start(run_log, operation="parallel")

        claimed: list[int | None] = []
        guard = threading.Lock()

        def write(worker: int) -> None:
            for i in range(8):
                record_id = log_run_record(run_log, "ROW_COUNT", count_name=f"t{worker}-{i}", count=i)
                with guard:
                    claimed.append(record_id)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(claimed) == 32
        assert len(set(claimed)) == 32, (
            f"{32 - len(set(claimed))} record ids were claimed more than once")

    def test_the_shape_of_the_gap_is_recorded(self, tmp_path: Path) -> None:
        """Not a fix and not a pass — a measurement, so regression is visible.

        Four threads writing eight records each currently produce far fewer
        distinct ids than records. The exact number varies with scheduling; what
        is stable is that it is short, and that every record still reaches the
        log. Losing rows would be a different and worse defect than reusing ids.
        """
        ctx = _ctx(tmp_path)
        log_run_start(run_log, operation="parallel")

        def write(worker: int) -> None:
            for i in range(8):
                log_run_record(run_log, "ROW_COUNT", count_name=f"t{worker}-{i}", count=i)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = _rows(ctx)
        # Every record is written; only their identity collides.
        assert len(rows) == 33
        assert len({r["record_id"] for r in rows}) < len(rows)

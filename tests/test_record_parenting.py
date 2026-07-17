"""Tests for logical record identity through the writer stamp/commit hooks
(SGC_Rey_Log_Record_Parenting_Phase_2).

Scope is deliberately narrow: the id-sequencing mechanics of ``stamp_record`` and
``commit_record``, which sit below the hierarchy contract and are not covered
elsewhere. Parent resolution, stable level anchors, semantic bases, the relative
nesting floor, and the execution-shape scenarios are owned by the authoritative
hierarchy suites — test_nesting_contract, test_hierarchy_delayed_descent, and
test_hierarchy_parenting — and are not restated here.
"""

from __future__ import annotations

from typing import Any

from rey_lib.config.config_utils import Namespace
from rey_lib.logs import set_nest_level
from rey_lib.logs import record_parenting as rp


def _ctx() -> Namespace:
    """Build a bare context; hierarchy state falls back to the in-memory store."""
    return Namespace({})


def _write(ctx: Any, nest_level: int, *, fail: bool = False) -> dict[str, Any]:
    """Stamp a record; commit only on a successful append (fail=False)."""
    record: dict[str, Any] = {}
    record_id = rp.stamp_record(ctx, record, nest_level)
    if not fail:
        rp.commit_record(ctx, record_id, nest_level)
    return record


# TEST-002 / AC-005
def test_failed_append_does_not_advance_last_record_id() -> None:
    """A stamped but uncommitted record leaves the sequence unadvanced."""
    ctx = _ctx()
    set_nest_level(ctx, "app")
    assert _write(ctx, 3)["record_id"] == 1
    assert _write(ctx, 3, fail=True)["record_id"] == 2   # stamped but not committed
    # The next successful write reuses id 2 — no skip.
    assert _write(ctx, 3)["record_id"] == 2


# TEST-010 / AC-003
def test_record_id_is_logical_not_physical_line() -> None:
    """record_id comes from writer state; failed writes leave physical gaps but ids do not skip."""
    ctx = _ctx()
    set_nest_level(ctx, "app")
    assert _write(ctx, 3)["record_id"] == 1
    _write(ctx, 3, fail=True)                    # would-be physical line, not written
    _write(ctx, 3, fail=True)                    # another
    assert _write(ctx, 3)["record_id"] == 2      # still logical 2, independent of lines

"""
Whether a migration adds up, computed rather than asserted.

The failure this exists to remove is an implementer typing COMPLETE. So what is
proved here is that the verdict follows from the record's contents alone: an
unaccounted capability, an evidence reference that points at nothing, a
retirement with no named decision, or a predecessor that is not itself complete
each keep an object out of COMPLETE, and no authored status is ever read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.migration_reconciliation import (
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    STATUS_UNPROVEN,
    EvidenceIndex,
    MigrationRecordError,
    compute_verdicts,
    generate_migration_status,
    load_object_record,
    verify_migration_status_unedited,
)


def _record(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.capabilities.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _evidence(tmp_path: Path) -> EvidenceIndex:
    """A tree holding one real test, so a reference has something to resolve to."""
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    (source / "thing.test.ts").write_text(
        'it("proves the thing", () => {});\n', encoding="utf-8",
    )
    return EvidenceIndex((source,))


COMPLETE_BODY = """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: PRESERVED
  target_owner: thing
  evidence: thing.test.ts > proves the thing
"""


def test_a_record_whose_every_capability_is_accounted_for_is_complete(tmp_path: Path) -> None:
    path = _record(tmp_path, "thing", COMPLETE_BODY)

    verdicts = compute_verdicts([load_object_record(path)], _evidence(tmp_path))

    assert verdicts["thing"].status == STATUS_COMPLETE
    assert verdicts["thing"].reasons == ()


def test_a_missing_capability_makes_it_partial(tmp_path: Path) -> None:
    path = _record(tmp_path, "thing", """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: MISSING
""")

    verdicts = compute_verdicts([load_object_record(path)], _evidence(tmp_path))

    assert verdicts["thing"].status == STATUS_PARTIAL
    assert "MISSING: it does the thing" in verdicts["thing"].reasons


def test_changed_semantics_make_it_partial(tmp_path: Path) -> None:
    path = _record(tmp_path, "thing", """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: SEMANTICS_CHANGED
""")

    assert compute_verdicts([load_object_record(path)], _evidence(tmp_path))["thing"].status \
        == STATUS_PARTIAL


def test_an_unproven_capability_makes_it_unproven(tmp_path: Path) -> None:
    path = _record(tmp_path, "thing", """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: UNPROVEN
""")

    assert compute_verdicts([load_object_record(path)], _evidence(tmp_path))["thing"].status \
        == STATUS_UNPROVEN


def test_evidence_that_points_at_nothing_makes_it_unproven(tmp_path: Path) -> None:
    """The check that caught a real defect on 2026-08-19: a cited source test name."""
    path = _record(tmp_path, "thing", """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: PRESERVED
  target_owner: thing
  evidence: thing.test.ts > a test nobody wrote
""")

    verdict = compute_verdicts([load_object_record(path)], _evidence(tmp_path))["thing"]

    assert verdict.status == STATUS_UNPROVEN
    assert "evidence does not resolve" in verdict.reasons[0]


def test_an_authored_status_is_never_read(tmp_path: Path) -> None:
    """An implementer writing the verdict is the failure this module removes."""
    path = _record(tmp_path, "thing", COMPLETE_BODY + """
computed_status: COMPLETE
""")
    claimed = _record(tmp_path, "other", """
object: other
computed_status: COMPLETE
capabilities:
- source_capability: it does the thing
  disposition: MISSING
""")

    verdicts = compute_verdicts(
        [load_object_record(path), load_object_record(claimed)], _evidence(tmp_path),
    )

    assert verdicts["thing"].status == STATUS_COMPLETE
    # Authored COMPLETE, computed PARTIAL. The record does not get a say.
    assert verdicts["other"].status == STATUS_PARTIAL


def test_a_retirement_must_name_a_decision(tmp_path: Path) -> None:
    path = _record(tmp_path, "thing", """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: INTENTIONALLY_RETIRED
""")

    with pytest.raises(MigrationRecordError, match="must name the architecture decision"):
        load_object_record(path)


def test_a_named_retirement_is_accepted(tmp_path: Path) -> None:
    path = _record(tmp_path, "thing", """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: INTENTIONALLY_RETIRED
  decision: console_next_panel_model.open_capabilities
""")

    assert compute_verdicts([load_object_record(path)], _evidence(tmp_path))["thing"].status \
        == STATUS_COMPLETE


def test_a_preserved_capability_must_name_evidence_and_an_owner(tmp_path: Path) -> None:
    no_evidence = _record(tmp_path, "a", """
object: a
capabilities:
- source_capability: it does the thing
  disposition: PRESERVED
  target_owner: a
""")
    no_owner = _record(tmp_path, "b", """
object: b
capabilities:
- source_capability: it does the thing
  disposition: PRESERVED
  evidence: thing.test.ts > proves the thing
""")

    with pytest.raises(MigrationRecordError, match="must name evidence"):
        load_object_record(no_evidence)
    with pytest.raises(MigrationRecordError, match="must name the object that answers"):
        load_object_record(no_owner)


def test_a_predecessor_that_is_not_complete_blocks_its_dependent(tmp_path: Path) -> None:
    upstream = _record(tmp_path, "upstream", """
object: upstream
capabilities:
- source_capability: it does the thing
  disposition: MISSING
""")
    downstream = _record(tmp_path, "downstream", """
object: downstream
predecessors: [upstream]
""" + COMPLETE_BODY.split("object: thing")[1])

    verdicts = compute_verdicts(
        [load_object_record(upstream), load_object_record(downstream)], _evidence(tmp_path),
    )

    assert verdicts["downstream"].status == STATUS_BLOCKED
    assert "predecessor upstream is PARTIAL" in verdicts["downstream"].reasons


def test_a_predecessor_nobody_reconciled_blocks_too(tmp_path: Path) -> None:
    """A same-named target file is not a reconciliation, which is the whole finding."""
    path = _record(tmp_path, "downstream", """
object: downstream
predecessors: [never_reconciled]
""" + COMPLETE_BODY.split("object: thing")[1])

    verdict = compute_verdicts([load_object_record(path)], _evidence(tmp_path))["downstream"]

    assert verdict.status == STATUS_BLOCKED
    assert "has no reconciliation record" in verdict.reasons[0]


def test_a_malformed_record_stops_generation_rather_than_being_skipped(tmp_path: Path) -> None:
    _record(tmp_path, "thing", "object: thing\ncapabilities: []\n")

    with pytest.raises(MigrationRecordError, match="declares no capabilities"):
        generate_migration_status(tmp_path, (tmp_path,))


def test_an_unknown_disposition_is_refused(tmp_path: Path) -> None:
    path = _record(tmp_path, "thing", """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: PROBABLY_FINE
""")

    with pytest.raises(MigrationRecordError, match="is not a disposition"):
        load_object_record(path)


def test_one_capability_declared_twice_is_refused(tmp_path: Path) -> None:
    path = _record(tmp_path, "thing", """
object: thing
capabilities:
- source_capability: it does the thing
  disposition: MISSING
- source_capability: It Does The Thing
  disposition: PRESERVED
  target_owner: thing
  evidence: thing.test.ts > proves the thing
""")

    with pytest.raises(MigrationRecordError, match="declared twice"):
        load_object_record(path)


def test_the_generated_artifact_detects_a_hand_edit(tmp_path: Path) -> None:
    _record(tmp_path, "thing", COMPLETE_BODY)
    _evidence(tmp_path)
    out = tmp_path / "status.jsonl"
    generate_migration_status(tmp_path, (tmp_path / "src",), out)

    assert verify_migration_status_unedited(out) == []

    out.write_text(out.read_text().replace('"COMPLETE"', '"COMPLETE "'), encoding="utf-8")
    problems = verify_migration_status_unedited(out)

    assert problems and "edited after it was generated" in problems[0]


def test_the_generator_holds_no_per_object_branch() -> None:
    """
    A special case for one object would report the implementer's intent instead
    of the record's contents, which is the defect this module exists to remove.

    Read as code rather than as text. A docstring may name console_next when it
    explains what a field means; what is banned is an object's name reaching the
    executable part of this module, where it could only be a branch.
    """
    import ast

    package = Path(__file__).resolve().parents[1] / "rey_lib" / "migration_reconciliation"
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # Drop docstrings, so prose is not read as code.
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    node.body = body[1:]
        code = ast.unparse(tree)
        for named in ["panel_view", "PanelView", "splitter", "console_next", "rey_console"]:
            assert named not in code, f"{source.name} names {named} in code"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))

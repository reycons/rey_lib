"""
Tests for the enriched file-operation evidence projection
(SGC_Rey_Lib_File_Operation_Evidence_Backend_Projection).

The files.file_operations projection derives, from typed records only: a stable
operation id, the lineage-resolved current path across chained moves, related log
record refs / source lines, viewer/open/copy capability flags (with capability-gated
actions), execution ownership metadata, and deterministic dedup. Nothing is inferred
from filenames or paths.
"""

from __future__ import annotations

import json
from pathlib import Path

from rey_lib.logs import normalize_artifacts, read_run_log_sections


def _fileop(**fields) -> dict:
    """Return a FILE_OPERATION execution record."""
    return {"record_type": "FILE_OPERATION", "record_group": "execution", **fields}


def _file_ops(records: list[dict], tmp_path: Path) -> list[dict]:
    """Project records through the real run-log reader; return file-operation rows."""
    log = tmp_path / "run.20260710_000000.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return read_run_log_sections(str(log))["sections"]["files"]["file_operations"]["files"]


def test_stable_id_is_deterministic_across_projections(tmp_path) -> None:
    """The same source evidence yields the same id on repeated projections."""
    rec = [_fileop(operation="move", source_path="/a/x.csv", target_path="/b/x.csv",
                   run_id="r1", run_timestamp="20260710_000000", step_name="prep",
                   step_sequence=1)]
    id1 = _file_ops(rec, tmp_path)[0]["id"]
    id2 = _file_ops(rec, tmp_path)[0]["id"]
    assert id1 == id2
    assert id1.startswith("fileop-")


def test_distinct_operations_produce_distinct_ids(tmp_path) -> None:
    """Genuinely distinct operations get distinct ids."""
    ops = _file_ops([
        _fileop(operation="move", source_path="/a/x.csv", target_path="/b/x.csv",
                step_name="s", step_sequence=1),
        _fileop(operation="move", source_path="/a/y.csv", target_path="/b/y.csv",
                step_name="s", step_sequence=2),
    ], tmp_path)
    assert ops[0]["id"] != ops[1]["id"]


def test_chained_moves_resolve_current_path(tmp_path) -> None:
    """A -> B -> C resolves the A->B operation's current_path to C."""
    ops = _file_ops([
        _fileop(operation="move", source_path="/inbox/a.csv", target_path="/proc/a.csv",
                step_name="s", step_sequence=1),
        _fileop(operation="move", source_path="/proc/a.csv", target_path="/done/a.csv",
                step_name="s", step_sequence=2),
    ], tmp_path)
    by_source = {o["source_path"]: o for o in ops}
    assert by_source["/inbox/a.csv"]["current_path"] == "/done/a.csv"
    assert by_source["/proc/a.csv"]["current_path"] == "/done/a.csv"


def test_related_records_and_source_lines_are_preserved(tmp_path) -> None:
    """Related log record ids and 1-based source lines point back to the evidence."""
    ops = _file_ops([
        _fileop(operation="move", source_path="/a/x.csv", target_path="/b/x.csv",  # line 1
                correlation_id="c1", step_name="s"),
        {"record_type": "STEP_END", "correlation_id": "c1"},                        # line 2
    ], tmp_path)
    entry = ops[0]
    assert 1 in entry["related_source_lines"] and 2 in entry["related_source_lines"]
    assert "c1" in entry["related_log_record_ids"]


def test_capabilities_and_gated_actions_require_a_path(tmp_path) -> None:
    """can_open/can_copy_path (and actions) are true only with a usable path."""
    with_path = _file_ops([_fileop(operation="move", source_path="/a/x.csv",
                                    target_path="/b/x.csv", step_name="s")], tmp_path)[0]
    assert with_path["capabilities"] == {"can_open": True, "can_copy_path": True}
    assert set(with_path["actions"]) == {"view", "open_external", "copy_path"}

    no_path = _file_ops([_fileop(operation="noop", step_name="s")], tmp_path)[0]
    assert no_path["capabilities"] == {"can_open": False, "can_copy_path": False}
    assert no_path["actions"] == []


def test_duplicate_evidence_collapses_but_distinct_ops_remain(tmp_path) -> None:
    """Repeated evidence for one operation dedupes; distinct operations are kept."""
    ops = _file_ops([
        _fileop(operation="move", source_path="/a/x.csv", target_path="/b/x.csv",
                step_name="s", step_sequence=1),
        _fileop(operation="move", source_path="/a/x.csv", target_path="/b/x.csv",
                step_name="s", step_sequence=1),   # duplicate evidence
        _fileop(operation="copy", source_path="/a/x.csv", target_path="/c/x.csv",
                step_name="s", step_sequence=1),   # distinct operation
    ], tmp_path)
    assert len(ops) == 2
    assert {o["operation"] for o in ops} == {"move", "copy"}


def test_ownership_metadata_surfaced_when_present(tmp_path) -> None:
    """producing_app / pipeline_name / run_id / step_name / step_sequence are surfaced."""
    entry = _file_ops([_fileop(operation="move", source_path="/a/x.csv",
                               target_path="/b/x.csv", producer="loader",
                               pipeline_name="daily", run_id="r9", run_timestamp="20260710_000000",
                               step_id="step-3", step_name="prepare", step_sequence=3)], tmp_path)[0]
    assert entry["producing_app"] == "loader"
    assert entry["pipeline_name"] == "daily"
    assert entry["run_id"] == "r9"
    assert entry["step_name"] == "prepare"
    assert entry["step_sequence"] == 3


def test_missing_optional_metadata_is_empty_not_fabricated(tmp_path) -> None:
    """Absent ownership fields render empty/None — never inferred from the path."""
    entry = _file_ops([_fileop(operation="move", source_path="/inbox/prepare_step.csv",
                               target_path="/done/prepare_step.csv")], tmp_path)[0]
    assert entry["producing_app"] == ""
    assert entry["pipeline_name"] == ""
    assert entry["step_name"] == ""
    assert entry["step_sequence"] is None


def test_artifact_projection_behaviour_unchanged(tmp_path) -> None:
    """The artifact projection is untouched: created artifacts still normalize."""
    records = [{"record_type": "ARTIFACT_REFERENCE", "event": "created",
                "path": "/a/report.json", "producer": "analyzer"}]
    arts = normalize_artifacts(records)
    assert len(arts) == 1 and arts[0]["producer"] == "analyzer"

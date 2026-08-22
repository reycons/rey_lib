"""Focused tests for the installation-scoped governed file manifest."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import make_run_log, start_test_run

from rey_lib.logs import (
    FileManifestError,
    file_manifest_session,
    file_manifest_write_boundary,
    log_file_manifest_record,
    log_run_record,
    manifest_lock_path,
    manifest_state_path,
    resolve_file_manifest_path,
)


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


def _ctx(manifest_path: Path) -> SimpleNamespace:
    """Return a context whose path resolver yields the given manifest path."""
    return SimpleNamespace(
        paths=SimpleNamespace(resolve=lambda name: manifest_path),
        app_name="rey_lib",
        log_depth=0,
    )


def _manifest(tmp_path: Path) -> Path:
    return tmp_path / "file_manifest.jsonl"


def _record(**overrides: object) -> dict:
    record = {
        "record_type": "source_file_inventory",
        "file": {"path": "/x", "size_bytes": 1},
    }
    record.update(overrides)
    return record


def _rows(manifest_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_manifest_path_comes_from_configuration(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert resolve_file_manifest_path(_ctx(manifest)) == manifest


def test_unconfigured_path_is_rejected() -> None:
    with pytest.raises(FileManifestError):
        resolve_file_manifest_path(SimpleNamespace())


def test_companion_paths_are_deterministic(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert manifest_lock_path(manifest).name == "file_manifest.jsonl.lock"
    assert manifest_state_path(manifest).name == "file_manifest.jsonl.hstate.json"


# ---------------------------------------------------------------------------
# Creation and append
# ---------------------------------------------------------------------------


def test_manifest_is_created_when_absent(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert not manifest.exists()

    log_file_manifest_record(_ctx(manifest), _record())

    assert manifest.exists()
    assert len(_rows(manifest)) == 1


def test_append_preserves_existing_records(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    ctx = _ctx(manifest)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))

    log_file_manifest_record(ctx, _record(file={"path": "/first"}))
    log_file_manifest_record(ctx, _record(file={"path": "/second"}))

    rows = _rows(manifest)
    assert [row["file"]["path"] for row in rows] == ["/first", "/second"]


def test_lock_aware_session_reads_and_appends_without_reacquiring(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    ctx = _ctx(manifest)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_file_manifest_record(ctx, _record(file={"path": "/first"}))

    with file_manifest_session(ctx) as session:
        assert [row["file"]["path"] for row in session.read_records()] == ["/first"]
        record_id = session.append(_record(file={"path": "/second"}))

    assert record_id == 2
    assert [row["file"]["path"] for row in _rows(manifest)] == ["/first", "/second"]


def test_one_object_per_line(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    ctx = _ctx(manifest)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    for index in range(3):
        log_file_manifest_record(ctx, _record(file={"path": f"/f{index}"}))

    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert all(isinstance(json.loads(line), dict) for line in lines)


# ---------------------------------------------------------------------------
# Sequencing
# ---------------------------------------------------------------------------


def test_record_id_is_the_physical_row_number(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    ctx = _ctx(manifest)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    returned = [log_file_manifest_record(ctx, _record(file={"path": f"/f{i}"})) for i in range(5)]

    assert returned == [1, 2, 3, 4, 5]
    rows = _rows(manifest)
    assert [row["record_id"] for row in rows] == [1, 2, 3, 4, 5]


def test_record_id_is_written_into_the_record(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    record_id = log_file_manifest_record(_ctx(manifest), _record())
    assert _rows(manifest)[0]["record_id"] == record_id


def test_state_tracks_last_record_id_and_size(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    ctx = _ctx(manifest)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_file_manifest_record(ctx, _record())
    log_file_manifest_record(ctx, _record())

    state = json.loads(manifest_state_path(manifest).read_text(encoding="utf-8"))
    assert state["last_record_id"] == 2
    assert state["manifest_size_bytes"] == manifest.stat().st_size


def test_state_is_repaired_after_an_interrupted_append(tmp_path: Path) -> None:
    """A writer that appended but died before committing state must not corrupt ids."""
    manifest = _manifest(tmp_path)
    ctx = _ctx(manifest)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_file_manifest_record(ctx, _record())

    # Simulate the interruption: the row is on disk, the state never advanced.
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"record_id": 2, "orphan": True}) + "\n")

    assert log_file_manifest_record(ctx, _record()) == 3
    assert [row["record_id"] for row in _rows(manifest)] == [1, 2, 3]


def test_missing_state_file_is_recovered_from_the_manifest(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    ctx = _ctx(manifest)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_file_manifest_record(ctx, _record())
    log_file_manifest_record(ctx, _record())
    manifest_state_path(manifest).unlink()

    assert log_file_manifest_record(ctx, _record()) == 3


def test_malformed_state_file_is_recovered(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    ctx = _ctx(manifest)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_file_manifest_record(ctx, _record())
    manifest_state_path(manifest).write_text("not json", encoding="utf-8")

    assert log_file_manifest_record(ctx, _record()) == 2


def test_public_write_boundary_preserves_highest_id_after_gap(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    manifest.write_text(
        json.dumps({"record_id": 1, "record_type": "one"})
        + "\n"
        + json.dumps({"record_id": 7, "record_type": "seven"})
        + "\n",
        encoding="utf-8",
    )

    with file_manifest_write_boundary(_ctx(manifest)) as locked_path:
        assert locked_path == manifest

    assert log_file_manifest_record(_ctx(manifest), _record()) == 8


def test_public_write_boundary_rejects_malformed_manifest(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    manifest.write_text("{bad json}\n", encoding="utf-8")

    with pytest.raises(FileManifestError, match="cannot be inspected"):
        with file_manifest_write_boundary(_ctx(manifest)):
            pass


# ---------------------------------------------------------------------------
# Rejected input
# ---------------------------------------------------------------------------


def test_caller_supplied_record_id_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(FileManifestError):
        log_file_manifest_record(_ctx(manifest), _record(record_id=7))


def test_non_object_record_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(FileManifestError):
        log_file_manifest_record(_ctx(manifest), ["not", "a", "mapping"])


def test_a_rejected_record_never_reaches_the_manifest(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(FileManifestError):
        log_file_manifest_record(_ctx(manifest), _record(record_id=1))
    assert not manifest.exists()


def test_unwritable_manifest_directory_is_reported(tmp_path: Path) -> None:
    blocked = tmp_path / "not_a_directory"
    blocked.write_text("", encoding="utf-8")
    with pytest.raises(FileManifestError):
        log_file_manifest_record(_ctx(blocked / "file_manifest.jsonl"), _record())


# ---------------------------------------------------------------------------
# log_run_record durable identity
# ---------------------------------------------------------------------------


def _run_ctx(tmp_path: Path) -> SimpleNamespace:
    """Return a context with a durable run-log directory."""
    run_dir = tmp_path / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(
        run_log_dir=str(run_dir),
        app_name="rey_lib",
        name="rey_lib",
        log_depth=0,
    )
    start_test_run(ctx)
    return ctx


def test_log_run_record_returns_the_committed_record_id(tmp_path: Path) -> None:
    ctx = _run_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    first = log_run_record(run_log, "SOURCE_FILE_INVENTORY", path="/a")
    second = log_run_record(run_log, "SOURCE_FILE_INVENTORY", path="/b")

    assert (first, second) == (1, 2)


def test_returned_record_id_matches_the_run_log_row(tmp_path: Path) -> None:
    ctx = _run_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    record_id = log_run_record(run_log, "SOURCE_FILE_INVENTORY", path="/a")

    rows = [
        json.loads(line)
        for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[record_id - 1]["record_id"] == record_id
    assert rows[record_id - 1]["path"] == "/a"


def test_log_run_record_returns_none_when_it_cannot_append(tmp_path: Path) -> None:
    """The never-raise contract holds: an unusable run log returns None, not an error."""
    from rey_lib.logs.run_log import RunLog

    unusable = RunLog(app="rey_lib", run_id="R1", run_timestamp="20260822_000000")
    assert log_run_record(unusable, "SOURCE_FILE_INVENTORY", path="/a") is None


def test_source_file_inventory_is_grouped_with_file_records(tmp_path: Path) -> None:
    ctx = _run_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_run_record(run_log, "SOURCE_FILE_INVENTORY", path="/a")

    record = json.loads(
        Path(run_log.path()).read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["record_group"] == "files"
    assert record["record_subgroup"] == "input_files"


def test_normal_run_log_writing_is_unaffected(tmp_path: Path) -> None:
    """Existing record types keep their group and still append normally."""
    ctx = _run_ctx(tmp_path)
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_run_record(run_log, "STEP_START", step_name="one")
    log_run_record(run_log, "STEP_END", step_name="one", status="ok")

    rows = [
        json.loads(line)
        for line in Path(run_log.path()).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["record_type"] for row in rows] == ["STEP_START", "STEP_END"]
    assert all(row["record_group"] == "execution" for row in rows)


def test_root_fields_are_written_in_canonical_order(tmp_path: Path) -> None:
    """The writer owns persisted order; a serializer's order does not survive."""
    ctx = _ctx(tmp_path / "file_manifest.jsonl")
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_file_manifest_record(ctx,
        {
            "producer": {"application": "file_operator"},
            "file": {"path": "/x"},
            "status": "success",
            "record_type": "source_file_mutation",
            "action": "create",
            "evidence": {"run_log_file": "r.jsonl", "run_log_record_id": 1},
            "recorded_at": "2026-07-31T00:00:00.000+00:00",
            "file_id": "f1",
        },
    )

    assert list(_rows(ctx.paths.resolve("file_manifest"))[0]) == [
        "record_id",
        "file_id",
        "recorded_at",
        "record_type",
        "action",
        "status",
        "evidence",
        "file",
        "producer",
    ]


def test_a_record_type_omits_the_fields_it_does_not_carry(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path / "file_manifest.jsonl")
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    log_file_manifest_record(ctx,
        {
            "record_type": "source_file_rollback",
            "status": "attempted",
            "rollback": {"original_record_id": 5, "phase": "attempt"},
        },
    )

    assert list(_rows(ctx.paths.resolve("file_manifest"))[0]) == [
        "record_id",
        "record_type",
        "status",
        "rollback",
    ]


def test_an_unknown_root_field_is_refused(tmp_path: Path) -> None:
    """An append-only store must never make an unnameable field permanent."""
    manifest = tmp_path / "file_manifest.jsonl"
    with pytest.raises(FileManifestError, match="unknown root field\\(s\\): mutation_kind"):
        log_file_manifest_record(
            _ctx(manifest),
            {"record_type": "source_file_mutation", "mutation_kind": "excel_to_csv"},
        )
    assert not manifest.exists() or _rows(manifest) == []


def test_a_governed_rewrite_preserves_retained_rows_byte_for_byte(
    tmp_path: Path,
) -> None:
    """Deleting one record must not silently re-encode the rows kept.

    An append-only evidence store whose retained lines change bytes is no
    longer byte-comparable, so every writer renders through one encoder.
    """
    from rey_lib.files.jsonl import read_jsonl_file, render_jsonl_line

    ctx = _ctx(tmp_path / "file_manifest.jsonl")
    run_log = make_run_log(tmp_path, path=getattr(ctx, "run_log_path", None) or getattr(ctx, "log_file", None))
    manifest = ctx.paths.resolve("file_manifest")
    for index in range(3):
        log_file_manifest_record(ctx,
            {
                "record_type": "source_file_inventory",
                "source_name": "feed",
                # Non-ASCII and a null-valued field: the two places encoders
                # most often disagree.
                "file": {"path": f"/data/Ünïcode/{index}.csv"},
            },
        )

    appended = manifest.read_bytes()
    rewritten = "".join(
        render_jsonl_line(item.record) + "\n" for item in read_jsonl_file(manifest)
    ).encode("utf-8")

    assert appended == rewritten


def test_sequencing_never_moves_backwards_after_a_rewrite(tmp_path) -> None:
    """A removed id is never reissued, however much of the manifest goes.

    Manifest ids are stored permanently outside the manifest — the run log
    records file_manifest_record_id, and a compensation record references the
    mutation it compensated. Reissuing an id would silently retarget evidence
    that is already written.
    """
    ctx = _ctx(tmp_path / "file_manifest.jsonl")
    with file_manifest_session(ctx) as session:
        ids = [
            session.append({"record_type": "source_file_inventory", "status": "ok"})
            for _ in range(5)
        ]
        # Remove the highest records, then every remaining record.
        session.remove_records(ids[3:])
        assert session.append(
            {"record_type": "source_file_inventory", "status": "ok"}
        ) > max(ids)
        session.remove_records(
            [record["record_id"] for record in session.read_records()]
        )
        assert session.read_records() == []
        assert session.append(
            {"record_type": "source_file_inventory", "status": "ok"}
        ) > max(ids) + 1

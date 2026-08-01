"""The shared run-to-file-manifest query behind Run History file links.

Selection is by exact evidence.run_log_file and nothing else, paths come from
the producer's recorded field rather than the filesystem, and distinct
lifecycle events stay distinct.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.files import serialize_source_file_mutation
from rey_lib.logs import (
    RunFileRecordsError,
    find_run_file_records,
    register_run_file_record_type,
)


class _Paths:
    def __init__(self, manifest: Path) -> None:
        self._manifest = manifest

    def resolve(self, name: str) -> Path:
        from rey_lib.errors.error_utils import ConfigError

        if name != "file_manifest":
            raise ConfigError(f"Unknown path name: {name!r}")
        return self._manifest


def _ctx(manifest: Path) -> SimpleNamespace:
    return SimpleNamespace(paths=_Paths(manifest))


def _mutation(
    record_id: int,
    *,
    action: str,
    run_log_file: str = "app.run.log",
    status: str = "success",
    source_path: str = "",
    destination_path: str = "",
) -> dict[str, Any]:
    # Built through the real serializer so these fixtures are exactly what a
    # producer writes; the manifest writer owns record_id, so it is added here.
    return {
        "record_id": record_id,
        **serialize_source_file_mutation(
            action=action,
            status=status,
            source_path=source_path,
            destination_path=destination_path,
            run_log_file=run_log_file,
            run_log_record_id=record_id,
            application_name="file_operator",
        ),
    }


def _write(manifest: Path, *records: dict[str, Any]) -> Path:
    manifest.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return manifest


@pytest.fixture()
def manifest(tmp_path: Path) -> Path:
    return tmp_path / "file_manifest.jsonl"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selects_only_records_matching_the_exact_run_log_file(manifest: Path) -> None:
    _write(
        manifest,
        _mutation(1, action="create", destination_path="/data/a.csv"),
        _mutation(2, action="create", destination_path="/data/b.csv",
                  run_log_file="other.run.log"),
    )

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert [item.path for item in found] == ["/data/a.csv"]
    assert found.run_log_file == "app.run.log"
    assert found.manifest_path == str(manifest)


def test_caller_input_is_normalized_to_the_stored_identity(manifest: Path) -> None:
    """Producers store the file name, so a full path addresses the same run."""
    _write(manifest, _mutation(1, action="create", destination_path="/data/a.csv"))
    ctx = _ctx(manifest)

    by_path = find_run_file_records(ctx, "/logs/file_operator/app.run.log")
    by_name = find_run_file_records(ctx, "app.run.log")

    assert by_path.run_log_file == by_name.run_log_file == "app.run.log"
    assert [item.path for item in by_path] == [item.path for item in by_name]


def test_run_logs_sharing_a_file_name_are_one_identity(manifest: Path) -> None:
    """Documented consequence of the stored identity being a file name.

    The manifest records only the file name, so two run logs in different
    directories that share a name cannot be told apart here. Widening this
    would require changing what producers record, not what this query compares.
    """
    _write(manifest, _mutation(1, action="create", destination_path="/data/a.csv"))
    ctx = _ctx(manifest)

    first = find_run_file_records(ctx, "/logs/app_a/app.run.log")
    second = find_run_file_records(ctx, "/logs/app_b/app.run.log")

    assert [item.manifest_record_id for item in first] == [1]
    assert [item.manifest_record_id for item in second] == [1]


@pytest.mark.parametrize(
    "requested",
    ["app.run", "app.run.log.old", "pp.run.log", "APP.RUN.LOG"],
)
def test_identity_comparison_is_exact_not_partial(
    manifest: Path,
    requested: str,
) -> None:
    """Normalization reduces to a file name; the comparison itself stays exact."""
    _write(manifest, _mutation(1, action="create", destination_path="/data/a.csv"))

    assert len(find_run_file_records(_ctx(manifest), requested)) == 0


def test_run_type_never_participates_in_selection(manifest: Path) -> None:
    """Pipeline, workflow, and app names on a record cannot change ownership."""
    foreign = _mutation(1, action="create", destination_path="/data/a.csv",
                        run_log_file="other.run.log")
    foreign["pipeline_name"] = "daily"
    foreign["workflow_name"] = "convert_excel_to_csv"
    _write(manifest, foreign)

    assert len(find_run_file_records(_ctx(manifest), "app.run.log")) == 0


def test_records_without_a_usable_path_produce_no_link(manifest: Path) -> None:
    classification = {
        "record_id": 1,
        "record_type": "source_file_classification",
        "file_id": "f1",
        "evidence": {"run_log_file": "app.run.log", "run_log_record_id": 1},
    }
    empty = _mutation(2, action="create", destination_path="")
    _write(manifest, classification, empty)

    assert len(find_run_file_records(_ctx(manifest), "app.run.log")) == 0


def test_inventory_records_use_their_recorded_path(manifest: Path) -> None:
    _write(
        manifest,
        {
            "record_id": 1,
            "record_type": "source_file_inventory",
            "file_id": "f1",
            "file": {"path": "/data/feed/inbox/book.xls"},
            "evidence": {"run_log_file": "app.run.log", "run_log_record_id": 1},
        },
    )

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert [item.path for item in found] == ["/data/feed/inbox/book.xls"]


def test_classification_records_with_a_governed_path_are_eligible(
    manifest: Path,
) -> None:
    _write(
        manifest,
        {
            "record_id": 1,
            "record_type": "source_file_classification",
            "file_id": "f1",
            "file": {"path": "/data/feed/inbox/book.xls"},
            "evidence": {"run_log_file": "app.run.log", "run_log_record_id": 1},
        },
    )

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert [item.path for item in found] == ["/data/feed/inbox/book.xls"]


# ---------------------------------------------------------------------------
# Path selection per action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "source", "destination", "expected"),
    [
        ("create", "", "/data/out.csv", "/data/out.csv"),
        ("move", "/data/inbox/a.xls", "/data/processing/a.xls",
         "/data/processing/a.xls"),
        ("replace", "/data/old.csv", "/data/new.csv", "/data/new.csv"),
        ("delete", "/data/removed.csv", "", "/data/removed.csv"),
    ],
)
def test_each_action_uses_its_authoritative_path(
    manifest: Path,
    action: str,
    source: str,
    destination: str,
    expected: str,
) -> None:
    _write(
        manifest,
        _mutation(1, action=action, source_path=source, destination_path=destination),
    )

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert [item.path for item in found] == [expected]


def test_failed_mutation_shows_the_attempted_path_and_failed_status(
    manifest: Path,
) -> None:
    _write(
        manifest,
        _mutation(1, action="move", status="failed",
                  source_path="/data/inbox/a.xls",
                  destination_path="/data/processing/a.xls"),
    )

    record = find_run_file_records(_ctx(manifest), "app.run.log").records[0]

    assert record.path == "/data/processing/a.xls"
    assert record.status == "failed"
    assert record.failed is True


def test_path_is_never_taken_from_the_current_filesystem(
    manifest: Path,
    tmp_path: Path,
) -> None:
    """A recorded path stays valid evidence even when nothing exists there."""
    _write(manifest, _mutation(1, action="create",
                               destination_path=str(tmp_path / "never_created.csv")))

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert len(found) == 1
    assert not Path(found.records[0].path).exists()


# ---------------------------------------------------------------------------
# Ordering and deduplication
# ---------------------------------------------------------------------------


def test_records_are_ordered_by_ascending_manifest_record_id(
    manifest: Path,
) -> None:
    _write(
        manifest,
        _mutation(7, action="move", source_path="/p/a.xls",
                  destination_path="/arch/a.xls"),
        _mutation(3, action="move", source_path="/in/a.xls",
                  destination_path="/p/a.xls"),
        _mutation(5, action="create", destination_path="/out/a.csv"),
    )

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert [item.manifest_record_id for item in found] == [3, 5, 7]


def test_distinct_lifecycle_events_for_one_file_remain_distinct(
    manifest: Path,
) -> None:
    """A move/create/move sequence is three events, never collapsed by path."""
    _write(
        manifest,
        _mutation(1, action="move", source_path="/in/a.xls",
                  destination_path="/p/a.xls"),
        _mutation(2, action="create", destination_path="/out/a.csv"),
        _mutation(3, action="move", source_path="/p/a.xls",
                  destination_path="/arch/a.xls"),
    )

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert [(item.action, item.path) for item in found] == [
        ("move", "/p/a.xls"),
        ("create", "/out/a.csv"),
        ("move", "/arch/a.xls"),
    ]


def test_repeated_events_on_the_same_path_are_not_deduplicated(
    manifest: Path,
) -> None:
    _write(
        manifest,
        _mutation(1, action="create", destination_path="/out/a.csv"),
        _mutation(2, action="replace", source_path="/out/a.csv",
                  destination_path="/out/a.csv"),
    )

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert len(found) == 2
    assert {item.path for item in found} == {"/out/a.csv"}


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_missing_manifest_reports_unavailable_evidence(tmp_path: Path) -> None:
    with pytest.raises(RunFileRecordsError, match="does not exist"):
        find_run_file_records(_ctx(tmp_path / "absent.jsonl"), "app.run.log")


def test_malformed_manifest_reports_unavailable_evidence(manifest: Path) -> None:
    manifest.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(RunFileRecordsError, match="unavailable"):
        find_run_file_records(_ctx(manifest), "app.run.log")


def test_empty_run_log_file_is_rejected(manifest: Path) -> None:
    _write(manifest)

    with pytest.raises(RunFileRecordsError, match="non-empty"):
        find_run_file_records(_ctx(manifest), "")


def test_artifact_manifest_records_are_ignored(manifest: Path) -> None:
    """Historical ARTIFACT_MANIFEST rows never feed the new path."""
    _write(
        manifest,
        {
            "record_id": 1,
            "record_type": "ARTIFACT_MANIFEST",
            "artifacts": [{"path": "/data/legacy.csv", "artifact_group": "output_files"}],
            "evidence": {"run_log_file": "app.run.log", "run_log_record_id": 1},
        },
    )

    assert len(find_run_file_records(_ctx(manifest), "app.run.log")) == 0


# ---------------------------------------------------------------------------
# Extension
# ---------------------------------------------------------------------------


def test_a_registered_record_type_joins_run_history(manifest: Path) -> None:
    register_run_file_record_type(
        "governed_export",
        lambda record: str(record.get("export_path") or ""),
        replace=True,
    )
    _write(
        manifest,
        {
            "record_id": 1,
            "record_type": "governed_export",
            "export_path": "/data/export.csv",
            "evidence": {"run_log_file": "app.run.log", "run_log_record_id": 1},
        },
    )

    found = find_run_file_records(_ctx(manifest), "app.run.log")

    assert [item.path for item in found] == ["/data/export.csv"]


def test_registering_a_known_type_without_replace_is_rejected() -> None:
    with pytest.raises(RunFileRecordsError, match="already registered"):
        register_run_file_record_type("source_file_mutation", lambda _record: "")

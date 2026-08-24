"""The shared run-to-file-manifest query behind Run History file links.

Selection is by exact ``evidence.run_log_file`` and nothing else, paths come
from the producer's recorded field rather than the filesystem, and distinct
lifecycle events stay distinct.

Records are seeded through the real writers against a Control-shaped double.

Known gap, deliberately not papered over here: the live database has no read
routine that filters on ``evidence.run_log_file``. These prove the query's
semantics; they are not evidence that the live path serves them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rey_lib.files.manifest import FileManifest
from rey_lib.logs import (
    RunFileRecordsError,
    find_run_file_records,
    register_run_file_record_type,
)

from tests.support.control_double import control_backed_ctx


class _Paths:
    def __init__(self, manifest: Path) -> None:
        self._manifest = manifest

    def resolve(self, name: str) -> Path:
        from rey_lib.errors.error_utils import ConfigError

        if name != "file_manifest":
            raise ConfigError(f"Unknown path name: {name!r}")
        return self._manifest


class _Store:
    """Governed records seeded through the real FileManifest."""

    def __init__(self, manifest: Path) -> None:
        self.ctx = control_backed_ctx(paths=_Paths(manifest))
        self.manifest = FileManifest(self.ctx.shared_control)
        self.path = manifest
        self._files = 0

    def file(self, path: str, *, run_log_file: str = "app.run.log",
             run_log_id: int = 1) -> int:
        """Record a governed file, with the run that discovered it."""
        self._files += 1
        name = f"f{self._files}"
        return self.manifest.inventory(
            path=path, file_name=path.rsplit("/", 1)[-1], base_name=name,
            file_extension=path.rsplit(".", 1)[-1], checksum_sha256=f"sha-{name}",
            size_bytes=1, source_name="feed",
            evidence={"run_log_file": run_log_file,
                      "run_log_id": run_log_id},
        )

    def mutation(self, file_id: int, *, action: str, path: str,
                 status: str = "success", run_log_file: str = "app.run.log",
                 run_log_id: int = 1, **payload: Any) -> int:
        """Append one lifecycle event, carrying the run that performed it."""
        return self.manifest.append_mutation(
            file_id, record_type="source_file_mutation", action=action,
            status=status, path=path, run_log_file=run_log_file,
            run_log_id=run_log_id,
            producer={"application": "file_operator"}, **payload,
        )


@pytest.fixture()
def store(tmp_path: Path) -> _Store:
    return _Store(tmp_path / "file_manifest.jsonl")


def test_selects_only_records_matching_the_exact_run_log_file(store) -> None:
    mine = store.file("/data/a.csv")
    store.mutation(mine, action="create", path="/data/a.csv")
    theirs = store.file("/data/b.csv", run_log_file="other.run.log")
    store.mutation(theirs, action="create", path="/data/b.csv",
                   run_log_file="other.run.log")

    found = find_run_file_records(store.ctx, "app.run.log")

    assert "/data/a.csv" in [item.path for item in found]
    assert "/data/b.csv" not in [item.path for item in found]
    assert found.run_log_file == "app.run.log"


def test_caller_input_is_normalized_to_the_stored_identity(store) -> None:
    """Producers store the file name, so a full path addresses the same run."""
    store.mutation(store.file("/data/a.csv"), action="create",
                   path="/data/a.csv")

    by_path = find_run_file_records(store.ctx, "/logs/file_operator/app.run.log")
    by_name = find_run_file_records(store.ctx, "app.run.log")

    assert by_path.run_log_file == by_name.run_log_file == "app.run.log"
    assert [item.path for item in by_path] == [item.path for item in by_name]


def test_run_logs_sharing_a_file_name_are_one_identity(store) -> None:
    """Documented consequence of the stored identity being a file name.

    The manifest records only the file name, so two run logs in different
    directories that share a name cannot be told apart here. Widening this
    would require changing what producers record, not what this query compares.
    """
    mutation = store.mutation(store.file("/data/a.csv"), action="create",
                              path="/data/a.csv")

    first = find_run_file_records(store.ctx, "/logs/app_a/app.run.log")
    second = find_run_file_records(store.ctx, "/logs/app_b/app.run.log")

    assert mutation in [item.manifest_record_id for item in first]
    assert mutation in [item.manifest_record_id for item in second]


@pytest.mark.parametrize(
    "requested",
    ["app.run", "app.run.log.old", "pp.run.log", "APP.RUN.LOG"],
)
def test_identity_comparison_is_exact_not_partial(store, requested: str) -> None:
    """Normalization reduces to a file name; the comparison stays exact."""
    store.mutation(store.file("/data/a.csv"), action="create",
                   path="/data/a.csv")

    assert len(find_run_file_records(store.ctx, requested)) == 0


def test_run_type_never_participates_in_selection(store) -> None:
    """A run's own identity owns the link; nothing about its type does."""
    foreign = store.file("/data/a.csv", run_log_file="other.run.log")
    store.mutation(foreign, action="create", path="/data/a.csv",
                   run_log_file="other.run.log")

    assert len(find_run_file_records(store.ctx, "app.run.log")) == 0


def test_records_without_a_usable_path_produce_no_link(store) -> None:
    store.mutation(store.file("/data/a.csv"), action="create", path="")

    found = find_run_file_records(store.ctx, "app.run.log")

    assert "" not in [item.path for item in found]


def test_inventory_records_use_their_recorded_path(store) -> None:
    store.file("/data/feed/inbox/book.xls")

    found = find_run_file_records(store.ctx, "app.run.log")

    assert [item.path for item in found] == ["/data/feed/inbox/book.xls"]


@pytest.mark.parametrize("action, recorded, expected", [
    ("create", "/out/a.csv", "/out/a.csv"),
    ("move", "/p/a.xls", "/p/a.xls"),
    ("replace", "/out/a.csv", "/out/a.csv"),
])
def test_each_action_uses_its_authoritative_path(store, action: str,
                                                 recorded: str,
                                                 expected: str) -> None:
    file_id = store.file("/in/a.xls")
    store.mutation(file_id, action=action, path=recorded)

    found = find_run_file_records(store.ctx, "app.run.log")

    assert expected in [item.path for item in found]


def test_failed_mutation_shows_the_attempted_path_and_failed_status(
    store,
) -> None:
    file_id = store.file("/data/inbox/a.xls")
    store.mutation(file_id, action="move", status="failed",
                   path="/data/processing/a.xls")

    record = [item for item in find_run_file_records(store.ctx, "app.run.log")
              if item.action == "move"][0]

    assert record.path == "/data/processing/a.xls"
    assert record.status == "failed"
    assert record.failed is True


def test_path_is_never_taken_from_the_current_filesystem(
    store, tmp_path: Path,
) -> None:
    """A recorded path stays valid evidence even when nothing exists there."""
    never = tmp_path / "never_created.csv"
    store.mutation(store.file("/in/a.xls"), action="create", path=str(never))

    found = find_run_file_records(store.ctx, "app.run.log")

    assert str(never) in [item.path for item in found]
    assert not never.exists()


# ---------------------------------------------------------------------------
# Ordering and deduplication
# ---------------------------------------------------------------------------


def test_records_are_ordered_by_ascending_manifest_record_id(store) -> None:
    file_id = store.file("/in/a.xls")
    first = store.mutation(file_id, action="move", path="/p/a.xls")
    second = store.mutation(file_id, action="create", path="/out/a.csv")
    third = store.mutation(file_id, action="move", path="/arch/a.xls")

    found = find_run_file_records(store.ctx, "app.run.log")
    mutation_ids = [item.manifest_record_id for item in found
                    if item.record_type == "source_file_mutation"]

    assert mutation_ids == [first, second, third]


def test_distinct_lifecycle_events_for_one_file_remain_distinct(store) -> None:
    """A move/create/move sequence is three events, never collapsed by path."""
    file_id = store.file("/in/a.xls")
    store.mutation(file_id, action="move", path="/p/a.xls")
    store.mutation(file_id, action="create", path="/out/a.csv")
    store.mutation(file_id, action="move", path="/arch/a.xls")

    found = find_run_file_records(store.ctx, "app.run.log")
    events = [(item.action, item.path) for item in found
              if item.record_type == "source_file_mutation"]

    assert events == [
        ("move", "/p/a.xls"),
        ("create", "/out/a.csv"),
        ("move", "/arch/a.xls"),
    ]


def test_repeated_events_on_the_same_path_are_not_deduplicated(store) -> None:
    file_id = store.file("/in/a.xls")
    store.mutation(file_id, action="create", path="/out/a.csv")
    store.mutation(file_id, action="replace", path="/out/a.csv")

    found = [item for item in find_run_file_records(store.ctx, "app.run.log")
             if item.path == "/out/a.csv"]

    assert len(found) == 2


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_an_installation_with_no_governed_files_is_empty_not_broken(
    store,
) -> None:
    """There is no manifest file to be missing; an empty store is empty."""
    found = find_run_file_records(store.ctx, "app.run.log")

    assert list(found) == []


def test_empty_run_log_file_is_rejected(store) -> None:
    with pytest.raises(RunFileRecordsError):
        find_run_file_records(store.ctx, "   ")


def test_a_registered_record_type_joins_run_history(store) -> None:
    """A further governed record type is admitted without teaching the query."""
    register_run_file_record_type(
        "source_file_inventory",
        path_resolver=lambda record: (record.get("file") or {}).get("path", ""),
        replace=True,
    )
    store.file("/data/feed/inbox/book.xls")

    found = find_run_file_records(store.ctx, "app.run.log")

    assert "/data/feed/inbox/book.xls" in [item.path for item in found]


def test_registering_a_known_type_without_replace_is_rejected() -> None:
    with pytest.raises(RunFileRecordsError):
        register_run_file_record_type(
            "source_file_mutation",
            path_resolver=lambda record: "",
        )

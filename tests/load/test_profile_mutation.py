"""Profiling writes one governed mutation, and what that mutation carries.

A profile is a mutation. Profiling reads a file as the mutation it consumed left
it, and appends a ``source_file_profile`` record naming that mutation as
``source_record_id`` and carrying the clear and redacted readings in their own
columns. It replaces nothing -- profiling the same file again appends another
complete record.

Copied from the legacy file_operator tests and pointed at the Loader-owned
profiling, rey_lib.load.profile (row 589, step 5). The step tests run
``run_record_type_profiling``; each selected state opens through ManifestSource
as the step's does. The legacy "Profile Source" button test is not copied:
``profile_source_object`` has no workflow caller and is not part of this step.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from rey_lib.logs import log_file_manifest_record
from rey_lib.files.data_file import data_file_for

from rey_lib.load import profile as workflow

from tests.support.selecting_control import SelectingControl


import pytest


def _opened(control: Any, *, file_mutation_id: int) -> SimpleNamespace:
    """ManifestSource opened at exactly the selected row's mutation."""
    row = next(one for one in control.selected
               if one["file_mutation_id"] == file_mutation_id)
    return SimpleNamespace(data_file=lambda: data_file_for(
        Path(row["path"]),
        file_manifest_id=row["file_manifest_id"],
        file_mutation_id=file_mutation_id,
    ))


@pytest.fixture(autouse=True)
def _selected_state_opens_as_a_data_file():
    """The step opens each selected state through ManifestSource."""
    with patch.object(workflow.ManifestSource, "create", side_effect=_opened):
        yield


def _run_log(tmp_path: Path, control: Any) -> Any:
    """A run log that commits, which is what governed evidence requires."""
    from rey_lib.logs.run_log import RunLog

    return RunLog(
        app="file_operator",
        run_id="00000000-0000-4000-8000-000000000001",
        run_timestamp="20260822_000000",
        log_dir=str(tmp_path),
        destination="both",
        control=control,
    )


def _ctx(tmp_path: Path, **extra: Any) -> SimpleNamespace:
    """A run-bound context whose governed manifest is the control double."""
    ctx = SimpleNamespace(
        paths=SimpleNamespace(
            resolve=lambda name: {"file_manifest": tmp_path / "file_manifest.jsonl"}[name]
        ),
        run_log_path=str(tmp_path / "run_log.20260810_163217.jsonl"),
        run_id="r1",
        run_timestamp="20260810_163217",
        owner_app_name="file_operator",
        app_name="rey_loader",
        **extra,
    )
    ctx.shared_control = SelectingControl()
    return ctx


CLASSIFICATION = {
    "type": "file_name_regex",
    "source_field": "file.path",
    "values": {"feed": "bmo", "client_alias": "BMO", "record_type": "Hold"},
    # Which of those values decided the grouping. Recorded by classification,
    # and the only way a later step learns the field NAMES -- the manifest
    # carries the key's value, never what it was built from.
    "key_fields": ["feed", "record_type"],
}


def _governed_file(ctx: Any, source: Path, classification: Any = None) -> int:
    """Record the governed file the profile will belong to, and classify it.

    Classifying is an event in the file's history, not a field on its row:
    ``control.file_manifest`` has no classification column and
    ``Control.inventory_file`` accepts no such argument. So the file is
    recorded, and the classification is appended as its own mutation.
    """
    file_manifest_id = ctx.shared_control.inventory_file(
        path=str(source), file_name=source.name, base_name=source.stem,
        file_extension=source.suffix.removeprefix("."),
        checksum_sha256="abc", size_bytes=source.stat().st_size,
        evidence={"run_log_id": 1},
    )
    ctx.shared_control.append_file_mutation(
        file_manifest_id,
        record_type="source_file_classification",
        action="classify",
        status="success",
        result="classified",
        classification=CLASSIFICATION if classification is None else classification,
    )
    return file_manifest_id


def _consumed_mutation(ctx: Any, file_manifest_id: int, source: Path) -> int:
    """The prior mutation profiling will consume, written by the real writer."""
    return log_file_manifest_record(ctx, {
        "record_type": "source_file_mutation",
        "action": "create",
        "status": "success",
        "file_id": file_manifest_id,
        "evidence": {"run_log_id": 1},
        "file": {"path": str(source)},
        "result": "file_sanitization",
    })


def _select(ctx: Any, file_manifest_id: int, file_mutation_id: int,
            source: Path) -> None:
    """What the step's selector procedure returns: the file and the mutation."""
    ctx.shared_control.selected = [{
        "file_manifest_id": file_manifest_id,
        "file_mutation_id": file_mutation_id,
        "path": str(source),
        "data_profile_key": PROFILE_KEY,
        "key_fields": list(PROFILE_KEY_FIELDS),
    }]


#: The group these tests profile. One key, so a second run of the same file is
#: the same group and must not be profiled again.
PROFILE_KEY = "test_feed|Hold"

#: The field names that key was built from. The selector projects them out of
#: the classification payload, and a file type cannot be established without
#: them -- control.file_type.key_fields is NOT NULL.
PROFILE_KEY_FIELDS = ("feed", "record_type")


def _profiles(ctx: Any) -> list[dict[str, Any]]:
    """Every profiling mutation the run appended, oldest first.

    Still a mutation -- profiling still records that it happened. What it no
    longer carries is the profile itself.
    """
    return [row for row in ctx.shared_control.list_file_mutations()
            if row["record_type"] == "source_file_profile"]


def _profile_row(ctx: Any) -> dict[str, Any]:
    """The profile as persisted: one row of columns, not an object."""
    rows = ctx.shared_control.data_profiles
    assert rows, "no profile was persisted"
    return rows[-1]


def _fields(ctx: Any, representation: str) -> list[dict[str, Any]]:
    """The field rows of one reading, in the order they were written.

    Where a reading lives now: rows carrying a type, not a blob under a key.
    """
    rows = [row for row in ctx.shared_control.data_profile_fields
            if row["data_profile_field_type"] == representation]
    assert rows, f"no {representation} field rows were written"
    return rows


PROFILE_CONFIG = {
    "file_selection": {
        "procedure": "get_files_to_profile",
        "source_field": "path",
    },
}


def _wide_source(path: Path, first: str = "A") -> Path:
    """A file long enough to have an identifiable header."""
    path.write_text(
        "Account,Amount\n"
        + "".join(f"{first}-{index},{index}.00\n" for index in range(1, 40)),
        encoding="utf-8",
    )
    return path


def test_an_existing_schema_5_sidecar_is_ignored_by_workflow_profiling(
    tmp_path: Path,
) -> None:
    """A stale on-disk sidecar is neither read nor rewritten."""
    source = _wide_source(tmp_path / "converted.csv")
    ctx = _ctx(tmp_path)
    run_log = _run_log(tmp_path, ctx.shared_control)
    file_manifest_id = _governed_file(ctx, source)
    consumed = _consumed_mutation(ctx, file_manifest_id, source)
    _select(ctx, file_manifest_id, consumed, source)

    legacy = tmp_path / "profiles" / "converted.profile.json"
    legacy.parent.mkdir(parents=True)
    legacy_text = json.dumps({"schema_version": 5, "profile_version": 10})
    legacy.write_text(legacy_text, encoding="utf-8")
    source_before = source.read_bytes()

    result = workflow.run_record_type_profiling(
        ctx, run_log, PROFILE_CONFIG, apply=True, profiler_version="test",
    )

    assert (result.profiled, result.failures) == (1, ())
    profile = _profiles(ctx)[-1]
    assert profile["source_record_id"] == consumed
    assert profile["file_manifest_id"] == file_manifest_id
    assert profile["action"] == "record_only"
    # control.file_mutation requires these. The double does not enforce NOT
    # NULL, so a record missing them passed here and failed against the real
    # database -- which is exactly what happened when this record type was
    # introduced. Every mutation written through serialize_source_file_mutation
    # gets them for free; this one is built for the boundary directly.
    # The producer is the runtime context's application (row 589, step 5).
    assert profile["producer"] == {
        "application": "rey_loader", "operation": "record_type_profiling",
    }
    assert profile["result"] == "record_type_profile"
    stored = _profile_row(ctx)
    assert stored["header_definition"] == "Account,Amount"
    # The blob is gone: a profile is columns, and nothing carries the
    # profiler's object wholesale.
    assert "structural_profile" not in stored
    assert "profile_metadata" not in stored
    assert legacy.read_text(encoding="utf-8") == legacy_text
    assert source.read_bytes() == source_before


def test_both_representations_are_written_on_the_one_mutation(
    tmp_path: Path,
) -> None:
    """One profiling event, two readings, and the shape each of them carries."""
    source = _wide_source(tmp_path / "converted.csv")
    ctx = _ctx(tmp_path)
    run_log = _run_log(tmp_path, ctx.shared_control)
    file_manifest_id = _governed_file(ctx, source)
    consumed = _consumed_mutation(ctx, file_manifest_id, source)
    _select(ctx, file_manifest_id, consumed, source)

    result = workflow.run_record_type_profiling(
        ctx, run_log, PROFILE_CONFIG, apply=True, profiler_version="test",
    )

    assert (result.profiled, result.failures) == (1, ())
    profile = _profiles(ctx)[-1]
    stored = _profile_row(ctx)
    clear = _fields(ctx, "clear")
    redacted = _fields(ctx, "redacted")

    # The distribution facts belong to the profile; only the field values differ
    # between the readings.
    # The header TEXT, not a wrapper. row_number is validated by the profiler
    # and deliberately not carried: how many preamble lines one delivery had is
    # a fact about the delivery, not the structure.
    assert stored["header_definition"] == "Account,Amount"
    assert "profile_version" not in stored["distribution"]
    assert "header" not in stored["distribution"]
    assert "detected_header" not in stored["distribution"]
    assert "llm_hints" not in stored["distribution"]

    # One row per column per reading, the same columns in the same order.
    assert [row["field_name"] for row in clear] == ["Account", "Amount"]
    assert [row["field_name"] for row in redacted] == [row["field_name"] for row in clear]
    assert [row["ordinal"] for row in clear] == [1, 2]
    assert stored["field_count"] == len(clear)

    # Every field value is a column of its own -- nothing arrives as an object.
    for row in [*clear, *redacted]:
        assert set(row) <= {
            "data_profile_id", "field_name", "data_profile_field_type", "ordinal",
            "detected_type", "blank_count", "min_length", "max_length",
            "min_decimal_places", "max_decimal_places", "min_numeric",
            "max_numeric", "min_date", "max_date", "sample_values",
            "null_like_values", "constant_value", "prepared_name",
        }, sorted(set(row))

    assert all(
        set(entry) == {"value", "count"}
        for row in clear
        for entry in (row.get("sample_values") or [])
    )
    assert not list(tmp_path.rglob("*.profile.json"))


def test_profiling_bypasses_row_redaction_and_preserves_the_source(
    tmp_path: Path,
) -> None:
    """The clear reading holds real values; the redacted one never does."""
    source = tmp_path / "customers.csv"
    source.write_text(
        "customer_name,vendor_name,amount\n"
        + "".join(
            f"ACME,ACME,{index}.00\n" if index % 2 else f"BETA,BETA,{index}.00\n"
            for index in range(1, 40)
        ),
        encoding="utf-8",
    )
    source_bytes = source.read_bytes()
    ctx = _ctx(tmp_path)
    run_log = _run_log(tmp_path, ctx.shared_control)
    file_manifest_id = _governed_file(ctx, source)
    consumed = _consumed_mutation(ctx, file_manifest_id, source)

    with patch(
        "rey_lib.load.layouts.delimited._detect_delimited_masks"
    ) as old_detector:
        workflow._store_profile_library_record(
            ctx, run_log, source, {"max_sample_rows": 12},
            file_manifest_id, consumed, PROFILE_KEY, PROFILE_KEY_FIELDS,
        )

    old_detector.assert_not_called()
    assert source.read_bytes() == source_bytes
    profile = _profiles(ctx)[-1]
    stored = _profile_row(ctx)
    clear = _fields(ctx, "clear")
    redacted = _fields(ctx, "redacted")

    assert "ACME" in json.dumps([row["sample_values"] for row in clear])
    assert "ACME" not in json.dumps([row["sample_values"] for row in redacted])

    # The row count has a column. The rest of the sampling provenance does not,
    # and a value with no column is not persisted.
    assert stored["row_count"] == 39
    assert "requested_sample_rows" not in stored
    assert "sampled_rows" not in stored
    assert "sampling_provenance" not in stored
    assert not (tmp_path / "distribution.json").exists()
    assert not (tmp_path / "distribution.redacted.json").exists()


def test_source_line_number_is_excluded_from_both_representations(
    tmp_path: Path,
) -> None:
    """The sanitizer's own column is not part of what the file is."""
    source = tmp_path / "sanitized.csv"
    source.write_text(
        "source_line_number,Name,Amount\n2,Alice,10.00\n3,Bob,20.00\n",
        encoding="utf-8",
    )
    ctx = _ctx(tmp_path)
    run_log = _run_log(tmp_path, ctx.shared_control)
    file_manifest_id = _governed_file(ctx, source)
    consumed = _consumed_mutation(ctx, file_manifest_id, source)

    workflow._store_profile_library_record(
        ctx, run_log, source, {"max_sample_rows": 500}, file_manifest_id, consumed,
        PROFILE_KEY, PROFILE_KEY_FIELDS,
    )

    profile = _profiles(ctx)[-1]
    assert "source_line_number" not in json.dumps(ctx.shared_control.data_profile_fields)
    assert "source_line_number" not in json.dumps(_profile_row(ctx))
    assert _profile_row(ctx)["header_definition"] == "Name,Amount"
    assert [row["field_name"] for row in _fields(ctx, "clear")] == ["Name", "Amount"]


# ---------------------------------------------------------------------------
# One profile per group
# ---------------------------------------------------------------------------

def _profileable(path: Path) -> Path:
    path.write_text(
        "Name,Amount\n" + "".join(f"Alice,{n}.00\n" for n in range(1, 40)),
        encoding="utf-8",
    )
    return path


def test_two_files_of_one_group_store_one_profile(tmp_path: Path) -> None:
    """The manifest holds no profile reference, so the key is the whole link."""
    ctx = _ctx(tmp_path)
    run_log = _run_log(tmp_path, ctx.shared_control)
    first_source = _profileable(tmp_path / "CustomerMay26.csv")
    second_source = _profileable(tmp_path / "CustomerJun26.csv")
    first = _governed_file(ctx, first_source)
    second = _governed_file(ctx, second_source)

    for file_id, source in ((first, first_source), (second, second_source)):
        workflow._store_profile_library_record(
            ctx, run_log, source, {"max_sample_rows": 7}, file_id, None, PROFILE_KEY,
            PROFILE_KEY_FIELDS,
        )

    assert len(ctx.shared_control.data_profiles) == 1
    assert ctx.shared_control.data_profiles[0]["data_profile_key"] == PROFILE_KEY


def test_no_file_records_a_profile_id(tmp_path: Path) -> None:
    """The column is gone: a file names its group, never a profile."""
    ctx = _ctx(tmp_path)
    run_log = _run_log(tmp_path, ctx.shared_control)
    source = _profileable(tmp_path / "CustomerMay26.csv")
    file_manifest_id = _governed_file(ctx, source)

    workflow._store_profile_library_record(
        ctx, run_log, source, {"max_sample_rows": 7},
        file_manifest_id, None, PROFILE_KEY, PROFILE_KEY_FIELDS,
    )

    row = next(r for r in ctx.shared_control.files
               if r["file_manifest_id"] == file_manifest_id)
    assert "data_profile_id" not in row


def test_a_file_with_no_group_stores_no_profile(tmp_path: Path) -> None:
    """No key means no group, so there is no profile to resolve or create."""
    ctx = _ctx(tmp_path)
    run_log = _run_log(tmp_path, ctx.shared_control)
    source = _profileable(tmp_path / "CustomerMay26.csv")
    file_manifest_id = _governed_file(ctx, source)

    workflow._store_profile_library_record(
        ctx, run_log, source, {"max_sample_rows": 7}, file_manifest_id, None, "",
    )

    assert ctx.shared_control.data_profiles == []

"""Focused tests for the governed create_prepared_files orchestration.

Real CSV sources are written to disk and real profiling mutations are appended
through the governed writer, so range selection, header consumption, and
publication are exercised as one behaviour. Only the manifest selection boundary
and evidence logging are stubbed.

Copied from the legacy file_operator tests and pointed at the Loader-owned
preparation, rey_lib.load.prepare (row 589, step 6). Each selected row opens
through ManifestSource as the step's does; every assertion is kept.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rey_lib.config.config_namespace import Namespace
from rey_lib.encryption import sha256_file
from rey_lib.logs import log_file_manifest_record

from rey_lib.files.data_file import data_file_for
from rey_lib.load import prepare as loader_prepare
from rey_lib.load.prepare import PreparationError, run_create_prepared_files

from tests.support.selecting_control import SelectingControl

_FILE_ID = 1001
_DATA_DATE = "May26"
_MUTATION_ID = 10


class _Paths:
    def __init__(self, data: Path) -> None:
        self.data = data

    def resolve(self, name: str) -> Path:
        return {
            "data": self.data,
            "file_manifest": self.data / "manifest.jsonl",
        }[name]


# One governed manifest per test. tmp_path is unique per test, so keying on it
# gives _profile and _ctx the same control without threading one through every
# call site.
_CONTROLS: dict[Path, SelectingControl] = {}


@pytest.fixture(autouse=True)
def _clear_controls():
    yield
    _CONTROLS.clear()


def _control(tmp_path: Path) -> SelectingControl:
    """The governed manifest this test's profiles and reads share."""
    control = _CONTROLS.get(tmp_path)
    if control is None:
        control = SelectingControl()
        _CONTROLS[tmp_path] = control
    return control


def _ctx(tmp_path: Path) -> SimpleNamespace:
    ctx = SimpleNamespace(
        app_name="rey_loader",
        paths=_Paths(tmp_path),
        pipeline_step_name="create_prepared_files",
        pipeline_step_id="step-6",
        pipeline_run_id="run-1",
    )
    ctx.shared_control = _control(tmp_path)
    return ctx


def _source(tmp_path: Path, lines: list[str], name: str = "CWATranMay26.csv") -> Path:
    path = tmp_path / "sanitized" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _record(path: Path, record_id: int = _MUTATION_ID,
            file_id: str = _FILE_ID) -> dict:
    return {
        "record_id": record_id,
        # What the selector returns: the mutation this step consumes, and the
        # governed file it belongs to.
        "file_mutation_id": record_id,
        "file_manifest_id": file_id,
        "record_type": "source_file_mutation",
        "file_id": file_id,
        "status": "success",
        "action": "create",
        "file": {
            "path": str(path),
            "file_name": path.name,
            "base_name": path.stem,
            "file_extension": path.suffix.removeprefix("."),
        },
        "result": {"reason": "file_sanitization"},
        "classification": {"type": "file_name_regex", "values": {
            "feed": "bny", "client_alias": "CWA",
            "record_type": "Tran", "data_date": _DATA_DATE}},
    }


def _identity(source: Path) -> str:
    """The profile identity for a source: its name without the data date."""
    return source.stem.replace(_DATA_DATE, "", 1)


def _profile(
    tmp_path: Path,
    source: Path,
    *,
    header: str | None = None,
    excludes: list[tuple[str, str]] | None = None,
    patterns: list[tuple[str, str]] | None = None,
    file_id: str = _FILE_ID,
    source_path: str | None = None,
    source_hash: str | None = None,
    dataset_id: str | None = None,
    source_record_id: int = _MUTATION_ID,
) -> int:
    """Append one governed profiling mutation for the consumed record.

    ``header`` is the exact header line; it defaults to the source's first
    line. ``excludes`` are (reason, exact row text) pairs, each becoming one
    anchored signature, in the order given. ``patterns`` are (reason, regex)
    pairs written through unescaped, for rules the profiler derives rather
    than observes; they are declared before ``excludes``.
    """
    lines = source.read_text(encoding="utf-8").splitlines()
    header_line = header if header is not None else lines[0]
    del excludes, patterns, source_path, dataset_id
    header_fields = [
        field for field in header_line.split(",")
        if field.strip().casefold() != "source_line_number"
    ]
    # The distribution facts both readings share. Only the samples differ, and
    # each reading carries the provenance so either can say what was sampled.
    shared = {
        "profile_schema_version": 1,
        "source_hash": source_hash or sha256_file(source),
        "profiler": {"application": "file_operator"},
        "sampling_strategy": "random_without_replacement_v1",
        "requested_sample_rows": 500,
        "sampled_rows": 1,
        "eligible_population_rows": 1,
        "sampling_provenance": {"strategy": "random_without_replacement_v1"},
        # The header TEXT, as the profile stores it now. Prepare parses the
        # ordered columns back out with the source's own delimiter.
        "header_definition": ",".join(header_fields),
        "distribution": {},
        "columns": [{"name": name} for name in header_fields],
    }
    control = _control(tmp_path)
    # Seeding again REPLACES this file's profile. The double refuses to move a
    # stamped file to another type -- which is the routine's own guard -- so a
    # fixture that re-profiles must clear the old one rather than add a second.
    previous = control.file_type_stamps.pop(file_id, None)
    if previous is not None:
        stale = [dp for dp, ft in control.file_types.items() if ft == previous]
        for data_profile_id in stale:
            control.file_types.pop(data_profile_id, None)
        control.data_profiles[:] = [
            row for row in control.data_profiles
            if row["data_profile_id"] not in stale
        ]
        control.data_profile_fields[:] = [
            row for row in control.data_profile_fields
            if row["data_profile_id"] not in stale
        ]
    # THE PROFILE STORE IS WHERE PREPARE READS FROM, so seed it: the profile
    # itself, its file type, the stamp on this file, and one field row per
    # column per representation. insert_data_profile does the first three in
    # one call, exactly as the routine does.
    identity = control.insert_data_profile(
        f"dp-{file_id}",
        len(header_fields),
        shared["header_definition"],
        1,
        file_id,
        source_hash=shared["source_hash"],
        profile_schema_version=1,
        profile_method="file_operator",
        distribution={},
    )
    for representation in ("clear", "redacted"):
        for name in header_fields:
            control.insert_data_profile_field(
                identity["o_data_profile_id"], name, representation,
            )
    ctx = SimpleNamespace(shared_control=control)
    return log_file_manifest_record(ctx, {
        "record_type": "source_file_profile",
        "action": "record_only",
        "status": "success",
        "file_id": file_id,
        "evidence": {"run_log_id": 44},
        "file": {"path": str(source)},
        "lineage": {"source_record_id": source_record_id},
        "clear_profile": {
            **shared, "samples": [{"column": name} for name in header_fields],
        },
        "redacted_profile": {
            **shared, "samples": [{"column": name} for name in header_fields],
        },
    })


def _profile_row(tmp_path: Path, file_mutation_id: int) -> dict:
    """The appended profiling mutation, so a test can state it exactly."""
    return next(
        row for row in _control(tmp_path).list_file_mutations()
        if row["file_mutation_id"] == file_mutation_id
    )


def _config(
    tmp_path: Path,
    *,
    outbox_overwrite: bool = False,
    kickouts_overwrite: bool = False,
    redacted_copy: bool = True,
) -> dict:
    return {
        "file_selection": {
            "procedure": "get_files_to_prepare",
            "source_field": "file.path",
        },
        "preparation": {"headers": {"convert_to": "snake_case"}},
        "outbox": {
            "path": str(tmp_path / "prepared" / "<file.file_name>"),
            "overwrite": outbox_overwrite,
        },
        "kickouts": {
            "path": str(
                tmp_path / "kickouts" / "<file.base_name>.kickouts.jsonl"
            ),
            "overwrite": kickouts_overwrite,
            "redacted_copy": redacted_copy,
        },
    }


def _opened(control: SelectingControl, *, file_mutation_id: int) -> SimpleNamespace:
    """ManifestSource opened at exactly the selected row's mutation."""
    row = next(one for one in control.selected
               if one.get("file_mutation_id") == file_mutation_id)
    return SimpleNamespace(data_file=lambda: data_file_for(
        Path(row["file"]["path"]),
        file_manifest_id=row["file_manifest_id"],
        file_mutation_id=file_mutation_id,
        classification=row.get("classification"),
    ))


@pytest.fixture(autouse=True)
def _selected_state_opens_as_a_data_file():
    """Each selected row opens its own mutation, as the step's ManifestSource does."""
    with patch.object(loader_prepare.ManifestSource, "create", side_effect=_opened):
        yield


@pytest.fixture(autouse=True)
def _mute_evidence():
    with (
        patch("rey_lib.load.prepare.log_input_file_reference"),
        patch("rey_lib.load.prepare.log_artifact_reference"),
        patch("rey_lib.load.prepare.log_governed_source_file_mutation"),
    ):
        yield


def _run(ctx, run_log, config, records, *, apply: bool = True):
    """Declare what the step's selector returns, then run it.

    The step names a routine and is handed rows; nothing about which files need
    preparing is decided here, which is why the rows are simply stated.
    """
    ctx.shared_control.selected = list(records)
    return run_create_prepared_files(ctx, run_log, config, apply=apply)


def _only_failure(result, expected: str):
    """Assert the batch recorded exactly one failed item carrying ``expected``."""
    assert result.failed == 1
    assert result.prepared == 0
    assert result.status == "failed"
    item = next(item for item in result.results if item.status == "failed")
    assert expected in item.reason
    assert item.applied is False
    return item


def _tabular(tmp_path: Path):
    """One ordinary tabular source whose only excluded range is its header."""
    source = _source(tmp_path, [
        "Account Number,Trade Amount,Settle Date",
        "A-1,100,2026-05-01",
        "A-2,200,2026-05-02",
    ])
    profile = _profile(tmp_path, source)
    return source, profile


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


def test_selected_header_is_the_canonical_header_and_never_a_kickout(run_log, tmp_path: Path) -> None:
    source, _ = _tabular(tmp_path)
    ctx = _ctx(tmp_path)
    # The profile is asked for by FILE -- not by the mutation this step consumed
    # -- and in the CLEAR representation only. Redacted is what LLM-facing
    # callers are served; internal work never asks for it.
    with patch.object(
        ctx.shared_control, "file_profile",
        wraps=ctx.shared_control.file_profile,
    ) as lookup:
        result = _run(ctx, run_log, _config(tmp_path), [_record(source)])

    assert [call.args for call in lookup.call_args_list] == [(_FILE_ID, "clear")]
    prepared = (tmp_path / "prepared" / source.name).read_text(encoding="utf-8")
    assert prepared.splitlines() == [
        "account_number,trade_amount,settle_date",
        "A-1,100,2026-05-01",
        "A-2,200,2026-05-02",
    ]
    assert not (tmp_path / "kickouts").exists()
    assert result.results[0].kickout_path is None
    assert result.results[0].excluded_rows == 0


def test_legacy_exclude_metadata_is_ignored(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["preamble", "Col One,Col Two", "1,2"])
    _profile(
        tmp_path, source,
        header="Col One,Col Two",
        excludes=[("pre_header_structural", "preamble")],
    )
    _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    prepared = (tmp_path / "prepared" / source.name).read_text(encoding="utf-8")
    assert prepared.splitlines() == ["col_one,col_two", "preamble", "1,2"]
    assert not (tmp_path / "kickouts").exists()


def test_legacy_excludes_never_filter_prepared_rows(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, [
        "Col One,Col Two",
        "1,2",
        "junk row",
        "3,4",
        "another junk",
    ])
    _profile(
        tmp_path, source,
        excludes=[
            ("structural_row_type", "junk row"),
            ("structural_row_type", "another junk"),
        ],
    )
    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    assert (tmp_path / "prepared" / source.name).read_text(
        encoding="utf-8").splitlines() == [
            "col_one,col_two", "1,2", "junk row", "3,4", "another junk",
        ]
    assert not (tmp_path / "kickouts").exists()
    assert (result.results[0].included_rows, result.results[0].excluded_rows) == (4, 0)


def test_source_column_order_and_values_are_preserved(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["Zeta,Alpha,Mid", "z1,a1,m1"])
    _profile(tmp_path, source)
    _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    assert (tmp_path / "prepared" / source.name).read_text(
        encoding="utf-8").splitlines() == ["zeta,alpha,mid", "z1,a1,m1"]


def test_source_line_number_is_preserved_in_output_but_absent_from_profile(run_log, 
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        ["source_line_number,Name,Amount", "2,Alice,10", "3,Bob,20"],
    )
    profile_id = _profile(tmp_path, source)

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    assert result.status == "success"
    profile = _profile_row(tmp_path, profile_id)
    assert "source_line_number" not in json.dumps(profile["clear_profile"])
    assert "source_line_number" not in json.dumps(profile["redacted_profile"])
    assert (tmp_path / "prepared" / source.name).read_text(
        encoding="utf-8"
    ).splitlines() == [
        "source_line_number,name,amount",
        "2,Alice,10",
        "3,Bob,20",
    ]


def test_output_filenames_come_from_manifest_fields(run_log, tmp_path: Path) -> None:
    source_with_junk = _source(tmp_path, [
        "Col One", "1", "junk",
    ], name="BmoHoldMay26.csv")
    _profile(
        tmp_path, source_with_junk,
        excludes=[("structural_row_type", "junk")],
    )
    result = _run(
        _ctx(tmp_path), run_log, _config(tmp_path), [_record(source_with_junk)]
    )

    assert Path(result.results[0].prepared_path).name == "BmoHoldMay26.csv"
    assert result.results[0].kickout_path is None


def test_headers_are_converted_to_snake_case(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["  Trade Amount (USD) ,Settle-Date", "1,2"])
    _profile(tmp_path, source)
    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    assert (tmp_path / "prepared" / source.name).read_text(
        encoding="utf-8").splitlines()[0] == "trade_amount_usd,settle_date"
    assert dict(result.results[0].header_mapping) == {
        "  Trade Amount (USD) ": "trade_amount_usd",
        "Settle-Date": "settle_date",
    }


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


def test_snake_case_header_collision_fails_closed(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["Trade Amount,trade_amount", "1,2"])
    _profile(tmp_path, source)
    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])
    _only_failure(result, "both normalize to 'trade_amount'")
    assert not (tmp_path / "prepared").exists()


def test_missing_profile_fails_the_item(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["Col One", "1"])
    legacy = tmp_path / "profiles" / f"{_identity(source)}.profile.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"schema_version": 6}), encoding="utf-8")
    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])
    _only_failure(result, "requires a current clear profile")


def test_a_profile_built_from_another_file_still_prepares_this_one(
    run_log, tmp_path: Path,
) -> None:
    """The recorded source hash names one file and does not gate the others.

    A profile is identified by installation, grouping key, field count and
    header -- deliberately not by the file -- so every file of one structure
    shares one profile row, and the hash it records names whichever of them was
    profiled first. Comparing it against the file in hand failed every other
    member of the group by construction: 28 of 30 files, on every rerun,
    regardless of what was on disk.
    """
    source = _source(tmp_path, ["Col One", "1"])
    _profile(tmp_path, source, source_hash="the-hash-of-some-other-file")
    legacy = tmp_path / "profiles" / f"{_identity(source)}.profile.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"schema_version": 6}), encoding="utf-8")

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    assert [item.reason for item in result.results if not item.prepared_path] == []
    assert (tmp_path / "prepared" / source.name).exists()


def test_a_profile_taken_against_another_mutation_still_describes_the_file(
    run_log, tmp_path: Path,
) -> None:
    """A profile is a fact about the file's structure, not about one event."""
    source = _source(tmp_path, ["Col One", "1"])
    # Profiled against a mutation this step did not consume.
    _profile(tmp_path, source, source_record_id=11)

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    # It resolves, because prepare reaches the profile through the FILE:
    # file_manifest.file_type_id -> file_type.data_profile_id -> data_profile.
    # Keyed on the consumed mutation instead, a file that was sanitized a
    # second time lost a profile that still described it perfectly, and every
    # file failed as "profile is missing".
    #
    # The currency question is still asked, and asked directly: the source hash
    # is compared against the file on disk.
    assert result.failed == 0
    assert result.prepared == 1


def test_missing_header_definition_fails(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["Col One", "1"])
    _profile(tmp_path, source)
    # The stored profile is what prepare reads, so that is what loses its
    # header. Breaking the mutation payload would prove nothing now.
    _control(tmp_path).data_profiles[0]["header_definition"] = None

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    _only_failure(result, "header_definition")


def test_schema_5_sidecar_is_ignored_when_current_library_profile_exists(run_log, 
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, ["Col One", "1"])
    _profile(tmp_path, source)
    legacy = tmp_path / "profiles" / f"{_identity(source)}.profile.json"
    legacy.parent.mkdir(parents=True)
    legacy_text = json.dumps({"schema_version": 5, "header_signature": {}})
    legacy.write_text(legacy_text, encoding="utf-8")

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    assert result.status == "success"
    assert Path(result.results[0].prepared_path).is_file()
    assert legacy.read_text(encoding="utf-8") == legacy_text


def test_a_canonical_header_matching_nothing_fails_the_item(run_log, tmp_path: Path) -> None:
    """The approved header must actually appear, or there is no canonical header."""
    source, _ = _tabular(tmp_path)
    _profile(tmp_path, source, header="Not,The,Header")
    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])
    _only_failure(result, "no canonical header")
# ---------------------------------------------------------------------------
# Folder-owned overwrite
# ---------------------------------------------------------------------------


def test_outbox_overwrite_authority_is_honored(run_log, tmp_path: Path) -> None:
    source, _ = _tabular(tmp_path)
    existing = tmp_path / "prepared" / source.name
    existing.parent.mkdir(parents=True)
    existing.write_text("keep me", encoding="utf-8")

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])
    _only_failure(result, "does not authorize replacement")
    assert existing.read_text(encoding="utf-8") == "keep me"

    _run(
        _ctx(tmp_path), run_log,
        _config(tmp_path, outbox_overwrite=True, kickouts_overwrite=True),
        [_record(source)],
    )
    assert existing.read_text(encoding="utf-8") != "keep me"


def test_legacy_kickout_file_is_not_touched(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["Col One", "1", "junk"])
    _profile(tmp_path, source, excludes=[("structural_row_type", "junk")])
    existing = tmp_path / "kickouts" / f"{source.stem}.kickouts.jsonl"
    existing.parent.mkdir(parents=True)
    existing.write_text("keep me", encoding="utf-8")

    result = _run(
        _ctx(tmp_path), run_log,
        _config(tmp_path, outbox_overwrite=True),
        [_record(source)],
    )
    assert result.failed == 0
    assert existing.read_text(encoding="utf-8") == "keep me"


def test_step_level_overwrite_is_rejected(run_log, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["overwrite"] = True
    # A configuration error stops the batch before any record is processed and
    # is never demoted to a per-file failure.
    with pytest.raises(PreparationError, match="step-level 'overwrite' is unsupported"):
        _run(_ctx(tmp_path), run_log, config, [])


def test_folder_declarations_are_read_from_loaded_config_namespaces(run_log, tmp_path: Path) -> None:
    """Loaded YAML supplies Namespace sections, not dicts."""
    source, _ = _tabular(tmp_path)
    config = _config(tmp_path)
    config["outbox"] = Namespace(dict(config["outbox"]))
    config["kickouts"] = Namespace(dict(config["kickouts"]))

    result = _run(_ctx(tmp_path), run_log, config, [_record(source)])

    assert Path(result.results[0].prepared_path).exists()


# ---------------------------------------------------------------------------
# Selection, evidence, and dry run
# ---------------------------------------------------------------------------


def test_only_selected_records_are_prepared_once_each(run_log, tmp_path: Path) -> None:
    source, _ = _tabular(tmp_path)
    record = _record(source)
    superseded = _record(source, record_id=4)

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [superseded, record])

    assert result.selected == 1
    assert result.prepared == 1


def test_evidence_records_prepared_artifact_with_counts_and_header_mapping(run_log, 
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, ["Col One", "1", "junk"])
    _profile(tmp_path, source, excludes=[("structural_row_type", "junk")])
    with (
        patch("rey_lib.load.prepare.log_artifact_reference") as artifact,
        patch(
            "rey_lib.load.prepare.log_governed_source_file_mutation"
        ) as mutation,
        patch("rey_lib.load.prepare.log_input_file_reference") as inputs,
    ):
        _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    assert [call.kwargs["role"] for call in artifact.call_args_list] == [
        "prepared_file",
    ]
    assert artifact.call_args_list[0].kwargs["header_mapping"] == {"Col One": "col_one"}
    assert artifact.call_args_list[0].kwargs["included_row_count"] == 2
    assert artifact.call_args_list[0].kwargs["excluded_row_count"] == 0
    assert artifact.call_args_list[0].kwargs["file_id"] == _FILE_ID
    assert mutation.call_count == 1
    assert {call.kwargs["action"] for call in mutation.call_args_list} == {"create"}
    # The source is the one consumed input file. The profile is a governed
    # record, so it is not referenced as a file that was read.
    assert [call.kwargs["file_role"] for call in inputs.call_args_list] == [
        "prepared_file_input",
    ]


def test_dry_run_plans_outputs_without_writing_or_recording(run_log, tmp_path: Path) -> None:
    source, _ = _tabular(tmp_path)
    with (
        patch("rey_lib.load.prepare.log_artifact_reference") as artifact,
        patch(
            "rey_lib.load.prepare.log_governed_source_file_mutation"
        ) as mutation,
    ):
        result = _run(
            _ctx(tmp_path), run_log, _config(tmp_path), [_record(source)], apply=False
        )

    assert result.selected == 1
    assert result.prepared == 0
    assert result.results[0].included_rows == 2
    assert Path(result.results[0].prepared_path).name == source.name
    assert not (tmp_path / "prepared").exists()
    assert artifact.call_count == 0
    assert mutation.call_count == 0


# ---------------------------------------------------------------------------
# Batch isolation
# ---------------------------------------------------------------------------


def _good(
    tmp_path: Path,
    name: str,
    *,
    file_id: str = _FILE_ID,
    source_record_id: int = 10,
):
    """One preparable file, distinct from every other record in the batch."""
    source = _source(tmp_path, ["Col One", "1"], name=name)
    _profile(
        tmp_path,
        source,
        file_id=file_id,
        source_record_id=source_record_id,
    )
    return source


def test_one_unusable_profile_does_not_stop_the_batch(run_log, tmp_path: Path) -> None:
    """The failing file is reported; every other selected file still publishes."""
    broken = _source(tmp_path, ["Col One", "1"], name="AustinFireHold.csv")
    _profile(tmp_path, broken, header="Not,The,Header", file_id=1003)
    after = _good(
        tmp_path,
        "BmoHold.csv",
        file_id=5001,
        source_record_id=11,
    )

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [
        _record(broken, record_id=10, file_id=1003),
        _record(after, record_id=11, file_id=5001),
    ])

    assert (result.selected, result.prepared, result.failed) == (2, 1, 1)
    assert result.status == "partial_success"

    failure = next(item for item in result.results if item.status == "failed")
    assert failure.source_path == str(broken)
    assert "no canonical header" in failure.reason

    # The failed file publishes nothing; the following file is unaffected.
    assert not (tmp_path / "prepared" / broken.name).exists()
    assert not (tmp_path / "kickouts" / f"{broken.stem}.kickouts.csv").exists()
    assert (tmp_path / "prepared" / after.name).read_text(
        encoding="utf-8").splitlines() == ["col_one", "1"]


def test_a_batch_where_every_file_fails_reports_failed(run_log, tmp_path: Path) -> None:
    first = _source(tmp_path, ["Col One", "1"], name="one.csv")
    second = _source(tmp_path, ["Col One", "1"], name="two.csv")

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [
        _record(first, record_id=10, file_id=1004),
        _record(second, record_id=11, file_id=1005),
    ])

    assert (result.prepared, result.failed) == (0, 2)
    assert result.status == "failed"
    assert all(item.status == "failed" for item in result.results)


def test_a_batch_where_every_file_succeeds_reports_success(run_log, tmp_path: Path) -> None:
    first = _good(tmp_path, "one.csv", file_id=5002)
    second = _good(
        tmp_path,
        "two.csv",
        file_id=5003,
        source_record_id=11,
    )

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [
        _record(first, record_id=10, file_id=5002),
        _record(second, record_id=11, file_id=5003),
    ])

    assert (result.prepared, result.failed) == (2, 0)
    assert result.status == "success"


def test_a_failed_file_records_governed_failure_evidence(
    run_log, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    broken = _source(tmp_path, ["Col One", "1"], name="broken.csv")
    _profile(tmp_path, broken, header="Not,The,Header", file_id=1003)
    with (
        patch(
            "rey_lib.load.prepare.log_governed_source_file_mutation"
        ) as mutation,
        patch("rey_lib.load.prepare.log_artifact_reference") as artifact,
        caplog.at_level("ERROR"),
    ):
        result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [
            _record(broken, record_id=10, file_id=1003),
        ])

    item = _only_failure(result, "")
    assert mutation.call_count == 1
    assert mutation.call_args.kwargs["status"] == "failed"
    # reason is the outcome, not the error: nothing was produced, and status
    # distinguishes a failure from a success.
    assert mutation.call_args.kwargs["reason"] == "prepared_file"
    # Why it failed is the error, carried as the evidence record's message.
    assert mutation.call_args.kwargs["message"] == item.reason
    assert item.reason
    # And the failure is logged once, with the error itself.
    logged = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [type(r.exc_info[1]).__name__ for r in logged] == ["PreparationError"]
    assert artifact.call_count == 0


def test_a_dry_run_failure_writes_no_evidence(run_log, tmp_path: Path) -> None:
    broken = _source(tmp_path, ["Col One", "1"], name="broken.csv")
    _profile(tmp_path, broken, header="Not,The,Header", file_id=1003)
    with patch(
        "rey_lib.load.prepare.log_governed_source_file_mutation"
    ) as mutation:
        result = _run(
            _ctx(tmp_path), run_log, _config(tmp_path),
            [_record(broken, record_id=10, file_id=1003)],
            apply=False,
        )

    assert result.failed == 1
    assert mutation.call_count == 0


# ---------------------------------------------------------------------------
# Redacted sister kickout
# ---------------------------------------------------------------------------


def _with_kickouts(tmp_path: Path):
    """One source whose excluded rows carry real values worth redacting."""
    source = _source(tmp_path, [
        "Account Number,Amount",
        "A-1,100.00",
        "12345,100.00",
        "67890,250.50",
    ])
    _profile(
        tmp_path, source,
        excludes=[
            ("structural_row_type", "12345,100.00"),
            ("structural_row_type", "67890,250.50"),
        ],
    )
    return source


def test_legacy_excludes_create_no_kickout_artifacts(run_log, 
    tmp_path: Path,
) -> None:
    source = _with_kickouts(tmp_path)

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])
    assert result.results[0].kickout_path is None
    assert result.results[0].kickout_redacted_path is None
    assert not (tmp_path / "kickouts").exists()


def test_legacy_excluded_values_remain_prepared_data(run_log, tmp_path: Path) -> None:
    source = _with_kickouts(tmp_path)

    _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])
    prepared = (tmp_path / "prepared" / source.name).read_text(encoding="utf-8")
    assert "12345,100.00" in prepared
    assert "67890,250.50" in prepared


def test_legacy_kickout_paths_are_not_materialized(run_log, tmp_path: Path) -> None:
    source = _with_kickouts(tmp_path)

    _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])
    assert not (tmp_path / "kickouts").exists()


def test_no_llm_is_reachable_from_the_orchestrator(tmp_path: Path) -> None:
    """Redaction is deterministic; nothing here may call a model."""
    module = Path(loader_prepare.__file__)
    text = module.read_text(encoding="utf-8")

    for forbidden in ("llm", "anthropic", "openai", "analyzer", "prompt"):
        assert forbidden not in text.lower()


def test_no_kickouts_are_created_without_non_header_exclusions(run_log, tmp_path: Path) -> None:
    source, _ = _tabular(tmp_path)

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    assert result.results[0].kickout_path is None
    assert result.results[0].kickout_redacted_path is None
    assert not (tmp_path / "kickouts").exists()


def test_existing_legacy_redacted_kickout_is_untouched(run_log, tmp_path: Path) -> None:
    source = _with_kickouts(tmp_path)
    existing = tmp_path / "kickouts" / f"{source.stem}.kickouts.redacted.jsonl"
    existing.parent.mkdir(parents=True)
    existing.write_text("keep me", encoding="utf-8")

    result = _run(
        _ctx(tmp_path), run_log, _config(tmp_path, outbox_overwrite=True), [_record(source)]
    )
    assert result.failed == 0
    assert existing.read_text(encoding="utf-8") == "keep me"

    _run(
        _ctx(tmp_path), run_log,
        _config(tmp_path, outbox_overwrite=True, kickouts_overwrite=True),
        [_record(source)],
    )
    assert existing.read_text(encoding="utf-8") == "keep me"


def test_existing_legacy_kickout_does_not_block_prepared_publication(run_log, 
    tmp_path: Path,
) -> None:
    source = _with_kickouts(tmp_path)
    blocked = tmp_path / "kickouts" / f"{source.stem}.kickouts.jsonl"
    blocked.parent.mkdir(parents=True)
    blocked.write_text("keep me", encoding="utf-8")

    result = _run(
        _ctx(tmp_path), run_log, _config(tmp_path, outbox_overwrite=True), [_record(source)]
    )

    assert result.failed == 0
    assert blocked.read_text(encoding="utf-8") == "keep me"
    assert (tmp_path / "prepared" / source.name).exists()
    assert not (
        tmp_path / "kickouts" / f"{source.stem}.kickouts.redacted.jsonl"
    ).exists()


# ---------------------------------------------------------------------------
# Folder-owned redacted_copy
# ---------------------------------------------------------------------------


def test_redacted_copy_defaults_to_false_and_publishes_no_sister(run_log, 
    tmp_path: Path,
) -> None:
    """Existing outputs without the flag keep their current behaviour."""
    source = _with_kickouts(tmp_path)
    config = _config(tmp_path)
    del config["kickouts"]["redacted_copy"]

    result = _run(_ctx(tmp_path), run_log, config, [_record(source)])

    assert result.results[0].kickout_redacted_path is None
    assert not (tmp_path / "kickouts" / f"{source.stem}.kickouts.jsonl").exists()
    assert not (
        tmp_path / "kickouts" / f"{source.stem}.kickouts.redacted.jsonl"
    ).exists()
    assert Path(result.results[0].prepared_path).exists()
    assert not (tmp_path / "prepared" / f"{source.stem}.redacted.csv").exists()


def test_legacy_kickout_sister_is_never_replaced(run_log, tmp_path: Path) -> None:
    source = _with_kickouts(tmp_path)
    sister = tmp_path / "kickouts" / f"{source.stem}.kickouts.redacted.jsonl"
    sister.parent.mkdir(parents=True)
    sister.write_text("keep me", encoding="utf-8")

    result = _run(
        _ctx(tmp_path), run_log, _config(tmp_path, outbox_overwrite=True), [_record(source)]
    )
    assert result.failed == 0
    assert sister.read_text(encoding="utf-8") == "keep me"

    _run(
        _ctx(tmp_path), run_log,
        _config(tmp_path, outbox_overwrite=True, kickouts_overwrite=True),
        [_record(source)],
    )
    assert sister.read_text(encoding="utf-8") == "keep me"


def test_an_unknown_folder_key_is_rejected(run_log, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["kickouts"]["redacted_path"] = "/nope"
    with pytest.raises(PreparationError, match="unknown fields: redacted_path"):
        _run(_ctx(tmp_path), run_log, config, [])


def test_redacted_copy_must_be_boolean(run_log, tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["kickouts"]["redacted_copy"] = "yes"
    with pytest.raises(PreparationError, match="'redacted_copy' must be true or false"):
        _run(_ctx(tmp_path), run_log, config, [])


def test_namespace_backed_folders_support_redacted_copy(run_log, tmp_path: Path) -> None:
    """Loaded YAML supplies Namespace sections, not dicts."""
    source = _with_kickouts(tmp_path)
    config = _config(tmp_path)
    config["kickouts"] = Namespace(dict(config["kickouts"]))
    config["outbox"] = Namespace(dict(config["outbox"]))

    result = _run(_ctx(tmp_path), run_log, config, [_record(source)])

    assert result.results[0].kickout_redacted_path is None
    assert Path(result.results[0].prepared_path).exists()


def test_the_prepared_outbox_may_opt_in_and_its_sister_is_redacted(run_log, 
    tmp_path: Path,
) -> None:
    """Any folder may opt in; the sister is a redaction, not a copy."""
    source = _with_kickouts(tmp_path)
    config = _config(tmp_path)
    config["outbox"]["redacted_copy"] = True

    _run(_ctx(tmp_path), run_log, config, [_record(source)])

    original = (tmp_path / "prepared" / source.name).read_text(
        encoding="utf-8").splitlines()
    sister = (tmp_path / "prepared" / f"{source.stem}.redacted.csv").read_text(
        encoding="utf-8").splitlines()

    assert sister[0] == original[0]                 # header preserved
    assert len(sister) == len(original)             # row count preserved
    for source_line, sister_line in zip(original[1:], sister[1:]):
        assert len(source_line.split(",")) == len(sister_line.split(","))
    assert sister[1:] != original[1:]               # values did differ


def test_blank_rows_are_not_filtered_by_legacy_profile_metadata(run_log, 
    tmp_path: Path,
) -> None:
    """Legacy blank signatures no longer filter prepared rows."""
    source = _source(tmp_path, [
        ",,",
        "Account Number,Trade Amount,Settle Date",
        "A-1,100,2026-05-01",
        " , , ",
        "A-2,200,2026-05-02",
        ",,,,,",
        "A-3,300,2026-05-03",
    ])
    _profile(
        tmp_path, source,
        header="Account Number,Trade Amount,Settle Date",
        patterns=[("blank_structural", "^$")],
    )

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    prepared = (tmp_path / "prepared" / source.name).read_text(encoding="utf-8")
    assert prepared.splitlines()[0] == "account_number,trade_amount,settle_date"
    assert ",," in prepared
    assert "A-1,100,2026-05-01" in prepared
    assert "A-3,300,2026-05-03" in prepared
    assert (result.results[0].included_rows, result.results[0].excluded_rows) == (6, 0)
    assert not (tmp_path / "kickouts").exists()


def test_repeated_header_is_removed_and_never_reaches_kickouts(run_log, 
    tmp_path: Path,
) -> None:
    """The header appears once in the output and nowhere in the evidence.

    A repeat is not an excluded row: it is the same header the consumer
    already writes, so it is removed by canonical column identity before any
    data handling and is never kicked out.
    """
    source = _source(tmp_path, [
        "Account Number,Trade Amount,Settle Date",
        "A-1,100,2026-05-01",
        # The same header, written three different ways.
        "Account Number, Trade Amount, Settle Date",
        "A-2,200,2026-05-02",
        "Account Number,Trade Amount,Settle Date,",
        "A-3,300,2026-05-03",
        '"Account Number","Trade Amount","Settle Date"',
        "A-4,400,2026-05-04",
    ])
    _profile(tmp_path, source)

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    # Written once from the canonical header columns, and every repeat is gone.
    assert (tmp_path / "prepared" / source.name).read_text(
        encoding="utf-8").splitlines() == [
        "account_number,trade_amount,settle_date",
        "A-1,100,2026-05-01",
        "A-2,200,2026-05-02",
        "A-3,300,2026-05-03",
        "A-4,400,2026-05-04",
    ]
    assert not (tmp_path / "kickouts").exists()
    assert result.results[0].kickout_path is None
    assert (result.results[0].included_rows, result.results[0].excluded_rows) == (4, 0)


def test_the_prepared_file_gets_a_redacted_companion(run_log, tmp_path: Path) -> None:
    """A prepared file can be reviewed without its contents.

    The companion carries the same header, the same rows and the same columns;
    only the values are replaced. It is recorded as its own governed artifact,
    or it exists on disk and in no lifecycle.
    """
    source = _source(tmp_path, [
        "Account Number,Trade Amount,Settle Date",
        "A-1,100,2026-05-01",
        "A-2,200,2026-05-02",
    ])
    _profile(tmp_path, source)
    config = _config(tmp_path)
    config["outbox"]["redacted_copy"] = True

    with patch(
        "rey_lib.load.prepare.log_governed_source_file_mutation",
        return_value=1,
    ) as logged:
        result = _run(_ctx(tmp_path), run_log, config, [_record(source)])

    prepared = tmp_path / "prepared" / source.name
    companion = prepared.with_name(f"{prepared.stem}.redacted{prepared.suffix}")
    assert companion.is_file()
    assert result.results[0].prepared_redacted_path == str(companion)

    original = prepared.read_text(encoding="utf-8").splitlines()
    redacted = companion.read_text(encoding="utf-8").splitlines()
    # Same shape, same header, different values.
    assert len(redacted) == len(original)
    assert redacted[0] == original[0]
    assert [len(line.split(",")) for line in redacted] == [
        len(line.split(",")) for line in original
    ]
    assert redacted[1] != original[1]
    assert "A-1" not in companion.read_text(encoding="utf-8")

    reasons = [call.kwargs["reason"] for call in logged.call_args_list]
    assert "redacted_prepared_file" in reasons


def test_no_redacted_companion_when_the_folder_does_not_ask_for_one(run_log, 
    tmp_path: Path,
) -> None:
    """Redaction of the prepared file is the folder's decision, as elsewhere."""
    source, _ = _tabular(tmp_path)

    result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    prepared = tmp_path / "prepared" / source.name
    companion = prepared.with_name(f"{prepared.stem}.redacted{prepared.suffix}")
    assert not companion.exists()
    assert result.results[0].prepared_redacted_path is None


# ---------------------------------------------------------------------------
# What the Loader boundary added (row 589, step 6)
# ---------------------------------------------------------------------------

from rey_lib.errors.error_utils import ConfigError  # noqa: E402
from rey_lib.files import file_routing  # noqa: E402
from rey_lib.files.manifest import FileManifest  # noqa: E402
from rey_lib.load import Transform  # noqa: E402
from rey_lib.load.prepare import PrepareTransform  # noqa: E402

_FILE_KICKOUTS = {"path": "<base_path>/work/kickouts/<file_name>", "overwrite": True}


def _preparer(tmp_path: Path, record: dict) -> PrepareTransform:
    config = loader_prepare._resolve_config(_config(tmp_path))
    return Transform(values={"config": config, "record": record},
                     selected="prepare").resolve(_ctx(tmp_path))


def _selected(record: dict):
    return data_file_for(Path(record["file"]["path"]),
                         file_manifest_id=record["file_manifest_id"],
                         file_mutation_id=record["file_mutation_id"],
                         classification=record["classification"],
                         base_path="/data/bny")


def test_prepare_is_resolved_through_the_one_resolver(tmp_path: Path) -> None:
    source, _ = _tabular(tmp_path)

    assert isinstance(_preparer(tmp_path, _record(source)), PrepareTransform)
    assert "prepare" not in Transform.kinds()


def test_an_applied_prepare_returns_each_artifact_at_its_m17_mutation(
    run_log, tmp_path: Path,
) -> None:
    source, _ = _tabular(tmp_path)
    record = _record(source)
    ids = iter((501, 502))

    with patch("rey_lib.load.prepare.log_governed_source_file_mutation",
               side_effect=lambda *_a, **_k: next(ids)):
        produced = _preparer(tmp_path, record).apply(_selected(record))

    prepared = produced[0]
    assert prepared.path == tmp_path / "prepared" / source.name
    assert (prepared.file_manifest_id, prepared.file_mutation_id) == (_FILE_ID, 501)
    assert prepared.base_path == "/data/bny"


def test_a_plan_publishes_nothing_and_returns_no_identity(run_log, tmp_path: Path) -> None:
    source, _ = _tabular(tmp_path)
    record = _record(source)

    with patch("rey_lib.load.prepare.publish_file_set") as publish, \
         patch("rey_lib.load.prepare.log_governed_source_file_mutation") as evidence:
        result = _preparer(tmp_path, record).plan(_selected(record))

    assert result.applied is False
    publish.assert_not_called()
    evidence.assert_not_called()


def _original(tmp_path: Path) -> Path:
    original = tmp_path / "bny" / "source" / "processing" / "CWATranMay26.csv"
    original.parent.mkdir(parents=True)
    original.write_text("anything\n", encoding="utf-8")
    return original


def _opening(original: Path, tmp_path: Path):
    """The step opens the sanitized row; the kickout opens the original (3)."""
    def create(control, *, file_mutation_id: int):
        if file_mutation_id == 3:
            return SimpleNamespace(
                data_file=lambda: data_file_for(
                    original, file_manifest_id=_FILE_ID, file_mutation_id=3,
                    classification={"type": "t", "values": {}},
                    base_path=str(tmp_path / "bny")),
                governed_context=lambda: {"file_name": original.name},
            )
        return _opened(control, file_mutation_id=file_mutation_id)
    return create


def _history(*moves: tuple[int, str]) -> list[dict]:
    return [{"file_mutation_id": mutation, "action": "move", "status": "success",
             "result": result, "deleted_in": None} for mutation, result in moves]


def test_a_file_that_cannot_be_prepared_is_kicked_out_then_recorded(
    run_log, tmp_path: Path,
) -> None:
    """Rule 75: the ORIGINAL leaves processing first; then the M18 failure."""
    source = _source(tmp_path, ["Col One", "1"])  # no profile: cannot be prepared
    original = _original(tmp_path)
    config = {**_config(tmp_path), "file_kickouts": _FILE_KICKOUTS}
    order: list[str] = []

    def moved(*_a, **_k):
        order.append("move")
        return tmp_path / "bny" / "work" / "kickouts" / original.name

    def recorded(*_a, **kwargs):
        order.append(f"M18:{kwargs.get('status')}")
        return 77

    with patch.object(FileManifest, "history", return_value=_history((3, "moved_to_processing"))), \
         patch.object(loader_prepare.ManifestSource, "create", side_effect=_opening(original, tmp_path)), \
         patch.object(file_routing, "move_file", side_effect=moved) as move, \
         patch.object(file_routing, "log_source_file_mutation", return_value=90) as mutation, \
         patch("rey_lib.load.prepare.log_governed_source_file_mutation", side_effect=recorded):
        result = _run(_ctx(tmp_path), run_log, config, [_record(source)])

    _only_failure(result, "requires a current clear profile")
    assert move.call_args.args[:2] == (original, tmp_path / "bny" / "work" / "kickouts")
    # The step's own operation: the prepare selector's `done` stops offering it.
    assert mutation.call_args.kwargs["operation"] == "create_prepared_files"
    assert mutation.call_args.kwargs["reason"] == "moved_to_kickouts"
    assert source.exists()  # the sanitized copy is never moved
    assert order == ["move", "M18:failed"]


def test_an_original_no_longer_in_processing_is_not_moved(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["Col One", "1"])
    original = _original(tmp_path)
    config = {**_config(tmp_path), "file_kickouts": _FILE_KICKOUTS}

    with patch.object(FileManifest, "history",
                      return_value=_history((3, "moved_to_processing"), (4, "moved_to_archive"))), \
         patch.object(loader_prepare.ManifestSource, "create", side_effect=_opening(original, tmp_path)), \
         patch.object(file_routing, "move_file") as move:
        result = _run(_ctx(tmp_path), run_log, config, [_record(source)])

    move.assert_not_called()
    _only_failure(result, "requires a current clear profile")


def test_without_file_kickouts_nothing_moves(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["Col One", "1"])

    with patch.object(FileManifest, "history") as history, \
         patch.object(file_routing, "move_file") as move:
        result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    history.assert_not_called()
    move.assert_not_called()
    _only_failure(result, "requires a current clear profile")


def test_a_dry_run_kicks_nothing_out(run_log, tmp_path: Path) -> None:
    source = _source(tmp_path, ["Col One", "1"])
    config = {**_config(tmp_path), "file_kickouts": _FILE_KICKOUTS}

    with patch.object(FileManifest, "history") as history, \
         patch.object(file_routing, "move_file") as move:
        _run(_ctx(tmp_path), run_log, config, [_record(source)], apply=False)

    history.assert_not_called()
    move.assert_not_called()


def test_a_state_that_cannot_be_opened_is_a_file_failure(run_log, tmp_path: Path) -> None:
    source, _ = _tabular(tmp_path)
    unopenable = SimpleNamespace(
        data_file=lambda: (_ for _ in ()).throw(ConfigError("no DataFile for TXT")))

    with patch.object(loader_prepare.ManifestSource, "create", return_value=unopenable):
        result = _run(_ctx(tmp_path), run_log, _config(tmp_path), [_record(source)])

    _only_failure(result, "cannot be opened for preparation")


def test_a_redacted_companion_that_cannot_be_built_is_a_file_failure(
    run_log, tmp_path: Path,
) -> None:
    """Row 606 live finding: RedactionExhausted is this file's failure (rule 75).

    The file is kicked out and recorded, nothing is published for it, and the
    batch goes on to prepare the next file.
    """
    from rey_lib.redaction.registry import RedactionExhausted

    first = _source(tmp_path, [
        "Account Number,Trade Amount,Settle Date", "A-1,100,2026-05-01"],
        name="CWATranMay26.csv")
    _profile(tmp_path, first)
    second = _source(tmp_path, [
        "Account Number,Trade Amount,Settle Date", "A-2,200,2026-05-02"],
        name="CWAHoldMay26.csv")
    _profile(tmp_path, second, source_record_id=11)
    config = {**_config(tmp_path), "file_kickouts": _FILE_KICKOUTS}
    config["outbox"] = {**config["outbox"], "redacted_copy": True}
    real = loader_prepare.redacted_csv_text
    calls = {"n": 0}

    def exhausted_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RedactionExhausted("Column 'asset_code': no replacement left")
        return real(*args, **kwargs)

    with patch.object(loader_prepare, "redacted_csv_text", side_effect=exhausted_first), \
         patch.object(loader_prepare, "kick_out_original") as kickout:
        result = _run(_ctx(tmp_path), run_log, config,
                      [_record(first), _record(second, record_id=11)])

    assert (result.selected, result.prepared, result.failed) == (2, 1, 1)
    failed = next(item for item in result.results if item.status == "failed")
    assert "no replacement left" in failed.reason
    assert not (tmp_path / "prepared" / first.name).exists()
    assert (tmp_path / "prepared" / second.name).exists()
    kickout.assert_called_once()
    assert kickout.call_args.kwargs["operation"] == "create_prepared_files"
    assert kickout.call_args.kwargs["destination"] == _FILE_KICKOUTS["path"]

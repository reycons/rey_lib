"""The Loader's governed whole-file sanitization (row 589, step 4).

The legacy file_operator sanitization tests, copied and pointed at the
Loader-owned implementation, rey_lib.load.sanitize. Each selected row now opens
exactly its own mutation as a DataFile and is sanitized through
``Transform(sanitize)``; every legacy assertion is kept. The tests after the
copied ones cover what the boundary added: the transform, the governed
DataFiles an applied run returns, the dry run returning none, and a file with no
registered DataFile type.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rey_lib.config.config_namespace import Namespace
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files import FileSanitizationCollisionPolicy
from rey_lib.files.data_file import data_file_for
from rey_lib.load import Transform
from rey_lib.load import sanitize as loader_sanitize
from rey_lib.load.sanitize import SanitizationError, SanitizeTransform, run_file_sanitization
from tests.support.selecting_control import SelectingControl


class _Paths:
    def __init__(self, data: Path) -> None:
        self.data = data

    def resolve(self, name: str) -> Path:
        if name != "data":
            raise AssertionError(f"Unexpected path lookup: {name}")
        return self.data


def _ctx(tmp_path: Path, *rows: object) -> SimpleNamespace:
    """A context reaching the control database, with its selector primed."""
    control = SelectingControl()
    control.selected = [dict(row) for row in rows]
    return SimpleNamespace(
        app_name="rey_loader",
        paths=_Paths(tmp_path),
        pipeline_step_name="sanitize_received_files",
        pipeline_step_id="step-4",
        pipeline_run_id="run-1",
        shared_control=control,
    )


def _record(record_id: int, file_id: int, path: Path,
            *, converted: bool = False) -> dict[str, object]:
    """One row as the selection routine returns it.

    The governed identity is control.file_manifest.file_manifest_id, a positive
    integer the database mints; the string identity was retired. The step
    translates it to file_id for the reference it builds.
    """
    return {
        "file_mutation_id": record_id,
        "record_type": "source_file_mutation",
        "file_manifest_id": file_id,
        "file": {"path": str(path), "file_name": path.name},
        "classification": {
            "type": "file_name_regex",
            "values": {"client": "alpha", "nested": {"kind": "positions"}},
        },
        # An excel conversion left its operator on the mutation; a delivered
        # CSV has none. That is what the origin is read from.
        **({"conversion": {"operator": "excel_conversion"}} if converted else {}),
    }


def _config(tmp_path: Path) -> dict[str, object]:
    return {
        # One selection, named once. The step declares no selections of its
        # own: the routine names the files that still need sanitizing, and
        # whether one was produced by an excel conversion or delivered as CSV
        # is read from the record's own conversion.operator.
        "file_selection": {
            "procedure": "files_to_sanitize",
            "source_field": "file.path",
        },
        # The feed is the step's, not each selection's: there is one selection.
        "feed": "bmo",
        "outbox": {
            "path": str(
                tmp_path
                / "clean"
                / "<classification.values.client>"
                / "<file.file_name>"
            ),
            "overwrite": False,
        },
        "sanitization": {
            "global": {
                "policy_name": "platform",
                "policy_version": "1.0",
                "remove": {
                    "U+0000": {"name": "NULL", "reason": "invalid null"}
                },
                "preserve": {},
                "preserve_if_quoted": {
                    "U+000A": {"name": "LF", "reason": "quoted newline"},
                    "U+000D": {"name": "CR", "reason": "quoted newline"},
                },
                "replace": {},
                "line_repair": {},
            },
            "feeds": {
                "bmo": {
                    "policy_name": "bmo",
                    "policy_version": "1.0",
                    "remove": {},
                    "preserve": {},
                    "preserve_if_quoted": {},
                    "replace": {},
                    "line_repair": {},
                }
            },
        },
    }


def _result(path: Path, *, applied: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        filesystem_applied=applied,
        destination_path=path,
        destination_replaced=False,
        destination_sha256="destination-sha256",
        destination_size=12,
        effective_policy_digest="policy-sha256",
        # The M7 record sanitization appended; None on a dry run.
        file_manifest_record_id=77 if applied else None,
    )


def _opened(control: SelectingControl, *, file_mutation_id: int) -> SimpleNamespace:
    """ManifestSource opened at exactly the selected row's mutation."""
    row = next(one for one in control.selected
               if one["file_mutation_id"] == file_mutation_id)
    return SimpleNamespace(data_file=lambda: data_file_for(
        Path(row["file"]["path"]),
        file_manifest_id=row["file_manifest_id"],
        file_mutation_id=file_mutation_id,
        classification=row["classification"],
        base_path="/data/alpha",
    ))


@pytest.fixture(autouse=True)
def _mute_file_reference_logging():
    with (
        patch("rey_lib.load.sanitize.log_input_file_reference"),
        patch("rey_lib.load.sanitize.log_artifact_reference"),
    ):
        yield


@pytest.fixture(autouse=True)
def _selected_state_opens_as_a_data_file():
    """Each selected row opens its own mutation, as ManifestSource does."""
    with patch.object(loader_sanitize.ManifestSource, "create", side_effect=_opened):
        yield


# -- copied from the legacy tests ---------------------------------------------

def test_excel_generated_and_delivered_csv_use_the_same_shared_boundary(run_log,
    tmp_path: Path,
) -> None:
    excel_source = tmp_path / "converted" / "book.Sheet.csv"
    delivered_source = tmp_path / "processing" / "delivered.csv"
    records = (
        _record(10, 101, excel_source, converted=True),
        _record(20, 202, delivered_source),
    )
    calls: list[tuple[object, object]] = []

    def capture(operation: object, file_reference: object) -> SimpleNamespace:
        calls.append((operation, file_reference))
        return _result(tmp_path / "clean" / file_reference.current_path.name)

    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            side_effect=capture,
        ),
    ):
        result = run_file_sanitization(_ctx(tmp_path, *records), run_log, _config(tmp_path))

    # One selection, asked once. Both origins come back from it, and which is
    # which is read from each record's own conversion.operator.
    assert result.selected == result.sanitized == 2
    assert [file.file_id for _, file in calls] == [101, 202]
    assert [file.current_path for _, file in calls] == [excel_source, delivered_source]
    assert [operation.mutation_run_log_fields["source_origin"] for operation, _ in calls] == [
        "excel_generated",
        "delivered_csv",
    ]
    assert [operation.destination_path for operation, _ in calls] == [
        (tmp_path / "clean" / "alpha" / "book.Sheet.csv").resolve(),
        (tmp_path / "clean" / "alpha" / "delivered.csv").resolve(),
    ]
    assert all(operation.add_source_line_number is True for operation, _ in calls)
    assert all(operation.policy.global_policy_name == "platform" for operation, _ in calls)
    assert all(operation.policy.feed_policy_name == "bmo" for operation, _ in calls)


def test_loaded_namespace_preserves_complete_inline_sanitization_mapping(run_log,
    tmp_path: Path,
) -> None:
    record = _record(10, 1006, tmp_path / "source.csv")
    config = _config(tmp_path)

    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            return_value=_result(tmp_path / "clean" / "source.csv"),
        ) as shared,
    ):
        result = run_file_sanitization(_ctx(tmp_path, record), run_log, Namespace(config))

    assert result.selected == result.sanitized == 1
    assert shared.call_args.args[0].policy.global_policy_name == "platform"
    assert shared.call_args.args[0].policy.feed_policy_name == "bmo"


def test_success_logs_viewable_input_and_output_file_references(run_log,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    destination = tmp_path / "clean" / "alpha" / "source.csv"
    record = _record(10, 1006, source)
    config = _config(tmp_path)

    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            return_value=_result(destination),
        ),
        patch(
            "rey_lib.load.sanitize.log_input_file_reference"
        ) as input_reference,
        patch(
            "rey_lib.load.sanitize.log_artifact_reference"
        ) as output_reference,
    ):
        run_file_sanitization(_ctx(tmp_path, record), run_log, config)

    input_reference.assert_called_once()
    assert input_reference.call_args.args[1] == str(source)
    assert input_reference.call_args.kwargs["file_role"] == "sanitization_input"
    assert input_reference.call_args.kwargs["safe_to_preview"] is True
    assert input_reference.call_args.kwargs["viewer_type"] == "file"
    output_reference.assert_called_once()
    assert output_reference.call_args.args[1] == str(destination)
    assert output_reference.call_args.kwargs["role"] == "sanitized_file"
    assert output_reference.call_args.kwargs["artifact_group"] == "output_files"
    assert output_reference.call_args.kwargs["safe_to_preview"] is True
    assert output_reference.call_args.kwargs["viewer_type"] == "file"


def test_multiple_converted_outputs_preserve_one_file_id_and_distinct_paths(run_log,
    tmp_path: Path,
) -> None:
    first = _record(10, 416, tmp_path / "book.Sheet1.csv")
    second = _record(11, 416, tmp_path / "book.Sheet2.csv")
    config = _config(tmp_path)

    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            return_value=_result(tmp_path / "clean" / "source.csv"),
        ) as shared,
    ):
        result = run_file_sanitization(_ctx(tmp_path, first, second), run_log, config)

    assert result.selected == result.sanitized == 2
    assert [call.args[1].file_id for call in shared.call_args_list] == [416, 416]
    assert [call.args[1].current_path.name for call in shared.call_args_list] == [
        "book.Sheet1.csv",
        "book.Sheet2.csv",
    ]


def test_historical_record_without_file_name_uses_governed_path_basename(run_log,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.Sheet.csv"
    record = _record(10, 416, source)
    config = _config(tmp_path)

    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            return_value=_result(tmp_path / "clean" / "book.Sheet.csv"),
        ) as shared,
    ):
        result = run_file_sanitization(_ctx(tmp_path, record), run_log, config)

    assert result.selected == result.sanitized == 1
    assert shared.call_args.args[0].destination_path == (
        tmp_path / "clean" / "alpha" / "book.Sheet.csv"
    ).resolve()


def test_complete_classification_is_passed_unchanged_without_mutating_record(run_log,
    tmp_path: Path,
) -> None:
    record = _record(10, 1006, tmp_path / "source.csv")
    snapshot = deepcopy(record)
    config = _config(tmp_path)

    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            return_value=_result(tmp_path / "clean" / "book.Sheet.csv"),
        ) as shared,
    ):
        run_file_sanitization(_ctx(tmp_path, record), run_log, config)

    supplied = shared.call_args.args[1].classification
    assert supplied == record["classification"]
    assert record == snapshot


def test_dry_run_is_owned_by_workflow_apply_boundary(run_log, tmp_path: Path) -> None:
    record = _record(10, 1006, tmp_path / "source.csv")
    config = _config(tmp_path)

    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            return_value=_result(tmp_path / "clean" / "source.csv", applied=False),
        ) as shared,
    ):
        result = run_file_sanitization(_ctx(tmp_path, record), run_log, config, apply=False)

    assert shared.call_args.args[0].dry_run is True
    assert result.selected == 1
    assert result.sanitized == 0


def test_explicit_overwrite_selects_shared_overwrite_policy(run_log, tmp_path: Path) -> None:
    record = _record(10, 1006, tmp_path / "source.csv")
    config = _config(tmp_path)
    config["outbox"]["overwrite"] = True  # type: ignore[index]

    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            return_value=_result(tmp_path / "clean" / "source.csv"),
        ) as shared,
    ):
        run_file_sanitization(_ctx(tmp_path, record), run_log, config)

    assert (
        shared.call_args.args[0].collision_policy
        is FileSanitizationCollisionPolicy.OVERWRITE
    )


@pytest.mark.parametrize(
    "config, message",
    [
        ({}, "requires an inline 'file_selection' mapping"),
        ({"config_ref": "sanitizers.received"}, "config_ref is unsupported"),
        (
            {
                "file_selection": {
                    "procedure": "files_to_sanitize",
                    "source_field": "file.path",
                },
                "feed": "bmo",
                "outbox": "/out/file.csv",
            },
            "outbox.*folder mapping",
        ),
        (
            {"outbox": {"path": "/out/file.csv"}, "overwrite": True},
            "step-level 'overwrite' is unsupported",
        ),
        (
            {
                "file_selection": {
                    "procedure": "files_to_sanitize",
                    "source_field": "file.path",
                },
                "feed": "bmo",
                "outbox": {"path": "/out/file.csv", "overwrite": "yes"},
            },
            "folder 'outbox' 'overwrite' must be true or false",
        ),
        # The step declares no selection of its own any more: the routine
        # names the files that still need sanitizing.
        (
            {
                "outbox": {"path": "/out/file.csv"},
                "selections": [],
            },
            "declares no selection of its own",
        ),
        (
            {"outbox": {"path": "/out/file.csv"}},
            "requires an inline 'file_selection' mapping",
        ),
        (
            {
                "outbox": {"path": "/out/file.csv"},
                "file_selection": {"procedure": "files_to_sanitize"},
            },
            "source_field",
        ),
        (
            {
                "outbox": {"path": "/out/file.csv"},
                "file_selection": {
                    "procedure": "files_to_sanitize",
                    "source_field": "file.path",
                },
                "feed": "bmo",
            },
            "inline 'sanitization' mapping",
        ),
    ],
)
def test_inline_configuration_fails_closed(run_log,
    tmp_path: Path,
    config: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(SanitizationError, match=message):
        run_file_sanitization(_ctx(tmp_path), run_log, config)


def test_sanitized_outbox_redacted_copy_defaults_to_false(run_log, tmp_path: Path) -> None:
    """An outbox without the flag keeps its current single-artifact behaviour."""
    config = _config(tmp_path)
    assert "redacted_copy" not in config["outbox"]

    records = (_record(10, 1001, tmp_path / "processing" / "a.csv"),)
    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            side_effect=lambda *_a, **_k: _result(tmp_path / "clean" / "a.csv"),
        ),
        patch("rey_lib.load.sanitize.publish_file_set") as publish,
    ):
        run_file_sanitization(_ctx(tmp_path, *records), run_log, config)

    assert publish.call_count == 0


def test_sanitized_outbox_redacted_copy_publishes_a_redacted_sister(run_log,
    tmp_path: Path,
) -> None:
    """The sister sits beside the sanitized artifact and preserves its shape."""
    sanitized = tmp_path / "clean" / "a.csv"
    sanitized.parent.mkdir(parents=True)
    sanitized.write_text(
        "account_number,amount\n12345,100.00\n67890,250.50\n", encoding="utf-8"
    )
    config = _config(tmp_path)
    config["outbox"]["redacted_copy"] = True

    records = (_record(10, 1001, tmp_path / "processing" / "a.csv"),)
    with (
        patch(
            "rey_lib.load.sanitize.sanitize_file",
            side_effect=lambda *_a, **_k: _result(sanitized),
        ),
        patch("rey_lib.load.sanitize.log_artifact_reference") as artifact,
        patch(
            "rey_lib.load.sanitize.log_governed_source_file_mutation",
            return_value=91,
        ) as mutation,
    ):
        run_file_sanitization(_ctx(tmp_path, *records), run_log, config)

    sister = tmp_path / "clean" / "a.redacted.csv"
    original_lines = sanitized.read_text(encoding="utf-8").splitlines()
    sister_lines = sister.read_text(encoding="utf-8").splitlines()

    assert sister_lines[0] == original_lines[0]
    assert len(sister_lines) == len(original_lines)
    for source_line, sister_line in zip(original_lines[1:], sister_lines[1:]):
        assert len(source_line.split(",")) == len(sister_line.split(","))
    assert sister_lines[1:] != original_lines[1:]
    assert original_lines[1] == "12345,100.00"  # the original is untouched
    assert [call.kwargs["role"] for call in artifact.call_args_list] == [
        "sanitized_file", "redacted_sanitized_file",
    ]
    # The companion needs its own lifecycle record or the governed file's tree
    # never shows it.
    assert mutation.call_count == 1
    assert mutation.call_args.kwargs["reason"] == "redacted_sanitized_file"
    assert mutation.call_args.kwargs["action"] == "create"


def test_sanitized_folder_rejects_unknown_keys_and_non_boolean_flag(run_log,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config["outbox"]["redacted_path"] = "/nope"
    with pytest.raises(SanitizationError, match="unknown fields: redacted_path"):
        run_file_sanitization(_ctx(tmp_path), run_log, config)

    config = _config(tmp_path)
    config["outbox"]["redacted_copy"] = "yes"
    with pytest.raises(SanitizationError, match="'redacted_copy' must be true or false"):
        run_file_sanitization(_ctx(tmp_path), run_log, config)


# -- what the boundary added ---------------------------------------------------

def _sanitizer(tmp_path: Path, record: dict[str, object], **outbox) -> SanitizeTransform:
    config = _config(tmp_path)
    config["outbox"].update(outbox)  # type: ignore[union-attr]
    return Transform(
        values={"process": config, "record": record}, selected="sanitize",
    ).resolve(_ctx(tmp_path, record))


def _selected(record: dict[str, object]):
    return data_file_for(
        Path(record["file"]["path"]),  # type: ignore[index]
        file_manifest_id=record["file_manifest_id"],
        file_mutation_id=record["file_mutation_id"],
        classification=record["classification"],
        base_path="/data/alpha",
    )


def test_sanitize_is_resolved_through_the_one_resolver(tmp_path: Path) -> None:
    record = _record(10, 1006, tmp_path / "source.csv")

    assert isinstance(_sanitizer(tmp_path, record), SanitizeTransform)
    assert "sanitize" not in Transform.kinds()


def test_an_applied_sanitize_returns_the_sanitized_file_at_its_record(
    run_log, tmp_path: Path,
) -> None:
    record = _record(10, 1006, tmp_path / "source.csv")
    destination = tmp_path / "clean" / "alpha" / "source.csv"

    with patch("rey_lib.load.sanitize.sanitize_file", return_value=_result(destination)):
        (sanitized,) = _sanitizer(tmp_path, record).apply(_selected(record))

    assert sanitized.path == destination
    assert (sanitized.file_manifest_id, sanitized.file_mutation_id) == (1006, 77)
    assert sanitized.classification == record["classification"]
    assert sanitized.base_path == "/data/alpha"


def test_the_redacted_sister_is_returned_at_its_own_mutation(
    run_log, tmp_path: Path,
) -> None:
    record = _record(10, 1006, tmp_path / "source.csv")
    sanitized_path = tmp_path / "clean" / "alpha" / "source.csv"
    sanitized_path.parent.mkdir(parents=True)
    sanitized_path.write_text("account_number,amount\n12345,100.00\n", encoding="utf-8")

    with patch("rey_lib.load.sanitize.sanitize_file", return_value=_result(sanitized_path)), \
         patch("rey_lib.load.sanitize.log_governed_source_file_mutation", return_value=91):
        sanitized, sister = _sanitizer(
            tmp_path, record, redacted_copy=True).apply(_selected(record))

    assert sanitized.file_mutation_id == 77
    assert sister.path == tmp_path / "clean" / "alpha" / "source.redacted.csv"
    assert (sister.file_manifest_id, sister.file_mutation_id) == (1006, 91)


def test_a_dry_run_returns_sanitizations_answer_and_no_governed_file(
    run_log, tmp_path: Path,
) -> None:
    """No M7/M9 is written on a dry run, so no governed identity is invented."""
    record = _record(10, 1006, tmp_path / "source.csv")
    planned = _result(tmp_path / "clean" / "alpha" / "source.csv", applied=False)

    with patch("rey_lib.load.sanitize.sanitize_file", return_value=planned) as shared, \
         patch("rey_lib.load.sanitize.publish_file_set") as publish:
        answer = _sanitizer(tmp_path, record, redacted_copy=True).plan(_selected(record))

    assert answer is planned
    assert shared.call_args.args[0].dry_run is True
    publish.assert_not_called()


def test_a_sanitization_that_applied_nothing_returns_no_file(
    run_log, tmp_path: Path,
) -> None:
    record = _record(10, 1006, tmp_path / "source.csv")
    failed = _result(tmp_path / "clean" / "alpha" / "source.csv", applied=False)

    with patch("rey_lib.load.sanitize.sanitize_file", return_value=failed):
        sanitizer = _sanitizer(tmp_path, record)
        produced = sanitizer.apply(_selected(record))

    assert produced == ()
    assert sanitizer.result is failed


def test_a_file_with_no_registered_data_file_type_stops_the_step(
    run_log, tmp_path: Path,
) -> None:
    record = _record(10, 1006, tmp_path / "source.unknown")
    unregistered = SimpleNamespace(
        data_file=lambda: (_ for _ in ()).throw(ConfigError("no DataFile for UNKNOWN")))

    with patch.object(loader_sanitize.ManifestSource, "create", return_value=unregistered), \
         patch("rey_lib.load.sanitize.sanitize_file") as shared:
        with pytest.raises(SanitizationError, match="no registered DataFile type"):
            run_file_sanitization(_ctx(tmp_path, record), run_log, _config(tmp_path))

    shared.assert_not_called()

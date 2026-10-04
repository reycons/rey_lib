"""Focused tests for the Loader Excel conversion lifecycle (row 589, step 3).

The legacy file_operator tests, copied and pointed at the Loader-owned
implementation, rey_lib.load.convert. Conversion candidates are resolved from
the governed source-file lifecycle manifest and handed to the unchanged
conversion loop -- now through ManifestSource -> DataFile -> Transform(convert),
with every legacy assertion kept. Each selector row carries the mutation the
routine returned, which is what the workbook is opened at; the tests after the
copied ones cover what the boundary added.
"""

from __future__ import annotations

import json
import inspect
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.support.manifest_rows import manifest_row
from tests.support.selecting_control import SelectingControl

from rey_lib.load import convert as excel_conversion
from rey_lib.errors.error_utils import ConfigError
from rey_lib.load import Transform
from rey_lib.load.convert import (
    ConversionError,
    ConvertTransform,
    resolve_excel_conversion_config,
    run_excel_conversion,
    select_conversion_candidates,
)

from rey_lib.config.config_namespace import Namespace
from rey_lib.files import WorkbookOpenError, serialize_source_file_mutation
from rey_lib.files.data_file import data_file_for
from rey_lib.logs import bind_run, bind_step, clear_run, clear_step

#: A sentinel, so a test can say "this row carries no classification".
_UNSET = object()


class _Lifecycle:
    """Captures the governed mutations the conversion records.

    Mutations are produced through the shared rey_lib boundary, so this fake
    stands in for that one producer rather than for the manifest writer.
    """

    def __init__(self) -> None:
        self.mutations: list[dict[str, object]] = []

    def log_governed_source_file_mutation(
        self, _ctx: object, mutation_context: object, **kwargs: object
    ) -> int:
        # Build the record through the real serializer so what is captured is
        # the canonical record the producer actually persists.
        kwargs.pop("message", None)
        kwargs.pop("run_log_fields", None)
        kwargs["file_id"] = mutation_context.file_id
        kwargs["classification"] = mutation_context.classification
        record = serialize_source_file_mutation(
            run_log_id=len(self.mutations) + 1,
            **kwargs,
        )
        self.mutations.append(record)
        return 100 + len(self.mutations)

    def log_source_file_mutation(self, _ctx: object, **kwargs: object) -> int:
        # The moves are routing's (the move kind), recorded through the same
        # canonical serializer as the conversion's own mutations.
        kwargs.pop("message", None)
        kwargs.pop("run_log_fields", None)
        for name in ("source_path", "destination_path", "recovery_path",
                     "previous_version_path"):
            if name in kwargs:
                kwargs[name] = str(kwargs[name])
        record = serialize_source_file_mutation(
            run_log_id=len(self.mutations) + 1,
            **kwargs,
        )
        self.mutations.append(record)
        return 100 + len(self.mutations)

    def of_action(self, action: str) -> list[dict[str, object]]:
        return [item for item in self.mutations if item.get("action") == action]


@pytest.fixture(autouse=True)
def lifecycle() -> _Lifecycle:
    """Route the shared mutation producer through a capture for every test."""
    captured = _Lifecycle()
    with patch(
        "rey_lib.load.convert.log_governed_source_file_mutation",
        side_effect=captured.log_governed_source_file_mutation,
    ), patch(
        "rey_lib.files.file_routing.log_source_file_mutation",
        side_effect=captured.log_source_file_mutation,
    ):
        yield captured


@pytest.fixture(autouse=True)
def _the_step_run_log_is_bound(run_log):
    """Bind the step's run log and step, as the workflow does before any step runs.

    The transform writes through the bound run log; in production that is the
    very run log the step is handed. A kickout states the bound step as the
    operation it served.
    """
    bind_run(run_log)
    bind_step(step_id="convert_alpha_workbooks")
    try:
        yield
    finally:
        clear_step()
        clear_run()


_CONTROLS: dict[Path, SelectingControl] = {}


@pytest.fixture(autouse=True)
def _clear_controls():
    yield
    _CONTROLS.clear()


def _control(tmp_path: Path) -> SelectingControl:
    """The governed manifest this test's selection is answered from."""
    control = _CONTROLS.get(tmp_path)
    if control is None:
        control = SelectingControl()
        _CONTROLS[tmp_path] = control
    return control


def _selector_row(
    file_manifest_id: object,
    path: Path | str | None,
    *,
    values: dict[str, object] | None = None,
    classification: object = _UNSET,
) -> dict[str, object]:
    """One governed file as the manifest retrieval routine returns it.

    The step reads what ``_resolve_candidate`` consumes off the ManifestSource
    built from this row (backlog 619): the governed identity, the
    classification that supplies the destination values, and the file's current
    path. The routine resolves the current location from the mutation history,
    so there is no inventory record to join and no lineage to follow -- a
    governed file has one identity.
    """
    if classification is _UNSET:
        classification = {
            "type": "file_name_regex",
            "source_field": "file.path",
            "values": dict(values) if values is not None else {"group": "alpha"},
        }
    # The mutation the routine selected; the workbook opens at exactly it. It
    # was inventoried where it is and has not moved, so it is its own original.
    mutation = 7000 + int(file_manifest_id)
    located = None if path is None else str(path)
    return manifest_row(
        file_manifest_id, mutation, path,
        classification=classification, base_path="/data/alpha",
        manifest_path=located, original_path=located, original_mutation_id=mutation,
    )


def _selector_rows(tmp_path: Path, *rows: dict[str, object]) -> None:
    """Prime what this test's manifest retrieval returns."""
    _control(tmp_path).selected = [dict(row) for row in rows]


def _source_file(tmp_path: Path, name: str, group: str = "alpha") -> Path:
    inbox = tmp_path / group / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    source = inbox / name
    source.write_bytes(b"workbook")
    return source


def _entry(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "name": "alpha",
        "enabled": True,
        # No file_extensions and no inbox: which workbooks are this step's work
        # is the routine's answer, and declaring them inline is refused. The
        # entry names that routine and the column of the returned row that
        # holds the workbook's current path.
        "file_selection": {
            "operation": "excel_conversion",
            "source_field": "path",
        },
        "processing": str(tmp_path / "processing"),
        "outbox": str(tmp_path / "outbox"),
        "archive": str(tmp_path / "archive"),
        "include_hidden_sheets": False,
        "include_empty_sheets": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ctx(tmp_path: Path, *entries: object) -> SimpleNamespace:
    return SimpleNamespace(
        app_name="rey_loader",
        # The move kind routes within the governed root.
        paths=SimpleNamespace(resolve=lambda name: str(tmp_path)),
        excel_conversions=list(entries or [_entry(tmp_path)]),
        pipeline_name="alpha_pipeline",
        pipeline_run_id="run-1",
        pipeline_step_name="convert_alpha_workbooks",
        pipeline_step_id="step-1",
        # The governed manifest is a table in the control database, so a
        # context that reaches it carries the Control it is reached through.
        shared_control=_control(tmp_path),
    )


def _conversion_result(source: Path, output: Path) -> SimpleNamespace:
    artifact = SimpleNamespace(
        output_path=output,
        extraction_kind="worksheet",
        sheet_name="Sheet 1",
        sheet_index=0,
        table_name=None,
        row_count=2,
        column_count=2,
        column_names=("Account", "Amount"),
    )
    return SimpleNamespace(
        source_path=source,
        workbook_name=source.name,
        outputs=(artifact,),
        warnings=(),
    )


def _converter(source: Path, outbox: Path, **_kwargs: object) -> SimpleNamespace:
    output = outbox / f"{source.stem}.Sheet1.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("Account,Amount\nA,1\n", encoding="utf-8")
    return _conversion_result(source, output)


def _one_workbook(tmp_path: Path, name: str = "a.xls") -> Path:
    source = _source_file(tmp_path, name)
    _selector_rows(tmp_path, _selector_row(1001, source))
    return source


def _config(ctx: SimpleNamespace) -> object:
    return resolve_excel_conversion_config(_inline(ctx))


def _inline(ctx: SimpleNamespace) -> dict[str, object]:
    """Return the selected entry as the canonical process-local mapping."""
    return vars(ctx.excel_conversions[0]).copy()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_resolve_excel_conversion_config_returns_concrete_paths(
    tmp_path: Path,
) -> None:
    resolved = _config(_ctx(tmp_path))

    assert resolved.name == "alpha"
    assert resolved.enabled is True
    assert resolved.operation == "excel_conversion"
    assert resolved.source_field == "path"
    assert resolved.processing == str(tmp_path / "processing")
    assert resolved.outbox == str(tmp_path / "outbox")
    assert resolved.archive == str(tmp_path / "archive")
    assert resolved.folder_overwrite == {
        "processing": False,
        "outbox": False,
        "archive": False,
    }


def test_folder_declarations_own_their_overwrite_authority(tmp_path: Path) -> None:
    entry = _entry(
        tmp_path,
        processing={"path": str(tmp_path / "processing"), "overwrite": True},
        outbox={"path": str(tmp_path / "outbox"), "overwrite": True},
        archive={"path": str(tmp_path / "archive"), "overwrite": True},
    )

    resolved = _config(_ctx(tmp_path, entry))

    assert resolved.processing == str(tmp_path / "processing")
    assert resolved.outbox == str(tmp_path / "outbox")
    assert resolved.archive == str(tmp_path / "archive")
    assert resolved.folder_overwrite == {
        "processing": True,
        "outbox": True,
        "archive": True,
    }


def test_folder_declarations_are_read_when_config_loading_yields_namespaces(
    tmp_path: Path,
) -> None:
    """Loaded YAML supplies Namespace sections, not dicts, for nested folders."""
    entry = _entry(
        tmp_path,
        outbox=Namespace({"path": str(tmp_path / "outbox"), "overwrite": True}),
        archive=Namespace({"path": str(tmp_path / "archive"), "overwrite": True}),
    )

    resolved = _config(_ctx(tmp_path, entry))

    assert resolved.outbox == str(tmp_path / "outbox")
    assert resolved.archive == str(tmp_path / "archive")
    assert resolved.folder_overwrite["outbox"] is True
    assert resolved.folder_overwrite["archive"] is True


def test_folder_overwrite_must_be_boolean(tmp_path: Path) -> None:
    entry = _entry(
        tmp_path,
        outbox={"path": str(tmp_path / "outbox"), "overwrite": "yes"},
    )

    with pytest.raises(ConversionError, match="outbox.*overwrite.*boolean"):
        _config(_ctx(tmp_path, entry))


def test_resolver_rejects_retained_inbox_discovery_field(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    entry.inbox = str(tmp_path / "alpha" / "inbox")

    with pytest.raises(ConversionError, match="must not declare 'inbox'"):
        _config(_ctx(tmp_path, entry))


@pytest.mark.parametrize(
    "unsupported",
    [
        "excel_conversions.alpha",
        {"config_ref": "excel_conversions.alpha"},
    ],
)
def test_resolver_rejects_config_ref_input(unsupported: object) -> None:
    with pytest.raises(ConversionError, match="config_ref is unsupported"):
        resolve_excel_conversion_config(unsupported)  # type: ignore[arg-type]


def test_resolver_rejects_non_mapping_inline_configuration() -> None:
    with pytest.raises(ConversionError, match="requires an inline process"):
        resolve_excel_conversion_config(None)  # type: ignore[arg-type]


def test_resolver_allows_classification_owned_processing(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    del entry.processing
    assert _config(_ctx(tmp_path, entry)).processing is None


def test_resolver_requires_the_lifecycle_selector(tmp_path: Path) -> None:
    """A conversion that names no routine cannot say what its work is."""
    entry = _entry(tmp_path)
    del entry.file_selection
    with pytest.raises(ConversionError, match="requires a 'file_selection' mapping"):
        _config(_ctx(tmp_path, entry))


def test_the_selector_must_name_the_column_holding_the_workbook(tmp_path: Path) -> None:
    entry = _entry(tmp_path, file_selection={"operation": "excel_conversion"})
    with pytest.raises(ConversionError, match="source_field"):
        _config(_ctx(tmp_path, entry))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"enabled": "true"}, "enabled.*boolean"),
        ({"processing": ""}, "processing.*non-empty path"),
        ({"include_hidden_sheets": "false"}, "include_hidden_sheets.*boolean"),
    ],
)
def test_resolver_rejects_malformed_entry(
    tmp_path: Path,
    override: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ConversionError, match=message):
        _config(_ctx(tmp_path, _entry(tmp_path, **override)))


# ---------------------------------------------------------------------------
# Resolution behaviour
# ---------------------------------------------------------------------------


def test_classified_workbook_resolves_and_enters_the_existing_converter(run_log, 
    tmp_path: Path,
) -> None:
    source = _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert, patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert [call.args[0].name for call in convert.call_args_list] == [source.name]
    assert convert.call_args.args[0].parent == (tmp_path / "processing")
    assert convert.call_args.kwargs["overwrite"] is False
    assert (tmp_path / "archive" / source.name).exists()


def test_outbox_folder_overwrite_reaches_shared_workbook_writer(run_log, 
    tmp_path: Path,
) -> None:
    source = _one_workbook(tmp_path)
    entry = _entry(
        tmp_path,
        outbox={"path": str(tmp_path / "outbox"), "overwrite": True},
    )
    ctx = _ctx(tmp_path, entry)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert, patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert convert.call_args.args[0].name == source.name
    assert convert.call_args.kwargs["overwrite"] is True


def test_conversion_consumes_classification_owned_processing_path(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    processing = tmp_path / "processing"
    processing.mkdir()
    source = processing / "book.xls"
    source.write_bytes(b"workbook")
    inventory_path = tmp_path / "alpha" / "inbox" / source.name
    _selector_rows(tmp_path, _selector_row(1001, source))
    entry = _entry(tmp_path)
    del entry.processing
    ctx = _ctx(tmp_path, entry)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert, patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert convert.call_args.args[0] == source
    assert not [
        record
        for record in lifecycle.of_action("move")
        if record.get("reason") == "processing"
    ]
    assert (tmp_path / "archive" / source.name).exists()


def test_classified_csv_is_skipped_without_failing_a_valid_workbook(run_log, 
    tmp_path: Path,
) -> None:
    workbook = _source_file(tmp_path, "book.xls")
    spreadsheet = _source_file(tmp_path, "extract.csv")
    _selector_rows(
        tmp_path,
        _selector_row(1001, workbook),
        _selector_row(1011, spreadsheet),
    )
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert, patch(
        "rey_lib.load.convert.log_artifact_reference"
    ), patch(
        "rey_lib.load.convert.log_validation_result"
    ) as validation:
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert [call.args[0].name for call in convert.call_args_list] == ["book.xls"]
    assert spreadsheet.exists()
    skipped = [
        call.kwargs
        for call in validation.call_args_list
        if call.kwargs.get("reason_code") == "unsupported_workbook"
    ]
    assert len(skipped) == 1
    assert skipped[0]["classification_record_id"] == 1011
    assert skipped[0]["status"] == "skipped"


def test_workbook_on_disk_without_a_classification_is_not_converted(run_log, 
    tmp_path: Path,
) -> None:
    classified = _source_file(tmp_path, "classified.xls")
    on_disk_only = _source_file(tmp_path, "on_disk_only.xls")
    _selector_rows(tmp_path, _selector_row(1001, classified))
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert, patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert [call.args[0].name for call in convert.call_args_list] == ["classified.xls"]
    assert on_disk_only.exists()


def test_one_malformed_candidate_does_not_block_a_valid_candidate(run_log, 
    tmp_path: Path,
) -> None:
    good = _source_file(tmp_path, "good.xls")
    _selector_rows(
        tmp_path,
        _selector_row(1012, tmp_path / "alpha" / "inbox" / "other.xls",
                      classification=None),
        _selector_row(1001, good),
    )
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert, patch(
        "rey_lib.load.convert.log_artifact_reference"
    ), patch(
        "rey_lib.load.convert.log_validation_result"
    ) as validation:
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert [call.args[0].name for call in convert.call_args_list] == ["good.xls"]
    assert [
        call.kwargs["reason_code"]
        for call in validation.call_args_list
        if "reason_code" in call.kwargs
    ] == ["missing_classification"]


@pytest.mark.parametrize(
    ("row", "reason_code"),
    [
        # Identity and lineage resolution used to live in _resolve_candidate and
        # does not any more, so invalid_classification_record_id,
        # missing_source_record_id, unresolved_inventory_record and
        # file_id_mismatch went with it: a governed file has one identity, and
        # where it currently is, is a relational fact the routine answers.
        # These are the refusals it still makes about a row it was handed.
        (lambda path: _selector_row(0, path), "missing_file_id"),
        (lambda path: _selector_row(1001, path, classification=None),
         "missing_classification"),
        (lambda path: _selector_row(1001, None), "missing_current_path"),
    ],
)
def test_a_structurally_unusable_row_is_a_rejected_candidate(run_log,
    tmp_path: Path,
    row: object,
    reason_code: str,
) -> None:
    """The database chose the row; whether it can be used is still decided here."""
    source = _source_file(tmp_path, "a.xls")
    _selector_rows(tmp_path, row(source))
    ctx = _ctx(tmp_path)

    selection = select_conversion_candidates(ctx, run_log, _config(ctx))

    assert selection.candidates == ()
    assert [item.reason_code for item in selection.rejected] == [reason_code]


def test_a_vanished_source_is_a_rejected_candidate(run_log,
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Selection resolved it; the file is not there. That is a rejection.

    The row-shape refusals are covered by
    test_a_structurally_unusable_row_is_a_rejected_candidate. What is unique
    here is the only stage that reads the filesystem: a row the routine
    resolved, whose file has since gone.
    """
    absent = _source_file(tmp_path, "gone.xls")
    _selector_rows(tmp_path, _selector_row(1011, absent))
    absent.unlink()
    ctx = _ctx(tmp_path)

    with caplog.at_level("ERROR"):
        selection = select_conversion_candidates(ctx, run_log, _config(ctx))

    assert selection.candidates == ()
    assert [item.reason_code for item in selection.rejected] == ["missing_source_file"]
    logged = [r for r in caplog.records if r.levelname == "ERROR"]
    # ConversionError replaces the legacy InputError (row 589, step 3).
    assert [type(r.exc_info[1]).__name__ for r in logged] == ["ConversionError"]


def test_missing_source_is_logged_as_an_exception_and_never_rediscovered(run_log,
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Inventory owns path governance; a vanished file is an explicit error."""
    good = _source_file(tmp_path, "good.xls")
    gone = _source_file(tmp_path, "gone.xls")
    _selector_rows(
        tmp_path,
        _selector_row(1001, good),
        _selector_row(1011, gone),
    )
    gone.unlink()
    ctx = _ctx(tmp_path)

    with caplog.at_level("ERROR"), patch(
        "os.scandir", side_effect=AssertionError("directory enumerated")
    ):
        selection = select_conversion_candidates(ctx, run_log, _config(ctx))

    assert [item.source_path for item in selection.candidates] == [good.resolve()]
    assert [(item.reason_code, item.classification_record_id)
            for item in selection.rejected] == [("missing_source_file", 1011)]
    logged = [r for r in caplog.records if r.levelname == "ERROR"]
    # ConversionError replaces the legacy InputError (row 589, step 3).
    assert [type(r.exc_info[1]).__name__ for r in logged] == ["ConversionError"]


def test_duplicate_classifications_produce_one_conversion_attempt(run_log, 
    tmp_path: Path,
) -> None:
    source = _source_file(tmp_path, "a.xls")
    # One governed file at TWO mutations. Two identical rows are one mutation,
    # and get_for_operation builds one object per mutation (backlog 619), so
    # the duplicate claim this guards against is the same file selected twice.
    second = _selector_row(1001, source)
    second["file_mutation_id"] = 9001
    _selector_rows(tmp_path, _selector_row(1001, source), second)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert, patch(
        "rey_lib.load.convert.log_artifact_reference"
    ), patch(
        "rey_lib.load.convert.log_validation_result"
    ) as validation:
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert convert.call_count == 1
    assert [
        call.kwargs["reason_code"]
        for call in validation.call_args_list
        if "reason_code" in call.kwargs
    ] == ["duplicate_inventory_source"]


def test_inventory_path_is_the_only_path_used(run_log, tmp_path: Path) -> None:
    """Classification values contradict the row's path; the path still wins."""
    source = _source_file(tmp_path, "actual_name.xls", group="unrelated_directory")
    _selector_rows(
        tmp_path,
        _selector_row(1001, source,
                      values={"group": "alpha", "name_hint": "a_different_name.xls"}),
    )
    ctx = _ctx(tmp_path)

    selection = select_conversion_candidates(ctx, run_log, _config(ctx))

    assert [item.source_path for item in selection.candidates] == [source.resolve()]


def test_selection_is_one_read_for_the_declared_operation(run_log, tmp_path: Path) -> None:
    """This module names an operation; it interprets no manifest filters."""
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    select_conversion_candidates(ctx, run_log, _config(ctx))

    assert ctx.shared_control.manifest_requests == [{
        "installation_id": 1, "operation": "excel_conversion", "source_name": None,
    }]


def test_a_retired_procedure_key_is_refused_by_name(tmp_path: Path) -> None:
    entry = _entry(tmp_path, file_selection={"procedure": "excel_candidates",
                                             "source_field": "path"})
    with pytest.raises(ConversionError, match="procedure is retired"):
        _config(_ctx(tmp_path, entry))


def test_no_directory_enumeration_occurs_during_selection(run_log, tmp_path: Path) -> None:
    """Any directory scan anywhere under selection would trip os.scandir."""
    _one_workbook(tmp_path)
    _source_file(tmp_path, "never_seen.xls")
    ctx = _ctx(tmp_path)
    config = _config(ctx)

    with patch("os.scandir", side_effect=AssertionError("directory enumerated")):
        selection = select_conversion_candidates(ctx, run_log, config)

    assert len(selection.candidates) == 1


def test_generic_code_declares_no_installation_defined_field_names() -> None:
    """Installation variable names live in YAML filters, never in module code.

    The lifecycle ``record_type`` is deliberately excluded: it is a framework
    field the configuration contract requires this module to name, and it is a
    separate concept from the installation-defined ``values.record_type``.
    """
    module = Path(excel_conversion.__file__).read_text(encoding="utf-8")

    for installation_name in (
        "feed",
        "client_alias",
        "data_date",
        "remainder",
        "Hold",
        "Tran",
        "bmo",
        "moody",
        "wolff_popper",
    ):
        # Whole words: the Loader boundary's own Transform / ConvertTransform
        # contain the letters "Tran" without naming the installation's value.
        assert not re.search(rf"\b{installation_name}\b", module), (
            f"{installation_name!r} is installation-defined and must not appear "
            "in generic File Operator code."
        )


def test_generic_code_never_reads_a_named_classification_value() -> None:
    """values is carried whole; indexing it by name would be installation knowledge."""
    module = Path(excel_conversion.__file__).read_text(encoding="utf-8")

    for named_access in ('values.get("', "values.get('", 'values["', "values['"):
        assert named_access not in module, (
            f"{named_access!r} reads one installation-defined variable by name; "
            "carry the complete values mapping instead."
        )


# ---------------------------------------------------------------------------
# Existing conversion lifecycle
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_filesystem_change(run_log, tmp_path: Path) -> None:
    source = _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv"
    ) as convert, patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx), apply=False) == 0

    convert.assert_not_called()
    assert source.exists()
    assert not (tmp_path / "processing").exists()
    assert not (tmp_path / "outbox").exists()
    assert not (tmp_path / "archive").exists()
    assert not (tmp_path / "kickouts").exists()


def test_success_without_archive_leaves_source_in_processing(run_log, 
    tmp_path: Path,
) -> None:
    source = _one_workbook(tmp_path, "book.xls")
    ctx = _ctx(tmp_path, _entry(tmp_path, archive=None))

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert (tmp_path / "processing" / source.name).exists()
    assert not source.exists()


def test_conversion_failure_kicks_the_original_out_to_its_inbox_and_fails_the_step(
    run_log,
    tmp_path: Path,
) -> None:
    """A failing workbook goes to <inbox>/kickouts by the common execution path."""
    source = _one_workbook(tmp_path, "broken.xls")
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=WorkbookOpenError("cannot open", source),
    ):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 1

    assert (source.parent / "kickouts" / source.name).exists()
    assert not (tmp_path / "processing" / source.name).exists()


def test_one_failing_workbook_does_not_stop_the_batch(run_log, tmp_path: Path) -> None:
    good = _source_file(tmp_path, "good.xls")
    bad = _source_file(tmp_path, "bad.xls")
    _selector_rows(tmp_path, _selector_row(1001, good), _selector_row(1011, bad))
    ctx = _ctx(tmp_path)

    def convert(source: Path, outbox: Path, **kwargs: object) -> SimpleNamespace:
        if source.name == "bad.xls":
            raise WorkbookOpenError("cannot open", source)
        return _converter(source, outbox, **kwargs)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv", side_effect=convert,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 1

    assert (bad.parent / "kickouts" / bad.name).exists()
    assert (tmp_path / "archive" / good.name).exists()


def test_conversion_evidence_links_both_lifecycle_records(run_log, tmp_path: Path) -> None:
    source = _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ), patch(
        "rey_lib.load.convert.log_artifact_reference"
    ) as artifact, patch(
        "rey_lib.load.convert.log_input_discovered"
    ) as discovered:
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    evidence = discovered.call_args.kwargs
    assert evidence["classification_record_id"] == 1001
    assert evidence["classification_file_id"] == 1001
    assert evidence["classification_type"] == "file_name_regex"
    assert evidence["classification_values"] == {"group": "alpha"}
    assert evidence["inventory_record_id"] == 1001
    assert evidence["inventory_source_path"] == str(source.resolve())

    metadata = artifact.call_args.kwargs["metadata"]
    assert metadata["classification_record_id"] == 1001
    assert metadata["inventory_record_id"] == 1001


def test_disabled_conversion_is_noop(run_log, tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _entry(tmp_path, enabled=False))

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv"
    ) as convert, patch(
        "rey_lib.load.convert.log_validation_result"
    ) as validation:
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    convert.assert_not_called()
    assert validation.call_args.kwargs["status"] == "skipped"


def test_selecting_nothing_is_no_work_rather_than_a_failure(run_log,
    tmp_path: Path,
) -> None:
    """A routine returning no rows means this run has nothing to convert.

    It used to raise "selected no convertible source", from a time when this
    module discovered files itself and an empty result meant its own filters
    had gone wrong. The routine owns the selection now, and answering with
    nothing is an ordinary answer -- the file on disk is simply not this run's
    work.
    """
    _source_file(tmp_path, "present_but_unclassified.xls")
    _selector_rows(tmp_path)
    ctx = _ctx(tmp_path)

    assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0


def _templated_entry(tmp_path: Path) -> SimpleNamespace:
    return _entry(
        tmp_path,
        processing=str(tmp_path) + "/<classification.values.group>/processing",
        outbox=str(tmp_path) + "/<classification.values.group>/converted_csv",
        archive=None,
    )


def test_destination_placeholders_resolve_per_candidate(run_log, tmp_path: Path) -> None:
    """One conversion entry serves every group; destinations follow the record."""
    alpha = _source_file(tmp_path, "a.xls", group="alpha")
    beta = _source_file(tmp_path, "b.xls", group="beta")
    _selector_rows(
        tmp_path,
        _selector_row(1001, alpha, values={"group": "alpha"}),
        _selector_row(1011, beta, values={"group": "beta"}),
    )
    ctx = _ctx(tmp_path, _templated_entry(tmp_path))

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert, patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    claimed = sorted(str(call.args[0]) for call in convert.call_args_list)
    assert claimed == [
        str(tmp_path / "alpha" / "processing" / "a.xls"),
        str(tmp_path / "beta" / "processing" / "b.xls"),
    ]
    assert sorted(str(call.args[1]) for call in convert.call_args_list) == [
        str(tmp_path / "alpha" / "converted_csv"),
        str(tmp_path / "beta" / "converted_csv"),
    ]


def test_unresolvable_destination_placeholder_rejects_only_that_candidate(run_log, 
    tmp_path: Path,
) -> None:
    good = _source_file(tmp_path, "good.xls", group="alpha")
    bad = _source_file(tmp_path, "bad.xls", group="alpha")
    _selector_rows(
        tmp_path,
        _selector_row(1001, good, values={"group": "alpha"}),
        _selector_row(1011, bad, values={"other": "no_group_key"}),
    )
    ctx = _ctx(tmp_path, _templated_entry(tmp_path))

    selection = select_conversion_candidates(ctx, run_log, _config(ctx))

    assert [item.source_path for item in selection.candidates] == [good.resolve()]
    assert [item.reason_code for item in selection.rejected] == [
        "unresolved_destination_token"
    ]


@pytest.mark.parametrize("template", ["/base/<>/processing", "/base/< values.x >/p"])
def test_malformed_destination_placeholder_fails_configuration(
    tmp_path: Path,
    template: str,
) -> None:
    with pytest.raises(ConversionError, match="placeholder"):
        _config(_ctx(tmp_path, _entry(tmp_path, processing=template)))


def test_source_file_move_mutations_are_appended_to_the_file_manifest(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    source = _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    moves = lifecycle.of_action("move")
    assert [record["result"] for record in moves] == [
        "moved_to_processing",
        "moved_to_archive",
    ]
    first = moves[0]
    assert first["file_id"] == 1001
    assert first["producer"]["operation"] == "excel_conversion"
    assert first["file"]["original_path"] == str(source.resolve())
    assert first["file"]["path"] == str(tmp_path / "processing" / source.name)
    assert "rollback" not in first
    assert first["evidence"]["run_log_id"] > 0


def test_conversion_outputs_are_appended_to_the_file_manifest(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    creates = lifecycle.of_action("create")
    assert len(creates) == 1
    created = creates[0]
    assert created["file"]["path"].endswith(".csv")
    assert created["file"]["file_extension"] == "csv"
    assert created["file_id"] == 1001
    assert created["conversion"]["name"] == "alpha"
    assert "original_path" not in created["file"]
    assert created["evidence"]["run_log_id"] > 0


def test_every_mutation_carries_the_selected_classification_unchanged(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    source = _source_file(tmp_path, "a.xls")
    expected = {
        "type": "MiXeD",
        "source_field": "file.path",
        "values": {"group": "alpha", "Group": "Alpha", "optional": None},
        "future_key": {"kept": True},
    }
    _selector_rows(tmp_path, _selector_row(1001, source, classification=expected))
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert lifecycle.mutations
    assert all(record["classification"] == expected for record in lifecycle.mutations)


def test_create_mutation_declares_its_kind_and_created_extension(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    """The producer states the conversion; a consumer never infers it."""
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    creates = lifecycle.of_action("create")
    assert len(creates) == 1
    assert creates[0]["conversion"] == {
        "operator": "excel_conversion",
        "name": "alpha",
        "source": {"sheet_name": "Sheet 1"},
    }
    assert creates[0]["file"]["file_extension"] == "csv"
    assert creates[0]["status"] == "success"


def test_move_mutations_declare_no_conversion_kind(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    """Only the created file is a conversion; claiming and archiving are moves.

    The moves are the move kind's, through routing, which states the operation
    they served and no conversion section (backlog 624).
    """
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    moves = lifecycle.of_action("move")
    assert moves
    for record in moves:
        assert "conversion" not in record
        assert record["producer"]["operation"] == "excel_conversion"


def test_created_extension_is_declared_not_derived_from_the_output_path(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    """A differently suffixed output still reports the produced type."""
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    def odd_suffix(source: Path, outbox: Path, **_kwargs: object) -> SimpleNamespace:
        output = outbox / f"{source.stem}.Sheet1.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("Account,Amount\nA,1\n", encoding="utf-8")
        return _conversion_result(source, output)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=odd_suffix,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    created = lifecycle.of_action("create")[0]
    assert created["file"]["path"].endswith(".txt")
    # file_extension describes the file the producer actually recorded.
    assert created["file"]["file_extension"] == "txt"


def test_kickout_move_is_recorded_when_conversion_fails(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    source = _one_workbook(tmp_path, "broken.xls")
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=WorkbookOpenError("cannot open", source),
    ):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 1

    assert [
        record["result"] for record in lifecycle.of_action("move")
    ] == ["moved_to_processing", "moved_to_kickouts"]
    # The conversion is recorded as attempted and failed, not left unrecorded:
    # converted_csv carries a failure label for exactly this, and it produced
    # no file, so it claims no path.
    created = lifecycle.of_action("create")
    assert [(record["result"], record["status"]) for record in created] == [
        ("converted_csv", "failed"),
    ]
    assert created[0].get("path") is None
    # Rule 75: the original is kicked out first, then the failure is recorded.
    assert [record.get("result") for record in lifecycle.mutations] == [
        "moved_to_processing", "moved_to_kickouts", "converted_csv",
    ]


def test_uncommitted_move_evidence_prevents_the_manifest_append(run_log, 
    tmp_path: Path,
) -> None:
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    from rey_lib.files.file_routing import FileRoutingEvidenceError
    from rey_lib.files.log_run_rollback import (
        SourceFileMutationEvidenceError,
        SourceFileMutationEvidenceFailurePhase,
    )

    with patch(
        "rey_lib.files.file_routing.log_source_file_mutation",
        side_effect=SourceFileMutationEvidenceError(
            "evidence did not commit",
            phase=SourceFileMutationEvidenceFailurePhase.RUN_LOG_NOT_COMMITTED,
            run_log_id=None,
        ),
    ), patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ) as convert:
        with pytest.raises(FileRoutingEvidenceError, match="evidence did not commit"):
            run_excel_conversion(ctx, run_log, _inline(ctx))

    convert.assert_not_called()


def test_dry_run_appends_no_lifecycle_record(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    assert run_excel_conversion(ctx, run_log, _inline(ctx), apply=False) == 0

    assert lifecycle.mutations == []


def test_tests_locate_the_module_independently_of_the_working_directory() -> None:
    """The neutrality test must not depend on pytest's invocation directory."""
    assert Path(excel_conversion.__file__).is_absolute()
    assert Path(excel_conversion.__file__).is_file()
    assert os.path.isabs(excel_conversion.__file__)


def test_excel_conversion_constructs_no_canonical_record_sections() -> None:
    """The producer supplies values; the serializer owns every section."""
    source = Path(inspect.getsourcefile(excel_conversion))
    body = source.read_text(encoding="utf-8")
    for section in ('"file"', '"evidence"', '"rollback"', '"conversion"',
                    '"result"', '"producer"', '"record_type": "source_file_mutation"'):
        assert f"{section}:" not in body, section
    assert "extra_fields" not in body
    assert "fields={" not in body


def test_conversion_values_reach_the_canonical_sections(run_log, 
    tmp_path: Path,
    lifecycle: _Lifecycle,
) -> None:
    """Sheet, table, and reason are translated by the shared serializer."""
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv",
        side_effect=_converter,
    ), patch("rey_lib.load.convert.log_artifact_reference"):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    created = lifecycle.of_action("create")[0]
    assert created["conversion"]["operator"] == "excel_conversion"
    assert created["conversion"]["name"] == "alpha"
    assert created["conversion"]["source"]["sheet_name"] == "Sheet 1"
    # The producer is the runtime context's application (row 589, step 3).
    assert created["producer"] == {
        "application": "rey_loader",
        "operation": "excel_conversion",
    }
    for legacy in ("mutation_kind", "created_file_extension", "origin_path",
                   "workbook_name", "extraction_kind", "sheet_index",
                   "row_count", "column_count", "source_record_id",
                   "classification_record_id", "conversion_name", "reason"):
        assert legacy not in created, legacy


@pytest.mark.parametrize("declared", ["xls", [], ["xls", "xlsx"]])
def test_declaring_extensions_inline_is_refused(tmp_path: Path, declared: object) -> None:
    """Which files are workbooks is the routine's, however it is declared.

    This used to validate the shape of an inline file_extensions list. There is
    no shape to validate: declaring one at all is refused, because the manifest
    retrieval routine owns which files are this step's work for
    file_selection.operation.
    """
    with pytest.raises(ConversionError, match="must not declare 'file_extensions'"):
        _config(_ctx(tmp_path, _entry(tmp_path, file_extensions=declared)))


def test_selection_without_a_control_fails_at_the_file_operator_boundary(
    run_log, tmp_path: Path,
) -> None:
    """Selection is the database's, so a context that cannot reach it says so."""
    ctx = _ctx(tmp_path)
    ctx.shared_control = None

    with pytest.raises(ConversionError, match="no shared Control"):
        select_conversion_candidates(ctx, run_log, _config(ctx))



# -- what the boundary added (row 589, step 3) ---------------------------------

def _selected_candidate(ctx: SimpleNamespace, run_log) -> object:
    """The one candidate selection resolved, exactly as the step holds it."""
    (candidate,) = select_conversion_candidates(ctx, run_log, _config(ctx)).candidates
    return candidate


def test_convert_is_resolved_through_the_one_resolver(run_log, tmp_path: Path) -> None:
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    resolved = Transform(
        values={"config": _config(ctx), "candidate": _selected_candidate(ctx, run_log)},
        selected="convert",
    ).resolve(ctx)

    assert isinstance(resolved, ConvertTransform)
    assert "convert" not in Transform.kinds()


def test_the_transform_takes_the_resolved_candidate_never_a_row(
    run_log, tmp_path: Path,
) -> None:
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with pytest.raises(ConversionError, match="resolved ConversionCandidate"):
        Transform(
            values={"config": _config(ctx), "candidate": {"file_manifest_id": 1001}},
            selected="convert",
        ).resolve(ctx)


def test_selection_resolves_each_row_once(run_log, tmp_path: Path) -> None:
    """The candidate crosses into the transform whole; nothing re-resolves it."""
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch(
        "rey_lib.load.convert.convert_workbook_to_csv", side_effect=_converter,
    ), patch(
        "rey_lib.load.convert._resolve_candidate",
        wraps=excel_conversion._resolve_candidate,
    ) as resolve:
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert resolve.call_count == 1


def test_converted_csvs_are_returned_at_their_m10_mutations(
    run_log, tmp_path: Path, lifecycle: _Lifecycle,
) -> None:
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)
    candidate = _selected_candidate(ctx, run_log)
    workbook = data_file_for(candidate.source_path, file_manifest_id=1001,
                             file_mutation_id=8001, classification={"type": "t"},
                             base_path="/data/alpha")

    with patch("rey_lib.load.convert.convert_workbook_to_csv", side_effect=_converter):
        (converted,) = Transform(
            values={"config": _config(ctx), "candidate": candidate},
            selected="convert",
        ).resolve(ctx).apply(workbook)

    (m10,) = [m for m in lifecycle.of_action("create")]
    assert converted.path == tmp_path / "outbox" / "a.Sheet1.csv"
    assert converted.file_type == "CSV"
    # Moves (processing, archive) are M11s; the CSV is the create that follows.
    assert converted.file_mutation_id == 100 + lifecycle.mutations.index(m10) + 1
    assert (converted.file_manifest_id, converted.base_path) == (1001, "/data/alpha")


def test_conversion_reads_the_workbook_after_its_processing_move(
    run_log, tmp_path: Path,
) -> None:
    """The ManifestSource path is the selected state; after M11 it is stale."""
    source = _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)
    read_from: list[Path] = []

    def converter(path: Path, outbox: Path, **kwargs: object) -> SimpleNamespace:
        read_from.append(path)
        return _converter(path, outbox, **kwargs)

    with patch("rey_lib.load.convert.convert_workbook_to_csv", side_effect=converter):
        assert run_excel_conversion(ctx, run_log, _inline(ctx)) == 0

    assert read_from == [tmp_path / "processing" / source.name]
    assert read_from[0] != source.resolve()


def test_a_dry_run_never_reaches_the_transform(run_log, tmp_path: Path) -> None:
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch.object(excel_conversion.ConvertTransform, "apply") as apply, \
         patch.object(excel_conversion.ManifestSource, "create") as opened:
        assert run_excel_conversion(ctx, run_log, _inline(ctx), apply=False) == 0

    apply.assert_not_called()
    opened.assert_not_called()


def test_a_workbook_with_no_registered_data_file_type_stops_the_step(
    run_log, tmp_path: Path,
) -> None:
    _one_workbook(tmp_path)
    ctx = _ctx(tmp_path)

    with patch.object(excel_conversion.ManifestSource, "data_file",
                      side_effect=ConfigError("no DataFile for XLS")), \
         patch("rey_lib.load.convert.convert_workbook_to_csv") as converter:
        with pytest.raises(ConversionError, match="no registered DataFile type"):
            run_excel_conversion(ctx, run_log, _inline(ctx))

    converter.assert_not_called()

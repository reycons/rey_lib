"""The file-level Transform foundations (row 589, step 2).

    governed state the caller selected
        -> ManifestSource.data_file()   DataFile carrying its governed identity
        -> Transform(move).resolve()    FileTransform, from the registry
        -> plan(data_file)              where it would go, nothing changed
        -> apply(data_file)             the same file, moved, at its new state
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files import file_routing
from rey_lib.files.data_file import data_file_for
from rey_lib.load import Transform
from rey_lib.load.file_transform import FileTransform, MoveTransform, file_kind
from rey_lib.load.manifest_source import ManifestSource
from rey_lib.load.transform import TRANSFORM_KINDS
from tests.load.test_manifest_source import _row


def _ctx(root: Path) -> SimpleNamespace:
    """The runtime a move needs: an application name and the governed root."""
    return SimpleNamespace(
        app_name="rey_loader",
        paths=SimpleNamespace(resolve=lambda name: str(root)),
    )


def _governed(root: Path, **identity) -> object:
    """The original file in processing, as ManifestSource would build it."""
    source = root / "feed" / "source" / "processing" / "a.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    values = {
        "file_manifest_id": 7,
        "file_mutation_id": 30,
        "classification": {"type": "file_name_regex", "values": {"feed": "feed"}},
        "base_path": str(root / "feed"),
    }
    values.update(identity)
    return data_file_for(source, **values)


def _move(**values) -> Transform:
    held = {"role": "kickouts", "route": "<base_path>/work/kickouts",
            "operation": "record_type_profiling"}
    held.update(values)
    return Transform(values=held, selected="move")


class TestTheGovernedDataFile:
    """A DataFile carries the identity of exactly the state it was built from."""

    def test_an_ungoverned_file_carries_none(self, tmp_path: Path) -> None:
        plain = data_file_for(tmp_path / "a.csv")

        assert (plain.file_manifest_id, plain.file_mutation_id,
                plain.classification, plain.base_path) == (None, None, None, None)

    def test_manifest_source_hands_on_the_selected_state(self) -> None:
        source = ManifestSource(
            [_row(file_mutation_id=31, path="/data/feed/source/processing/a.csv",
                  layout="CSV", classification={"type": "t", "values": {}},
                  base_path="/data/feed")],
            opened_by="mutation",
        )

        built = source.data_file()

        assert built.file_manifest_id == 10
        assert built.file_mutation_id == 31
        assert built.classification == {"type": "t", "values": {}}
        assert built.base_path == "/data/feed"


class TestTheKindRegistry:
    """File kinds resolve through Transform but are never a record-load choice."""

    def test_record_kinds_are_unchanged(self) -> None:
        assert Transform.kinds() == ("identity", "declaration", "yaml", "manifest")
        assert "move" not in [kind.id for kind in TRANSFORM_KINDS]

    def test_move_registers_itself(self) -> None:
        move = file_kind("move")

        assert move is not None
        assert move.fields == ("role", "route", "operation", "name")
        # Only the role is required (backlog 624): kickouts has an intrinsic
        # route, and an unnamed operation is the bound workflow step's.
        assert move.required == ("role",)
        assert move.builder is MoveTransform

    def test_a_move_resolves_to_a_file_transform(self, tmp_path: Path) -> None:
        resolved = _move().resolve(_ctx(tmp_path))

        assert isinstance(resolved, MoveTransform)
        assert isinstance(resolved, FileTransform)

    def test_a_file_kind_has_no_record_declaration(self) -> None:
        with pytest.raises(ConfigError, match="file-level kind"):
            _move().executed_declaration()

    def test_an_incomplete_move_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="needs a route"):
            _move(role="processing", route="").resolve(_ctx(tmp_path))

    def test_an_unknown_role_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="'elsewhere' is not a routing role"):
            _move(role="elsewhere").resolve(_ctx(tmp_path))


class TestTheMove:
    """The move acts on exactly the DataFile it is given."""

    def test_moves_to_the_route_resolved_from_base_path(self, tmp_path: Path) -> None:
        original = _governed(tmp_path)
        destination = tmp_path / "feed" / "work" / "kickouts" / "a.csv"

        with patch.object(file_routing, "move_file", return_value=destination) as move, \
             patch.object(file_routing, "log_source_file_mutation", return_value=41) as evidence:
            (moved,) = _move().resolve(_ctx(tmp_path)).apply(original)

        assert move.call_args.args[:2] == (original.path, tmp_path / "feed" / "work" / "kickouts")
        assert evidence.call_args.kwargs["file_id"] == 7
        assert evidence.call_args.kwargs["operation"] == "record_type_profiling"
        assert evidence.call_args.kwargs["reason"] == "moved_to_kickouts"
        assert moved.path == destination
        assert (moved.file_manifest_id, moved.file_mutation_id) == (7, 41)
        assert moved.base_path == original.base_path
        assert moved.classification == original.classification

    def test_plan_names_the_destination_and_changes_nothing(self, tmp_path: Path) -> None:
        original = _governed(tmp_path)

        with patch.object(file_routing, "move_file") as move, \
             patch.object(file_routing, "log_source_file_mutation") as evidence:
            planned = _move().resolve(_ctx(tmp_path)).plan(original)

        assert planned == tmp_path / "feed" / "work" / "kickouts" / "a.csv"
        move.assert_not_called()
        evidence.assert_not_called()
        assert original.path.exists()

    def test_an_ungoverned_file_is_moved_with_no_mutation(self, tmp_path: Path) -> None:
        """Routing distinguishes persistence, not behaviour (backlog 624)."""
        inbox = tmp_path / "feed" / "source" / "inbox"
        inbox.mkdir(parents=True)
        raw = inbox / "a.csv"
        raw.write_text("a,b\n", encoding="utf-8")
        destination = inbox / "kickouts" / "a.csv"

        with patch.object(file_routing, "move_file", return_value=destination) as move, \
             patch.object(file_routing, "log_source_file_mutation") as evidence:
            (moved,) = Transform(
                values={"role": "kickouts", "operation": "inventory_source_files"},
                selected="move",
            ).resolve(_ctx(tmp_path)).apply(
                data_file_for(raw, inbox=str(inbox), original_path=str(raw)))

        assert move.call_args.args[:2] == (raw, inbox / "kickouts")
        evidence.assert_not_called()
        assert moved.path == destination
        assert moved.file_manifest_id is None

    def test_a_route_needing_base_path_refuses_a_file_without_one(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(ConfigError, match="needs the file's base_path"):
            _move().resolve(_ctx(tmp_path)).apply(_governed(tmp_path, base_path=None))

    def test_a_runtime_without_an_application_name_is_refused(
        self, tmp_path: Path,
    ) -> None:
        ctx = _ctx(tmp_path)
        ctx.app_name = ""

        with pytest.raises(ConfigError, match="app_name"):
            _move().resolve(ctx).apply(_governed(tmp_path))


# -- the one common execution path (backlog 624) -------------------------------

from rey_lib.errors.error_utils import AppError  # noqa: E402
from rey_lib.logs import bind_step, clear_step  # noqa: E402
from rey_lib.load.file_transform import file_transform  # noqa: E402


class _FileFailed(AppError):
    """A terminal failure of the file a test kind operated on."""


@file_transform("test_failing_kind", fields=())
class _FailingKind(FileTransform):
    """A kind whose operation always fails its file."""

    file_failures = (_FileFailed,)

    #: What happened, in order, across the failure path.
    events: list[str] = []

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def _apply(self, data_file):
        raise _FileFailed(f"cannot handle {data_file.path.name}")

    def _record_failure(self, data_file, error):
        _FailingKind.events.append(f"recorded:{error}")


def _sanitized_copy_of_an_original_in_processing(root: Path):
    """A derived copy whose original is in processing, at mutation 30."""
    inbox = root / "feed" / "source" / "inbox"
    original = _governed(root)  # feed/source/processing/a.csv
    copy = root / "feed" / "work" / "sanitized_csv" / "a.csv"
    copy.parent.mkdir(parents=True)
    copy.write_text("a,b\n", encoding="utf-8")
    return data_file_for(
        copy, file_manifest_id=7, file_mutation_id=55,
        classification=original.classification, base_path=original.base_path,
        inbox=str(inbox), original_path=str(original.path), original_mutation_id=30,
    ), original


class TestTheCommonExecutionPath:
    """A kind's terminal file failure moves the ORIGINAL to <inbox>/kickouts."""

    def test_the_original_is_kicked_out_and_the_failure_raised_again(
        self, tmp_path: Path,
    ) -> None:
        failing, original = _sanitized_copy_of_an_original_in_processing(tmp_path)
        kickouts = tmp_path / "feed" / "source" / "inbox" / "kickouts"
        bind_step(step_id="profile_csv_record_types")
        try:
            with patch.object(file_routing, "move_file",
                              return_value=kickouts / "a.csv") as move, \
                 patch.object(file_routing, "log_source_file_mutation",
                              return_value=88) as evidence:
                with pytest.raises(_FileFailed, match="cannot handle a.csv"):
                    Transform(values={}, selected="test_failing_kind").resolve(
                        _ctx(tmp_path)).apply(failing)
        finally:
            clear_step()

        # The ORIGINAL moved, from where it is, to its inbox's kickouts --
        # never the copy the kind failed on.
        assert move.call_args.args[:2] == (original.path, kickouts)
        assert evidence.call_args.kwargs["reason"] == "moved_to_kickouts"
        assert evidence.call_args.kwargs["operation"] == "profile_csv_record_types"
        assert evidence.call_args.kwargs["run_log_fields"] == {"source_record_id": 30}

    def test_the_kind_records_its_failure_after_the_kickout(self, tmp_path: Path) -> None:
        """Rule 75 (4): kicked out first, then the kind's own record, then raise."""
        failing, _original = _sanitized_copy_of_an_original_in_processing(tmp_path)
        kickouts = tmp_path / "feed" / "source" / "inbox" / "kickouts"
        _FailingKind.events = []

        def moved(*_a, **_k):
            _FailingKind.events.append("moved")
            return kickouts / "a.csv"

        bind_step(step_id="profile_csv_record_types")
        try:
            with patch.object(file_routing, "move_file", side_effect=moved), \
                 patch.object(file_routing, "log_source_file_mutation", return_value=88):
                with pytest.raises(_FileFailed):
                    Transform(values={}, selected="test_failing_kind").resolve(
                        _ctx(tmp_path)).apply(failing)
        finally:
            clear_step()

        assert _FailingKind.events == ["moved", "recorded:cannot handle a.csv"]

    def test_the_failure_is_recorded_even_when_the_kickout_fails(
        self, tmp_path: Path,
    ) -> None:
        """A move that cannot be made must not lose the failure's evidence."""
        failing, _original = _sanitized_copy_of_an_original_in_processing(tmp_path)
        _FailingKind.events = []

        bind_step(step_id="profile_csv_record_types")
        try:
            with patch.object(file_routing, "move_file",
                              side_effect=OSError("disk full")), \
                 patch.object(file_routing, "log_source_file_mutation", return_value=88):
                with pytest.raises(_FileFailed, match="cannot handle a.csv"):
                    Transform(values={}, selected="test_failing_kind").resolve(
                        _ctx(tmp_path)).apply(failing)
        finally:
            clear_step()

        assert _FailingKind.events == ["recorded:cannot handle a.csv"]

    def test_a_kind_that_defines_apply_is_refused(self) -> None:
        with pytest.raises(TypeError, match="defines apply"):
            class _Bypassing(FileTransform):  # noqa: F841
                def apply(self, data_file):
                    return ()

                def _apply(self, data_file):
                    return ()

    def test_a_failed_move_is_not_kicked_out_again(self, tmp_path: Path) -> None:
        original = _governed(tmp_path, inbox=str(tmp_path / "feed" / "source" / "inbox"))

        with patch.object(file_routing, "move_file",
                          side_effect=OSError("disk full")) as move, \
             patch.object(file_routing, "log_source_file_mutation", return_value=1):
            with pytest.raises(file_routing.FileRoutingError):
                _move(route=None).resolve(_ctx(tmp_path)).apply(original)

        assert move.call_count == 1

    def test_kickouts_route_is_intrinsic(self, tmp_path: Path) -> None:
        inbox = tmp_path / "feed" / "source" / "inbox"
        original = _governed(tmp_path, inbox=str(inbox))

        planned = Transform(values={"role": "kickouts", "operation": "x"},
                            selected="move").resolve(_ctx(tmp_path)).plan(original)

        assert planned == inbox / "kickouts" / "a.csv"

    def test_an_unnamed_operation_with_no_bound_step_is_refused(self, tmp_path: Path) -> None:
        original = _governed(tmp_path, inbox=str(tmp_path / "feed" / "source" / "inbox"))
        clear_step()

        with pytest.raises(ConfigError, match="no workflow step is bound"):
            Transform(values={"role": "kickouts"}, selected="move").resolve(
                _ctx(tmp_path)).plan(original)

    def test_moving_the_original_moves_its_facts(self, tmp_path: Path) -> None:
        original = _governed(tmp_path, original_path=None)
        destination = tmp_path / "feed" / "work" / "kickouts" / "a.csv"

        with patch.object(file_routing, "move_file", return_value=destination), \
             patch.object(file_routing, "log_source_file_mutation", return_value=41):
            (moved,) = _move().resolve(_ctx(tmp_path)).apply(original)

        assert (moved.original_path, moved.original_mutation_id) == (str(destination), 41)

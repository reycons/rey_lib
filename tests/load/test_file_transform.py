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
        assert move.fields == ("role", "route", "operation")
        assert move.builder is MoveTransform

    def test_a_move_resolves_to_a_file_transform(self, tmp_path: Path) -> None:
        resolved = _move().resolve(_ctx(tmp_path))

        assert isinstance(resolved, MoveTransform)
        assert isinstance(resolved, FileTransform)

    def test_a_file_kind_has_no_record_declaration(self) -> None:
        with pytest.raises(ConfigError, match="file-level kind"):
            _move().executed_declaration()

    def test_an_incomplete_move_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="route"):
            _move(route="").resolve(_ctx(tmp_path))

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

    def test_an_ungoverned_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no governed identity"):
            _move().resolve(_ctx(tmp_path)).apply(data_file_for(tmp_path / "a.csv"))

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

"""The Target: one canonical object for where a load's records go.

What is asserted is the contract every entry point relies on -- which kinds
exist, what a selection keeps, what the write policy is, what validation
reports, that the declarative form round-trips, and that resolution goes
through the existing resolution points with their own call shapes and leaves
nothing live behind. And the load shape: routing from two selected kinds, and
nothing else.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file.base import DataFile
from rey_lib.load import Source, Target, derived_load_shape
from rey_lib.load import load_operation
from rey_lib.load import shape as shape_module
from rey_lib.load.target import TARGET_FIELDS, TARGET_PARAMETERS

_TABLE = {"connection": "warehouse", "table": "landing.orders"}

Calls = list[tuple[str, tuple[Any, ...], dict[str, Any]]]


@pytest.fixture()
def builds(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Wrap the two real resolution points, recording each call as it was made."""
    calls: Calls = []
    for name in ("_destination_identity", "_build_data_loader"):
        real = getattr(load_operation, name)

        def spy(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
            calls.append((_name, args, kwargs))
            return _real(*args, **kwargs)

        monkeypatch.setattr(load_operation, name, spy)
    return calls


class TestTheVocabulary:

    def test_the_kinds_are_the_two(self) -> None:
        assert Target.kinds() == ("database", "file")

    def test_the_fields_are_the_loaders_destination_parameters(self) -> None:
        assert TARGET_FIELDS == (
            "connection", "table", "create", "replace", "recreate", "append", "out-file",
        )
        assert TARGET_PARAMETERS == TARGET_FIELDS


class TestTheSelection:

    def test_nothing_said_is_a_database_table(self) -> None:
        assert Target().selected_kind() == "database"

    def test_a_choice_wins(self) -> None:
        assert Target(_TABLE, selected="file").selected_kind() == "file"

    def test_unchosen_it_is_the_kind_whose_own_fields_carry_values(self) -> None:
        assert Target({"out-file": "/out/rows.csv"}).selected_kind() == "file"

    def test_a_false_flag_decides_nothing(self) -> None:
        # `append: false` held under the database kind must not raise it over a
        # file that was actually named.
        target = Target({"append": False, "create": "false", "out-file": "/o.csv"})

        assert target.selected_kind() == "file"

    def test_switching_kinds_keeps_what_was_said_under_the_other(self) -> None:
        target = Target({**_TABLE, "out-file": "/o.csv"}, selected="file")

        target.select("database")

        assert target.configuration()["table"] == "landing.orders"
        assert target.value("out-file") == "/o.csv"

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no target kind"):
            Target().select("queue")

    def test_a_field_that_is_not_a_target_field_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a target field"):
            Target().update("statement", "select 1")


class TestTheWritePolicy:

    @pytest.mark.parametrize("mode", ["create", "replace", "recreate", "append"])
    def test_each_flag_gives_its_policy(self, mode: str) -> None:
        assert Target({**_TABLE, mode: True}).write_policy() == mode

    def test_no_flag_is_append(self) -> None:
        assert Target(_TABLE).write_policy() == "append"


class TestSettingTheWritePolicy:

    @pytest.mark.parametrize("mode", ["create", "replace", "recreate", "append"])
    def test_it_sets_exactly_one_mode(self, mode: str) -> None:
        target = Target({**_TABLE, "create": True, "replace": True})

        target.set_write_policy(mode)

        assert target.modes_given() == [mode]
        assert target.write_policy() == mode
        assert target.validate() == []

    def test_a_name_that_is_not_a_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a write mode"):
            Target(_TABLE).set_write_policy("truncate")

    def test_it_round_trips(self) -> None:
        target = Target(_TABLE)
        target.set_write_policy("recreate")

        again = Target.from_declaration(target.declaration())

        assert again.write_policy() == "recreate"


class TestValidation:

    @pytest.mark.parametrize(("values", "kind", "needs"), [
        ({}, "database", ["connection", "table"]),
        ({"connection": "w"}, "database", ["table"]),
        (_TABLE, "database", []),
        ({}, "file", ["out-file"]),
        ({"out-file": "/o.csv"}, "file", []),
    ])
    def test_each_kind_names_what_it_still_needs(
        self, values: dict[str, Any], kind: str, needs: list[str],
    ) -> None:
        assert Target(values, selected=kind).validate() == needs

    def test_two_modes_together_are_reported_and_not_raised(self) -> None:
        problems = Target({**_TABLE, "create": True, "append": True}).validate()

        assert problems == ["one mode, not create, append together"]

    def test_modes_under_a_file_target_are_not_its_business(self) -> None:
        target = Target({"create": True, "replace": True, "out-file": "/o.csv"}, selected="file")

        assert target.validate() == []


class TestTheDeclarativeForm:

    def test_it_round_trips(self) -> None:
        target = Target({**_TABLE, "replace": True, "out-file": "/o.csv"}, selected="database")

        again = Target.from_declaration(target.declaration())

        assert again.declaration() == target.declaration()

    def test_it_holds_nothing_live(self) -> None:
        declared = Target({**_TABLE, "recreate": True}).declaration()

        assert json.loads(json.dumps(declared)) == declared


class TestResolution:

    @pytest.mark.parametrize(("flags", "policy"), [
        ({"create": True}, (True, False, False)),
        ({"replace": True}, (False, True, False)),
        ({"recreate": True}, (False, False, True)),
    ])
    def test_a_table_resolves_through_the_two_resolution_points(
        self, builds: Calls, flags: dict[str, bool], policy: tuple[bool, bool, bool],
    ) -> None:
        ctx = SimpleNamespace()

        identity, loader = Target({**_TABLE, **flags}).resolve(ctx)

        assert builds == [
            ("_destination_identity", ("landing.orders", "warehouse"), {}),
            ("_build_data_loader", (ctx, *policy), {}),
        ]
        assert isinstance(identity, DatabaseObjectIdentity)
        assert (identity.connection, identity.schema, identity.name) == (
            "warehouse", "landing", "orders",
        )
        assert (
            loader.create_destination, loader.replace_destination,
            loader.recreate_destination,
        ) == policy

    @pytest.mark.parametrize("flags", [{"append": True}, {}])
    def test_append_and_no_mode_both_build_every_flag_false(
        self, builds: Calls, flags: dict[str, bool],
    ) -> None:
        ctx = SimpleNamespace()

        _, loader = Target({**_TABLE, **flags}).resolve(ctx)

        # APPEND IS THE DEFAULT SPELLED: no fourth argument, the builder unchanged.
        assert builds[-1] == ("_build_data_loader", (ctx, False, False, False), {})
        assert Target({**_TABLE, **flags}).write_policy() == "append"
        assert not (
            loader.create_destination or loader.replace_destination
            or loader.recreate_destination
        )

    def test_a_file_resolves_to_its_data_file(self) -> None:
        resolved = Target({"out-file": "/out/rows.csv"}).resolve(SimpleNamespace())

        assert isinstance(resolved, DataFile)

    def test_a_suffix_that_names_no_format_is_refused(self) -> None:
        with pytest.raises(ConfigError):
            Target({"out-file": "/out/rows.nothing"}).resolve(SimpleNamespace())

    @pytest.mark.parametrize("values", [
        {"connection": "w"},
        {**_TABLE, "create": True, "replace": True},
    ])
    def test_resolve_refuses_before_invoking_any_builder(
        self, builds: Calls, values: dict[str, Any],
    ) -> None:
        with pytest.raises(ConfigError, match="not complete"):
            Target(values).resolve(SimpleNamespace())
        assert builds == []

    def test_nothing_is_left_live_on_the_target(self) -> None:
        target = Target({**_TABLE, "create": True})
        before = dict(vars(target))

        target.resolve(SimpleNamespace())

        assert dict(vars(target)) == before


class TestTheDerivedLoadShape:

    @pytest.mark.parametrize(("source_kind", "target_kind", "shape"), [
        ("file", "database", "direct"),
        ("database", "database", "query"),
        ("sql_file", "database", "query_file"),
        ("manifest", "database", "manifest"),
        ("database", "file", "query_to_file"),
    ])
    def test_each_supported_pair(self, source_kind: str, target_kind: str, shape: str) -> None:
        assert derived_load_shape(
            Source(selected=source_kind), Target(selected=target_kind),
        ) == shape

    @pytest.mark.parametrize(("source_kind", "target_kind"), [
        ("file", "file"), ("sql_file", "file"), ("manifest", "file"),
    ])
    def test_an_unsupported_pair_is_none(self, source_kind: str, target_kind: str) -> None:
        assert derived_load_shape(
            Source(selected=source_kind), Target(selected=target_kind),
        ) is None

    def test_it_routes_an_incomplete_pair_and_never_validates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def refuse(_self: Any) -> list[str]:
            raise AssertionError("derived_load_shape must not validate")

        monkeypatch.setattr(Source, "validate", refuse)
        monkeypatch.setattr(Target, "validate", refuse)

        # Nothing named on either side: still a route.
        assert derived_load_shape(Source(selected="database"), Target()) == "query"

    def test_the_shapes_are_the_five_movements(self) -> None:
        assert set(shape_module.LOAD_SHAPES.values()) == {
            "direct", "query", "query_file", "manifest", "query_to_file",
        }

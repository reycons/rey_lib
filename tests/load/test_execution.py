"""A load executed through the three canonical objects.

Asserted: every object validates through its own contract before anything
resolves; routing refuses a pair with no implemented path without judging it;
each object resolves itself; and the transfer boundary is reached with exactly
what the existing builders hand it, for every movement they support.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.db.query_source import QuerySource
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file.base import DataFile
from rey_lib.load import Source, Target, Transform, run_selected_load
from rey_lib.load import load_operation
from rey_lib.load import source as source_module

_DECLARED = {"columns": [{"source": "a", "name": "a"}]}

Seam = list[tuple[tuple[Any, ...], dict[str, Any]]]


@pytest.fixture()
def seam(monkeypatch: pytest.MonkeyPatch) -> Seam:
    """The transfer boundary, recorded instead of run."""
    calls: Seam = []

    def record(*args: Any, **kwargs: Any) -> int:
        calls.append((args, kwargs))
        return 7

    monkeypatch.setattr(load_operation, "_load_one_file", record)
    return calls


@pytest.fixture(autouse=True)
def connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured connections, as named handles: one per name, both paths."""
    def shared_connection(_ctx: Any, name: str) -> Any:
        return SimpleNamespace(handle=lambda: f"handle:{name}")

    monkeypatch.setattr(source_module, "shared_connection", shared_connection)
    monkeypatch.setattr(load_operation, "shared_connection", shared_connection)


@pytest.fixture()
def csv_file(tmp_path: Path) -> Path:
    held = tmp_path / "in.csv"
    held.write_text("a\n1\n", encoding="utf-8")
    return held


def _describe(value: Any) -> Any:
    """What one seam argument IS, for comparing two calls."""
    if isinstance(value, DataFile):
        return (type(value).__name__, str(value.path), value.encoding)
    if isinstance(value, QuerySource):
        return ("QuerySource", value.conn, value.statement, value.adapter)
    if isinstance(value, DatabaseObjectIdentity):
        return value
    if hasattr(value, "create_destination"):
        return ("DataLoader", value.create_destination, value.replace_destination,
                value.recreate_destination)
    if hasattr(value, "columns_for_names"):
        return (type(value).__name__, getattr(value, "columns", None))
    return value


def _call(seam_call: tuple[tuple[Any, ...], dict[str, Any]]) -> dict[str, Any]:
    args, kwargs = seam_call
    return {
        "source": _describe(args[0]), "transform": _describe(args[1]),
        "target": _describe(args[2]),
        **{key: _describe(value) for key, value in kwargs.items()},
    }


class TestValidationComesFirst:

    @pytest.mark.parametrize(("source", "transform", "target", "named"), [
        (Source(selected="database"), Transform(), Target({"table": "s.t", "connection": "w"}),
         "Source"),
        (Source({"file": "/x.csv"}), Transform(selected="yaml"),
         Target({"table": "s.t", "connection": "w"}), "Transform"),
        (Source({"file": "/x.csv"}), Transform(), Target({"connection": "w"}), "Target"),
        (Source({"file": "/x.csv"}), Transform(),
         Target({"table": "s.t", "connection": "w", "create": True, "replace": True}), "Target"),
    ])
    def test_an_incomplete_object_is_refused_before_anything_resolves(
        self, seam: Seam, monkeypatch: pytest.MonkeyPatch,
        source: Source, transform: Transform, target: Target, named: str,
    ) -> None:
        resolved: list[str] = []
        for cls in (Source, Transform, Target):
            real = cls.resolve

            def spy(self: Any, *a: Any, _real: Any = real, _name: str = cls.__name__,
                    **k: Any) -> Any:
                resolved.append(_name)
                return _real(self, *a, **k)

            monkeypatch.setattr(cls, "resolve", spy)

        with pytest.raises(ConfigError, match=named):
            run_selected_load(SimpleNamespace(), None, source, transform, target)
        assert resolved == [] and seam == []

    def test_all_three_validate_through_their_own_contracts(
        self, seam: Seam, monkeypatch: pytest.MonkeyPatch, csv_file: Path,
    ) -> None:
        asked: list[str] = []
        for cls in (Source, Transform, Target):
            real = cls.validate

            def spy(self: Any, _real: Any = real, _name: str = cls.__name__) -> Any:
                asked.append(_name)
                return _real(self)

            monkeypatch.setattr(cls, "validate", spy)

        run_selected_load(SimpleNamespace(), None, Source({"file": str(csv_file)}), Transform(),
                          Target({"table": "s.t", "connection": "w"}))

        assert set(asked) >= {"Source", "Transform", "Target"}


class TestRouting:

    def test_a_pair_with_no_implemented_path_is_refused_as_such(self, seam: Seam) -> None:
        with pytest.raises(ConfigError, match="No execution path is implemented"):
            run_selected_load(SimpleNamespace(), None, Source({"file": "/x.csv"}), Transform(),
                              Target({"out-file": "/o.csv"}))
        assert seam == []

    def test_a_governed_file_with_no_reader_is_refused_by_the_source(self, seam: Seam) -> None:
        with pytest.raises(ConfigError, match="reader"):
            run_selected_load(SimpleNamespace(), None, Source({"file-manifest-id": "10"}),
                              Transform(), Target({"table": "s.t", "connection": "w"}))
        assert seam == []

    def test_a_missing_file_source_is_refused(self, seam: Seam, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no such file"):
            run_selected_load(SimpleNamespace(), None, Source({"file": str(tmp_path / "no.csv")}),
                              Transform(), Target({"table": "s.t", "connection": "w"}))
        assert seam == []


class TestTheResolvedObjectsReachTheBoundary:

    @pytest.mark.parametrize(("flags", "policy"), [
        ({}, (False, False, False)),
        ({"append": True}, (False, False, False)),
        ({"create": True}, (True, False, False)),
        ({"replace": True}, (False, True, False)),
        ({"recreate": True}, (False, False, True)),
    ])
    def test_the_loader_carries_the_write_policy(
        self, seam: Seam, csv_file: Path, flags: dict[str, bool], policy: tuple,
    ) -> None:
        run_selected_load(SimpleNamespace(), None, Source({"file": str(csv_file)}), Transform(),
                          Target({"table": "s.t", "connection": "w", **flags}))

        (_, kwargs), = seam
        assert _describe(kwargs["loader"])[1:] == policy

    def test_nothing_resolved_is_kept(self, seam: Seam, csv_file: Path) -> None:
        objects = (Source({"file": str(csv_file)}), Transform({"transform": json.dumps(_DECLARED)}),
                   Target({"table": "s.t", "connection": "w"}))
        before = [dict(vars(one)) for one in objects]

        run_selected_load(SimpleNamespace(), None, *objects)

        assert [dict(vars(one)) for one in objects] == before


class TestSeamEquivalence:
    """For each supported movement, the same boundary call as the existing builder."""

    def test_direct(self, seam: Seam, csv_file: Path) -> None:
        ctx, run_log = SimpleNamespace(), object()

        load_operation.load_file_to_table(
            ctx, run_log, csv_file, "s.t", "w", replace_destination=True,
            file_type="csv", transform=_DECLARED,
        )
        run_selected_load(
            ctx, run_log, Source({"file": str(csv_file), "file-type": "csv"}),
            Transform({"transform": json.dumps(_DECLARED)}),
            Target({"table": "s.t", "connection": "w", "replace": True}),
        )

        builder, canonical = seam
        assert _call(canonical) == _call(builder)

    @pytest.mark.parametrize("kind", ["database", "sql_file"])
    def test_query_to_table(self, seam: Seam, tmp_path: Path, kind: str) -> None:
        ctx, run_log = SimpleNamespace(), object()
        query = tmp_path / "q.sql"
        query.write_text("select 1 as a", encoding="utf-8")
        values = (
            {"statement": "select 1 as a"} if kind == "database" else {"sql-file": str(query)}
        )

        load_operation.load_query_to_table(
            ctx, run_log, "select 1 as a", "src", "s.t", "w", create_destination=True,
        )
        run_selected_load(
            ctx, run_log, Source({**values, "source-connection": "src"}, selected=kind),
            Transform(), Target({"table": "s.t", "connection": "w", "create": True}),
        )

        builder, canonical = seam
        assert _call(canonical) == _call(builder)

    def test_query_to_file(self, seam: Seam, tmp_path: Path) -> None:
        ctx, run_log = SimpleNamespace(), object()
        out = str(tmp_path / "out.csv")

        load_operation.load_query_to_file(
            ctx, run_log, "select 1 as a", "src", out, transform=_DECLARED,
        )
        run_selected_load(
            ctx, run_log,
            Source({"statement": "select 1 as a", "source-connection": "src"},
                   selected="database"),
            Transform({"transform": json.dumps(_DECLARED)}),
            Target({"out-file": out}, selected="file"),
        )

        builder, canonical = seam
        assert _call(canonical) == _call(builder)

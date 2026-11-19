"""The database index and the bridge are promoted through the code procedure map (#541).

No database is opened. Each writer stages through its tables and then asks the
``code`` map's binding for the promotion -- governed and logged in the run --
and never assembles a CALL in Python.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.repository_map import bridge_index, database_index


class _Recording:
    """An adapter that records statements and staged tables instead of connecting."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.staged: list[str] = []

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return f'"{name}"'

    def execute_statement(self, _conn: Any, sql_text: str) -> int:
        self.statements.append(sql_text)
        return 0

    def execute_sql(self, _conn: Any, sql_text: str, *_rest: Any) -> None:
        self.statements.append(sql_text)

    def bulk_insert(self, _conn: Any, _schema: str, table: str, rows: list,
                    _columns: list) -> int:
        self.staged.append(table)
        return len(rows)


def _routed(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple]:
    """The module's procedure-map calls, recorded in order."""
    calls: list[tuple] = []

    class _ProcedureMap:
        def __init__(self, map_name: str, action: str) -> None:
            self.map_name, self.action = map_name, action

        @classmethod
        def resolve(cls, _conn: Any, _map_cfg: Any, map_name: str, action: str) -> Any:
            return cls(map_name, action)

        def call(self, _conn: Any, values: dict, run_log: Any, run_ctx: Any = None) -> dict:
            calls.append((self.map_name, self.action, dict(values), run_log))
            return {"outputs": {}, "rows": None}

    monkeypatch.setattr(module, "ProcedureMap", _ProcedureMap)
    monkeypatch.setattr(module, "get_procedure_map", lambda _ctx, name: object())
    return calls


def test_the_database_index_is_promoted_through_the_code_map(monkeypatch) -> None:
    calls = _routed(monkeypatch, database_index)
    adapter, run_log = _Recording(), object()

    database_index.DatabaseIndexWriter(adapter, None, object(), run_log).replace_index(
        source_key="rey_apps", provider="postgres", catalog_name="rey_apps",
        generator_version="1", objects=[], members=[], observations=[])

    assert calls == [("code", "publish_database_index", {}, run_log)]
    assert not [s for s in adapter.statements if s.startswith("CALL")]
    assert adapter.staged == ["db_source_stage"]


def test_the_bridge_is_promoted_through_the_code_map(monkeypatch) -> None:
    calls = _routed(monkeypatch, bridge_index)
    adapter, run_log = _Recording(), object()

    bridge_index.BridgeIndexWriter(adapter, None, object(), run_log).replace_index(
        {"code": [], "bindings": [], "coverage": []})

    assert calls == [("code", "publish_bridge_index", {}, run_log)]
    assert not [s for s in adapter.statements if s.startswith("CALL")]

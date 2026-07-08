"""Tests for the shared db_utils operation executor (routine / mapped / ad hoc SQL).

Covers SGC_Update_Procedure_Map_Handling_In_DB_Utils and
SGC_DB_Utils_Mapped_And_Adhoc_SQL_Execution: binding parsing/lookup, legacy
call_type normalization, the three execution targets (routine, mapped_sql,
adhoc_sql) and result modes (no_return, scalar_result, dataset_result), input
binding, output loading, fail-closed rules, the ad hoc interpolation guard, and
the named-SQL adapter primitive.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rey_lib.db.postgres_utils import execute_named_sql
from rey_lib.db.procedure_map import (
    execute_mapped_routine,
    execute_operation,
    execute_procedure_call,
    execute_sql_text,
    get_procedure_map,
    resolve_routine_binding,
    resolve_sql_binding,
)
from rey_lib.errors.error_utils import ConfigError, DatabaseError


def _log_ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        log_file=str(tmp_path / "procedure_map.run.jsonl"),
        owner_app_name="test_app",
    )


def _run_records(ctx: SimpleNamespace) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(ctx.run_log_path).read_text(encoding="utf-8").splitlines()
    ]


def _rb(name="start_batch", routine="control.f_start_batch", call_type=None,
        routine_type=None, result_mode=None, output=None, inp=None):
    binding = {"name": name, "routine": routine}
    if call_type:
        binding["call_type"] = call_type
    if routine_type:
        binding["routine_type"] = routine_type
    if result_mode:
        binding["result_mode"] = result_mode
    if output is not None:
        binding["output"] = output
    if inp is not None:
        binding["input"] = inp
    return binding


def _map(*routine_bindings, sql_bindings=None):
    m = {"name": "control", "connection_name": "control",
         "routine_bindings": list(routine_bindings)}
    if sql_bindings is not None:
        m["sql_bindings"] = sql_bindings
    return m


def _sb(name="start_batch", sql="INSERT INTO control.batch VALUES (:run_id) RETURNING batch_id",
        result_mode="scalar_result", output=None, inp=None):
    binding = {"name": name, "execution_target": "mapped_sql", "sql": sql,
               "result_mode": result_mode}
    if output is not None:
        binding["output"] = output
    if inp is not None:
        binding["input"] = inp
    return binding


# ---------------------------------------------------------------------------
# Routine binding resolution + legacy normalization
# ---------------------------------------------------------------------------

def test_routine_binding_normalizes_legacy_call_type():
    m = _map(_rb(name="start_batch", call_type="function_with_return",
                 output={"variable": "batch_id", "load_to_ctx": "batch_id"},
                 inp={"p_run_id": "run_id"}))
    b = resolve_routine_binding(m, "control", "start_batch")
    assert b["execution_target"] == "routine"
    assert b["routine_type"] == "function"
    assert b["result_mode"] == "scalar_result"
    assert b["output"] == {"variable": "batch_id", "load_to_ctx": "batch_id"}
    assert b["inputs"] == {"p_run_id": "run_id"}


def test_routine_binding_accepts_new_fields():
    m = _map({"name": "x", "routine": "control.p_end", "routine_type": "procedure",
              "result_mode": "no_return"})
    b = resolve_routine_binding(m, "control", "x")
    assert b["routine_type"] == "procedure" and b["result_mode"] == "no_return"


def test_duplicate_binding_names_fail_closed():
    m = _map(_rb(name="x", call_type="procedure_no_return"),
             _rb(name="x", call_type="procedure_no_return"))
    with pytest.raises(ConfigError, match="duplicate"):
        resolve_routine_binding(m, "control", "x")


def test_missing_routine_bindings_fails_closed():
    with pytest.raises(ConfigError, match="no 'routine_bindings'"):
        resolve_routine_binding({"name": "control"}, "control", "x")


def test_binding_without_name_fails_closed():
    m = {"routine_bindings": [{"routine": "r", "call_type": "procedure_no_return"}]}
    with pytest.raises(ConfigError, match="without a 'name'"):
        resolve_routine_binding(m, "control", "x")


def test_routine_not_found_fails_closed():
    m = _map(_rb(name="a", call_type="procedure_no_return"))
    with pytest.raises(ConfigError, match="not found"):
        resolve_routine_binding(m, "control", "zzz")


def test_missing_routine_fails_closed():
    m = {"routine_bindings": [{"name": "a", "call_type": "procedure_no_return"}]}
    with pytest.raises(ConfigError, match="missing 'routine'"):
        resolve_routine_binding(m, "control", "a")


def test_missing_result_mode_fails_closed():
    m = {"routine_bindings": [{"name": "a", "routine": "r"}]}
    with pytest.raises(ConfigError, match="result_mode"):
        resolve_routine_binding(m, "control", "a")


def test_unsupported_call_type_fails_closed():
    m = _map(_rb(name="a", call_type="weird"))
    with pytest.raises(ConfigError, match="unsupported call_type"):
        resolve_routine_binding(m, "control", "a")


def test_scalar_result_requires_output_variable():
    m = _map(_rb(name="a", call_type="function_with_return"))
    with pytest.raises(ConfigError, match="output.variable"):
        resolve_routine_binding(m, "control", "a")


def test_input_and_inputs_spellings_both_supported():
    m1 = _map(_rb(name="a", routine="r", call_type="procedure_no_return", inp={"p_x": "x"}))
    assert resolve_routine_binding(m1, "control", "a")["inputs"] == {"p_x": "x"}
    m2 = {"routine_bindings": [{"name": "a", "routine": "r",
                               "call_type": "procedure_no_return",
                               "inputs": {"p_x": "x"}}]}
    assert resolve_routine_binding(m2, "control", "a")["inputs"] == {"p_x": "x"}


def test_legacy_actions_normalize_inside_db_utils():
    m = {"name": "control", "actions": {
        "start_batch": {"routine": "control.f_start_batch", "call_type": "function",
                        "return_variable": "batch_id", "inputs": {"p_run_id": "run_id"}}}}
    b = resolve_routine_binding(m, "control", "start_batch")
    assert b["result_mode"] == "scalar_result" and b["routine_type"] == "function"
    assert b["output"] == {"variable": "batch_id", "load_to_ctx": "batch_id"}


# ---------------------------------------------------------------------------
# Mapped-SQL binding resolution
# ---------------------------------------------------------------------------

def test_sql_binding_lookup():
    m = {"name": "control", "sql_bindings": [
        _sb(name="start_batch", sql="INSERT ... RETURNING batch_id",
            result_mode="scalar_result", output={"variable": "batch_id"})]}
    b = resolve_sql_binding(m, "control", "start_batch")
    assert b["execution_target"] == "mapped_sql"
    assert b["sql"] == "INSERT ... RETURNING batch_id"
    assert b["result_mode"] == "scalar_result"


def test_sql_binding_missing_sql_fails_closed():
    m = {"sql_bindings": [{"name": "a", "result_mode": "no_return"}]}
    with pytest.raises(ConfigError, match="missing 'sql'"):
        resolve_sql_binding(m, "control", "a")


def test_sql_binding_scalar_requires_output():
    m = {"sql_bindings": [_sb(name="a", sql="SELECT 1", result_mode="scalar_result")]}
    with pytest.raises(ConfigError, match="output.variable"):
        resolve_sql_binding(m, "control", "a")


# ---------------------------------------------------------------------------
# execute_mapped_routine (routine target)
# ---------------------------------------------------------------------------

def test_routine_scalar_executes_and_loads_output():
    conn = object()
    m = _map(_rb(name="start_batch", call_type="function_with_return",
                 output={"variable": "batch_id", "load_to_ctx": "batch_id"},
                 inp={"p_run_id": "run_id"}))
    values = {"run_id": "R1"}
    run_ctx = SimpleNamespace()
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db") as db:
        db.execute_function.return_value = 123
        result = execute_mapped_routine(object(), conn, "control", "start_batch",
                                        values, run_ctx=run_ctx)
    db.execute_function.assert_called_once_with(conn, "control.f_start_batch",
                                                {"p_run_id": "R1"})
    assert values["batch_id"] == 123 and run_ctx.batch_id == 123
    assert result["outputs"] == {"batch_id": 123}
    assert result["result_mode"] == "scalar_result"


def test_routine_execution_logs_sql_execution_evidence(tmp_path: Path):
    ctx = _log_ctx(tmp_path)
    conn = object()
    m = _map(_rb(name="start_batch", call_type="function_with_return",
                 output={"variable": "batch_id", "load_to_ctx": "batch_id"},
                 inp={"p_run_id": "run_id"}))
    run_ctx = SimpleNamespace()
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db") as db:
        db.execute_function.return_value = 123
        execute_mapped_routine(ctx, conn, "control", "start_batch",
                               {"run_id": "R1"}, run_ctx=run_ctx)

    record = next(r for r in _run_records(ctx) if r["record_type"] == "SQL_EXECUTION")
    assert record["operation"] == "routine"
    assert record["sql_label"] == "start_batch"
    assert record["routine"] == "control.f_start_batch"
    assert record["status"] == "success"
    assert record["object_count"] == 1
    assert "p_run_id" not in json.dumps(record)


def test_routine_no_return_executes_procedure():
    conn = object()
    m = _map(_rb(name="end_batch", routine="control.p_end_batch",
                 call_type="procedure_no_return", inp={"p_batch_id": "batch_id"}))
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db") as db:
        result = execute_mapped_routine(object(), conn, "control", "end_batch",
                                        {"batch_id": 5})
    db.execute_procedure.assert_called_once_with(conn, "control.p_end_batch",
                                                 {"p_batch_id": 5})
    assert result["result_mode"] == "no_return" and result["outputs"] == {}


def test_missing_input_fails_closed():
    conn = object()
    m = _map(_rb(name="a", routine="r", call_type="procedure_no_return", inp={"p_x": "x"}))
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db"):
        with pytest.raises(ConfigError, match="missing from the runtime context"):
            execute_mapped_routine(object(), conn, "control", "a", {})


def test_present_none_input_binds_as_null():
    conn = object()
    m = _map(_rb(name="a", routine="r", call_type="procedure_no_return", inp={"p_x": "x"}))
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db") as db:
        execute_mapped_routine(object(), conn, "control", "a", {"x": None})
    db.execute_procedure.assert_called_once_with(conn, "r", {"p_x": None})


def test_input_resolves_from_run_ctx():
    conn = object()
    m = _map(_rb(name="a", routine="r", call_type="procedure_no_return", inp={"p_b": "batch_id"}))
    run_ctx = SimpleNamespace(batch_id=99)
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db") as db:
        execute_mapped_routine(object(), conn, "control", "a", {}, run_ctx=run_ctx)
    db.execute_procedure.assert_called_once_with(conn, "r", {"p_b": 99})


# ---------------------------------------------------------------------------
# execute_operation (generic dispatch: routine / mapped_sql / adhoc_sql)
# ---------------------------------------------------------------------------

def test_operation_routine_delegates():
    conn = object()
    m = _map(_rb(name="start_batch", call_type="function_with_return",
                 output={"variable": "batch_id", "load_to_ctx": "batch_id"},
                 inp={"p_run_id": "run_id"}))
    run_ctx = SimpleNamespace()
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db") as db:
        db.execute_function.return_value = 7
        result = execute_operation(object(), conn, "control",
                                   {"execution_target": "routine", "binding": "start_batch"},
                                   {"run_id": "R"}, run_ctx)
    assert result["execution_target"] == "routine" and result["outputs"] == {"batch_id": 7}
    assert run_ctx.batch_id == 7


def test_operation_mapped_sql_scalar():
    conn = object()
    m = {"name": "control", "sql_bindings": [
        _sb(name="start_batch", sql="INSERT INTO b VALUES (:run_id) RETURNING batch_id",
            result_mode="scalar_result",
            output={"variable": "batch_id", "load_to_ctx": "batch_id"},
            inp={"run_id": "run_id"})]}
    run_ctx = SimpleNamespace()
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db") as db:
        db.execute_sql.return_value = 99
        result = execute_operation(object(), conn, "control",
                                   {"execution_target": "mapped_sql", "binding": "start_batch"},
                                   {"run_id": "R"}, run_ctx)
    db.execute_sql.assert_called_once_with(
        conn, "INSERT INTO b VALUES (:run_id) RETURNING batch_id", {"run_id": "R"},
        "scalar_result")
    assert result["outputs"] == {"batch_id": 99} and run_ctx.batch_id == 99


def test_operation_adhoc_no_return():
    conn = object()
    config = {"execution_target": "adhoc_sql", "result_mode": "no_return",
              "sql_text": "DELETE FROM staging WHERE batch_id = :batch_id",
              "input": {"batch_id": "batch_id"}}
    with patch("rey_lib.db.procedure_map._db") as db:
        db.execute_sql.return_value = None
        result = execute_operation(object(), conn, "control", config, {"batch_id": 5})
    db.execute_sql.assert_called_once_with(
        conn, "DELETE FROM staging WHERE batch_id = :batch_id", {"batch_id": 5}, "no_return")
    assert result["execution_target"] == "adhoc_sql" and result["result_mode"] == "no_return"


def test_operation_adhoc_dataset_returns_rows():
    conn = object()
    with patch("rey_lib.db.procedure_map._db") as db:
        db.execute_sql.return_value = [{"id": 1}, {"id": 2}]
        result = execute_operation(object(), conn, "control",
                                   {"execution_target": "adhoc_sql",
                                    "result_mode": "dataset_result",
                                    "sql_text": "SELECT id FROM t"}, {})
    assert result["rows"] == [{"id": 1}, {"id": 2}]


def test_mapped_sql_logs_dataset_row_count_without_raw_sql(tmp_path: Path):
    ctx = _log_ctx(tmp_path)
    conn = object()
    m = {"name": "control", "sql_bindings": [
        _sb(name="find_batch", sql="SELECT secret_value FROM t WHERE id = :batch_id",
            result_mode="dataset_result", inp={"batch_id": "batch_id"})]}
    with patch("rey_lib.db.procedure_map.get_procedure_map", return_value=m), \
         patch("rey_lib.db.procedure_map._db") as db:
        db.execute_sql.return_value = [{"id": 1}, {"id": 2}]
        execute_operation(ctx, conn, "control",
                          {"execution_target": "mapped_sql", "binding": "find_batch"},
                          {"batch_id": 5})

    record = next(r for r in _run_records(ctx) if r["record_type"] == "SQL_EXECUTION")
    assert record["operation"] == "mapped_sql"
    assert record["sql_label"] == "find_batch"
    assert record["row_count"] == 2
    text = json.dumps(record)
    assert "secret_value" not in text
    assert "batch_id" not in text


def test_sql_failure_logs_sanitized_failure_evidence(tmp_path: Path):
    ctx = _log_ctx(tmp_path)
    conn = object()
    config = {"execution_target": "adhoc_sql", "result_mode": "no_return",
              "sql_text": "DELETE FROM t WHERE api_key = :api_key",
              "input": {"api_key": "api_key"}}
    with patch("rey_lib.db.procedure_map._db") as db:
        db.execute_sql.side_effect = RuntimeError("connection failed")
        with pytest.raises(RuntimeError, match="connection failed"):
            execute_operation(ctx, conn, "control", config, {"api_key": "SECRET"})

    record = next(r for r in _run_records(ctx) if r["record_type"] == "SQL_EXECUTION")
    assert record["operation"] == "adhoc_sql"
    assert record["status"] == "failed"
    assert record["error_message"] == "connection failed"
    text = json.dumps(record)
    assert "DELETE FROM" not in text
    assert "SECRET" not in text


def test_execute_sql_text_logs_one_authoritative_record(tmp_path: Path):
    ctx = _log_ctx(tmp_path)

    class Conn:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _sql: str) -> None:
            self.calls += 1

    conn = Conn()
    execute_sql_text(
        ctx,
        conn,
        "select secret_value from source",
        sql_label="hook_file",
        operation="hook_sql_file",
        sql_path="/tmp/hook.sql",
        safe_to_preview=True,
    )

    records = [r for r in _run_records(ctx) if r["record_type"] == "SQL_EXECUTION"]
    assert conn.calls == 1
    assert len(records) == 1
    assert records[0]["operation"] == "hook_sql_file"
    assert records[0]["sql_label"] == "hook_file"
    assert "secret_value" not in json.dumps(records[0])


def test_execute_procedure_call_logs_one_authoritative_record(tmp_path: Path):
    ctx = _log_ctx(tmp_path)
    conn = object()
    with patch("rey_lib.db.procedure_map._db") as db:
        db.call_proc_with_output.return_value = {"out_id": 7}
        result = execute_procedure_call(
            ctx,
            conn,
            "control.finish_batch",
            [("batch_id", 5)],
            [("out_id", "INT")],
            sql_label="finish_batch",
            operation="hook_procedure",
        )

    records = [r for r in _run_records(ctx) if r["record_type"] == "SQL_EXECUTION"]
    assert result == {"out_id": 7}
    assert len(records) == 1
    assert records[0]["operation"] == "hook_procedure"
    assert records[0]["routine"] == "control.finish_batch"
    assert records[0]["object_count"] == 1


def test_operation_missing_execution_target_fails_closed():
    with pytest.raises(ConfigError, match="missing 'execution_target'"):
        execute_operation(object(), object(), "control", {}, {})


def test_operation_unsupported_execution_target_fails_closed():
    with pytest.raises(ConfigError, match="unsupported execution_target"):
        execute_operation(object(), object(), "control", {"execution_target": "telepathy"}, {})


def test_adhoc_interpolation_fails_closed():
    conn = object()
    config = {"execution_target": "adhoc_sql", "result_mode": "no_return",
              "sql_text": "DELETE FROM t WHERE id = {batch_id}",
              "input": {"batch_id": "batch_id"}}
    with patch("rey_lib.db.procedure_map._db"):
        with pytest.raises(ConfigError, match="interpolate"):
            execute_operation(object(), conn, "control", config, {"batch_id": 5})


# ---------------------------------------------------------------------------
# Named-SQL adapter primitive (postgres backend)
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, one=None, all_rows=None, description=None):
        self._one = one
        self._all = all_rows or []
        self.description = description
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolledback = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolledback = True


def test_named_sql_scalar_binds_named_params():
    cur = _FakeCursor(one=(42,))
    conn = _FakeConn(cur)
    value = execute_named_sql(conn, "SELECT f(:run_id, :name)",
                              {"run_id": "R", "name": "N"}, "scalar_result")
    assert value == 42
    sql, params = cur.executed[0]
    assert sql == "SELECT f(%(run_id)s, %(name)s)"
    assert params == {"run_id": "R", "name": "N"}
    assert conn.committed


def test_named_sql_leaves_type_casts_alone():
    cur = _FakeCursor(one=(1,))
    execute_named_sql(_FakeConn(cur), "SELECT (:x)::text", {"x": "1"}, "scalar_result")
    assert cur.executed[0][0] == "SELECT (%(x)s)::text"


def test_named_sql_scalar_no_row_fails_closed():
    conn = _FakeConn(_FakeCursor(one=None))
    with pytest.raises(DatabaseError, match="none was returned"):
        execute_named_sql(conn, "SELECT f()", {}, "scalar_result")
    assert conn.rolledback


def test_named_sql_scalar_multiple_values_fails_closed():
    conn = _FakeConn(_FakeCursor(one=(1, 2)))
    with pytest.raises(DatabaseError, match="one value"):
        execute_named_sql(conn, "SELECT 1, 2", {}, "scalar_result")


def test_named_sql_dataset_returns_dict_rows():
    cur = _FakeCursor(all_rows=[(1, "a"), (2, "b")], description=[("id",), ("name",)])
    rows = execute_named_sql(_FakeConn(cur), "SELECT id, name FROM t WHERE b = :b",
                             {"b": 1}, "dataset_result")
    assert rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_named_sql_no_return_commits_and_returns_none():
    conn = _FakeConn(_FakeCursor())
    assert execute_named_sql(conn, "DELETE FROM t WHERE b = :b", {"b": 1}, "no_return") is None
    assert conn.committed


# ---------------------------------------------------------------------------
# get_procedure_map + app boundary
# ---------------------------------------------------------------------------

def test_get_procedure_map_missing_fails_closed():
    ctx = SimpleNamespace(procedure_maps=None)
    with pytest.raises(ConfigError, match="not configured"):
        get_procedure_map(ctx, "control")


def test_apps_do_not_parse_operation_map_internals():
    """Apps request operations by name; they never parse map internals."""
    apps_dir = Path(__file__).resolve().parents[2]  # .../apps
    forbidden = ("routine_bindings", "sql_bindings", "return_variable", "call_type")
    checked = 0
    for app in ("rey_loader/rey_loader", "rey_db_admin/rey_db_admin"):
        root = apps_dir / app
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if "/.claude/" in py.as_posix():
                continue
            text = py.read_text(encoding="utf-8")
            checked += 1
            for token in forbidden:
                assert token not in text, (
                    f"{py} references operation-map internal '{token}' — mapped/ad "
                    "hoc SQL handling belongs in rey_lib db_utils."
                )
    assert checked > 0, "expected to scan at least one app source tree"

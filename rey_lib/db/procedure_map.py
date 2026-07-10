"""
Generic database operation executor (db_utils).

The ONLY layer that understands the operation-map config shape. Apps request an
operation by map + binding name (or supply ad hoc SQL) and pass runtime values;
they never parse ``routine_bindings``/``sql_bindings``, dispatch mechanics,
engine SQL syntax, return extraction, or context output loading.

Execution targets and result modes
-----------------------------------
Every operation is ``execution_target + result_mode + inputs + optional output``:

    execution_target: routine | mapped_sql | adhoc_sql
    result_mode:      no_return | scalar_result | dataset_result

Config shape (list-based named records):

    procedure_maps:
      - name: control
        connection_name: control
        routine_bindings:
          - name: start_batch
            execution_target: routine        # optional; default routine
            routine: control.f_start_batch
            routine_type: function           # function | procedure
            result_mode: scalar_result
            output: {variable: batch_id, load_to_ctx: batch_id}
            input: {p_run_id: run_id}
        sql_bindings:
          - name: start_batch
            execution_target: mapped_sql
            result_mode: scalar_result
            sql: "INSERT INTO control.batch (...) VALUES (:run_id, ...) RETURNING batch_id"
            output: {variable: batch_id, load_to_ctx: batch_id}
            input: {run_id: run_id}

Ad hoc SQL is not registered; it is supplied at execution time via the step
config (``sql_text`` + ``result_mode`` + ``input``).

Legacy compatibility (isolated here, never in app code): the old ``actions``
dict shape, ``call_type: function|procedure`` and ``function_with_return |
procedure_no_return``, and ``return_variable`` normalize into the new shape:

    function_with_return -> execution_target=routine, routine_type=function, result_mode=scalar_result
    procedure_no_return  -> execution_target=routine, routine_type=procedure, result_mode=no_return
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from rey_lib.config.ctx import find_by_name
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import log_sql_execution

__all__ = [
    "get_procedure_map",
    "get_connection_config",
    "resolve_routine_binding",
    "resolve_sql_binding",
    "execute_mapped_routine",
    "execute_operation",
    "execute_sql_text",
    "execute_procedure_call",
    "call_action",
]

_logger = logging.getLogger(__name__)

_db = DBAdapter()

_SUPPORTED_EXECUTION_TARGETS = {"routine", "mapped_sql", "adhoc_sql"}
_SUPPORTED_RESULT_MODES = {"no_return", "scalar_result", "dataset_result"}
_LEGACY_CALL_TYPE_ALIASES = {"function": "function_with_return", "procedure": "procedure_no_return"}
_LEGACY_CALL_TYPE_TO_ROUTINE = {
    "function_with_return": ("function", "scalar_result"),
    "procedure_no_return": ("procedure", "no_return"),
}


# ---------------------------------------------------------------------------
# Map / connection resolution
# ---------------------------------------------------------------------------

def get_procedure_map(ctx: Any, map_name: str) -> Any:
    """Return the named operation-map record from ``ctx.procedure_maps``.

    Raises
    ------
    ConfigError
        If ``ctx.procedure_maps`` is absent or the named map is not found.
    """
    maps = getattr(ctx, "procedure_maps", None)
    if not maps:
        raise ConfigError(
            "procedure_map: ctx.procedure_maps is not configured. "
            "Add procedure_maps.yaml to the config include path."
        )
    found = find_by_name(list(maps), map_name)
    if found is None:
        raise ConfigError(
            f"procedure_map: map '{map_name}' not found in ctx.procedure_maps."
        )
    return found


def get_connection_config(ctx: Any, map_name: str) -> Any:
    """Resolve the ``db_connections`` record bound to the named operation map.

    Raises
    ------
    ConfigError
        If the map has no ``connection_name``, ``ctx.db_connections`` is
        absent, or the bound connection is not found.
    """
    map_cfg = get_procedure_map(ctx, map_name)
    conn_name = _get(map_cfg, "connection_name")
    if not conn_name:
        raise ConfigError(f"procedure_map: map '{map_name}' has no connection_name.")
    conns = getattr(ctx, "db_connections", None)
    if not conns:
        raise ConfigError(
            "procedure_map: ctx.db_connections is not configured. "
            "Add db_settings.yaml to the config include path."
        )
    conn_cfg = find_by_name(list(conns), str(conn_name))
    if conn_cfg is None:
        raise ConfigError(
            f"procedure_map: connection '{conn_name}' (bound by map '{map_name}') "
            "not found in ctx.db_connections."
        )
    return conn_cfg


# ---------------------------------------------------------------------------
# Binding resolution (normalized, fail-closed)
# ---------------------------------------------------------------------------

def resolve_routine_binding(map_cfg: Any, map_name: str, routine_name: str) -> dict[str, Any]:
    """Return a normalized routine binding for ``routine_name``.

    Normalized dict:
    ``{procedure_map_name, name, execution_target, routine, routine_type,
    result_mode, inputs, output}``. Derives ``routine_type``/``result_mode``
    from a legacy ``call_type`` when present. Fails closed on the usual
    structural problems and on a ``scalar_result`` binding without
    ``output.variable``.
    """
    bindings = _get(map_cfg, "routine_bindings")
    if bindings is None:
        bindings = _normalize_legacy_actions(_get(map_cfg, "actions"), map_name)
    binding = _find_binding(bindings, map_name, routine_name, "routine_bindings")

    routine = _get(binding, "routine")
    if not routine:
        raise ConfigError(
            f"procedure_map: routine binding '{routine_name}' in map '{map_name}' "
            "is missing 'routine'."
        )

    routine_type = _get(binding, "routine_type")
    result_mode = _get(binding, "result_mode")
    call_type = _get(binding, "call_type")
    if call_type:
        canonical = _LEGACY_CALL_TYPE_ALIASES.get(str(call_type), str(call_type))
        legacy = _LEGACY_CALL_TYPE_TO_ROUTINE.get(canonical)
        if legacy is None:
            raise ConfigError(
                f"procedure_map: unsupported call_type '{call_type}' for routine "
                f"'{routine_name}' in map '{map_name}'."
            )
        routine_type = routine_type or legacy[0]
        result_mode = result_mode or legacy[1]

    result_mode = _validate_result_mode(result_mode, routine_name, map_name)
    if result_mode == "scalar_result" and not _get(_get(binding, "output"), "variable"):
        raise ConfigError(
            f"procedure_map: routine '{routine_name}' in map '{map_name}' is "
            "'scalar_result' but has no 'output.variable'."
        )

    return {
        "procedure_map_name": map_name,
        "name": routine_name,
        "execution_target": "routine",
        "routine": str(routine),
        "routine_type": str(routine_type) if routine_type else "",
        "result_mode": result_mode,
        "inputs": _binding_inputs(binding),
        "output": _get(binding, "output"),
    }


def resolve_sql_binding(map_cfg: Any, map_name: str, binding_name: str) -> dict[str, Any]:
    """Return a normalized mapped-SQL binding for ``binding_name``.

    Normalized dict:
    ``{procedure_map_name, name, execution_target, sql, result_mode, inputs,
    output}``. Fails closed on missing ``sql``, unsupported ``result_mode``, or
    a ``scalar_result`` binding without ``output.variable``.
    """
    bindings = _find_binding(_get(map_cfg, "sql_bindings"), map_name, binding_name,
                             "sql_bindings")
    sql = _get(bindings, "sql")
    if not sql:
        raise ConfigError(
            f"procedure_map: sql binding '{binding_name}' in map '{map_name}' "
            "is missing 'sql'."
        )
    result_mode = _validate_result_mode(_get(bindings, "result_mode"), binding_name, map_name)
    if result_mode == "scalar_result" and not _get(_get(bindings, "output"), "variable"):
        raise ConfigError(
            f"procedure_map: sql binding '{binding_name}' in map '{map_name}' is "
            "'scalar_result' but has no 'output.variable'."
        )
    return {
        "procedure_map_name": map_name,
        "name": binding_name,
        "execution_target": "mapped_sql",
        "sql": str(sql),
        "result_mode": result_mode,
        "inputs": _binding_inputs(bindings),
        "output": _get(bindings, "output"),
    }


def _find_binding(bindings: Any, map_name: str, name: str, section: str) -> Any:
    """Return the uniquely-named binding from a bindings list, fail-closed."""
    bindings = list(bindings) if isinstance(bindings, (list, tuple)) else None
    if not bindings:
        raise ConfigError(f"procedure_map: map '{map_name}' has no '{section}' list.")
    names = [str(_get(b, "name", "") or "") for b in bindings]
    if "" in names:
        raise ConfigError(
            f"procedure_map: map '{map_name}' has a {section} entry without a 'name'."
        )
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(
            f"procedure_map: map '{map_name}' has duplicate {section} name(s): "
            f"{', '.join(duplicates)}."
        )
    binding = next((b for b, n in zip(bindings, names) if n == name), None)
    if binding is None:
        raise ConfigError(
            f"procedure_map: binding '{name}' not found in {section} of map '{map_name}'."
        )
    return binding


def _validate_result_mode(result_mode: Any, name: str, map_name: str) -> str:
    """Return a validated result_mode, failing closed when missing/unsupported."""
    if not result_mode:
        raise ConfigError(
            f"procedure_map: binding '{name}' in map '{map_name}' is missing "
            "'result_mode' (or a legacy 'call_type')."
        )
    if str(result_mode) not in _SUPPORTED_RESULT_MODES:
        raise ConfigError(
            f"procedure_map: unsupported result_mode '{result_mode}' for binding "
            f"'{name}' in map '{map_name}'. Use one of {sorted(_SUPPORTED_RESULT_MODES)}."
        )
    return str(result_mode)


def _binding_inputs(binding: Any) -> Any:
    """Return a binding's input map, accepting 'input' or legacy 'inputs'."""
    inputs = _get(binding, "input")
    return inputs if inputs is not None else _get(binding, "inputs")


def _normalize_legacy_actions(actions: Any, map_name: str) -> Optional[list[dict[str, Any]]]:
    """Normalize the legacy ``actions`` dict into routine bindings (compat only)."""
    if actions is None:
        return None
    _logger.warning(
        "procedure_map: map '%s' uses the legacy 'actions' shape; migrate to "
        "'routine_bindings'.", map_name,
    )
    bindings: list[dict[str, Any]] = []
    for name, cfg in _items(actions):
        binding: dict[str, Any] = {
            "name": str(name),
            "routine": _get(cfg, "routine"),
            "call_type": _get(cfg, "call_type"),
            "input": _get(cfg, "inputs"),
        }
        return_variable = _get(cfg, "return_variable")
        if return_variable:
            binding["output"] = {"variable": return_variable, "load_to_ctx": return_variable}
        bindings.append(binding)
    return bindings


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute_operation(
    ctx: Any,
    conn: Any,
    operation_map: str,
    config: Any,
    values: dict[str, Any],
    run_ctx: Any = None,
) -> dict[str, Any]:
    """Execute one generic database operation from a workflow step config.

    Dispatches on ``config.execution_target`` (routine | mapped_sql | adhoc_sql).
    Routine and mapped-SQL bindings are resolved by name from ``operation_map``;
    ad hoc SQL is supplied inline via ``config.sql_text``.

    Raises
    ------
    ConfigError
        On unsupported/absent execution target, missing binding, unsupported
        result mode, missing required input, or attempted SQL interpolation.
    """
    target = _get(config, "execution_target")
    if not target:
        raise ConfigError("procedure_map: operation config is missing 'execution_target'.")
    if str(target) not in _SUPPORTED_EXECUTION_TARGETS:
        raise ConfigError(
            f"procedure_map: unsupported execution_target '{target}'. Use one of "
            f"{sorted(_SUPPORTED_EXECUTION_TARGETS)}."
        )

    if target == "routine":
        binding_name = _get(config, "binding") or _get(config, "routine")
        return execute_mapped_routine(ctx, conn, operation_map, str(binding_name),
                                      values, run_ctx=run_ctx)

    if target == "mapped_sql":
        binding = resolve_sql_binding(get_procedure_map(ctx, operation_map),
                                      operation_map, str(_get(config, "binding")))
        return _execute_sql(ctx, conn, operation_map, binding["name"], binding["sql"],
                            binding["result_mode"], binding["inputs"],
                            binding["output"], values, run_ctx)

    # adhoc_sql
    sql_text = _get(config, "sql_text")
    if not sql_text:
        raise ConfigError("procedure_map: adhoc_sql operation is missing 'sql_text'.")
    result_mode = _validate_result_mode(_get(config, "result_mode"), "adhoc_sql", operation_map)
    return _execute_sql(ctx, conn, operation_map, "adhoc_sql", str(sql_text), result_mode,
                        _get(config, "input") if _get(config, "input") is not None
                        else _get(config, "inputs"),
                        _get(config, "output"), values, run_ctx)


def execute_mapped_routine(
    ctx: Any,
    conn: Any,
    procedure_map: str,
    routine_name: str,
    values: dict[str, Any],
    run_ctx: Any = None,
) -> dict[str, Any]:
    """Execute one mapped routine (function/procedure) and capture its output.

    Resolves the named routine binding, binds inputs, executes per
    ``result_mode`` (``scalar_result`` -> function/SELECT; ``no_return`` ->
    procedure/CALL), and stores a captured scalar via ``output``.
    ``dataset_result`` for a routine is not supported — use a mapped_sql binding.
    """
    binding = resolve_routine_binding(get_procedure_map(ctx, procedure_map),
                                      procedure_map, routine_name)
    named_params = _bind_inputs(binding["inputs"], values, run_ctx,
                                procedure_map, routine_name)
    result_mode = binding["result_mode"]

    _logger.debug(
        "procedure_map: %s.%s -> %s(%s) [%s]",
        procedure_map, routine_name, binding["routine"], list(named_params), result_mode,
    )

    t0 = time.monotonic()
    outputs: dict[str, Any] = {}
    try:
        if result_mode == "scalar_result":
            scalar = _db.execute_function(conn, binding["routine"], named_params)
            outputs = _apply_output(scalar, binding["output"], values, run_ctx)
        elif result_mode == "no_return":
            _db.execute_procedure(conn, binding["routine"], named_params)
        else:  # dataset_result
            raise ConfigError(
                f"procedure_map: routine '{routine_name}' in map '{procedure_map}' "
                "requests 'dataset_result'; use a mapped_sql binding for datasets."
            )
    except Exception as exc:
        log_sql_execution(
            ctx,
            sql_label=routine_name,
            operation="routine",
            status="failed",
            duration_ms=int((time.monotonic() - t0) * 1000),
            error_message=str(exc),
            safe_to_preview=False,
            procedure_map=procedure_map,
            routine=str(binding["routine"]),
            routine_type=str(binding.get("routine_type") or ""),
            result_mode=result_mode,
        )
        raise

    log_sql_execution(
        ctx,
        sql_label=routine_name,
        operation="routine",
        status="success",
        duration_ms=int((time.monotonic() - t0) * 1000),
        safe_to_preview=False,
        procedure_map=procedure_map,
        routine=str(binding["routine"]),
        routine_type=str(binding.get("routine_type") or ""),
        result_mode=result_mode,
        object_count=len(outputs) if outputs else None,
    )

    return {
        "procedure_map": procedure_map,
        "routine_name": routine_name,
        "routine": binding["routine"],
        "execution_target": "routine",
        "result_mode": result_mode,
        "outputs": outputs,
        "status": "success",
    }


def execute_sql_text(
    ctx: Any,
    conn: Any,
    sql_text: str,
    *,
    sql_label: str,
    operation: str,
    sql_path: str = "",
    safe_to_preview: bool = False,
    **context: Any,
) -> Any:
    """Execute raw SQL text and emit authoritative SQL_EXECUTION evidence."""
    t0 = time.monotonic()
    try:
        result = _db.run_sql(conn, sql_text)
    except Exception as exc:
        log_sql_execution(
            ctx,
            sql_path=sql_path,
            sql_label=sql_label,
            operation=operation,
            status="failed",
            duration_ms=int((time.monotonic() - t0) * 1000),
            error_message=str(exc),
            safe_to_preview=safe_to_preview,
            **context,
        )
        raise

    log_sql_execution(
        ctx,
        sql_path=sql_path,
        sql_label=sql_label,
        operation=operation,
        status="success",
        duration_ms=int((time.monotonic() - t0) * 1000),
        safe_to_preview=safe_to_preview,
        **context,
    )
    return result


def execute_procedure_call(
    ctx: Any,
    conn: Any,
    proc_name: str,
    named_inputs: list[tuple[str, Any]],
    output_specs: list[tuple[str, str]],
    *,
    sql_label: str,
    operation: str,
    safe_to_preview: bool = False,
    **context: Any,
) -> dict[str, Any]:
    """Execute a procedure call and emit authoritative SQL_EXECUTION evidence."""
    t0 = time.monotonic()
    try:
        if output_specs:
            output_values = _db.call_proc_with_output(
                conn, proc_name, named_inputs, output_specs
            )
        else:
            _db.call_proc(conn, proc_name, [v for _n, v in named_inputs])
            output_values = {}
    except Exception as exc:
        log_sql_execution(
            ctx,
            sql_label=sql_label,
            operation=operation,
            status="failed",
            duration_ms=int((time.monotonic() - t0) * 1000),
            error_message=str(exc),
            safe_to_preview=safe_to_preview,
            routine=str(proc_name),
            **context,
        )
        raise

    log_sql_execution(
        ctx,
        sql_label=sql_label,
        operation=operation,
        status="success",
        duration_ms=int((time.monotonic() - t0) * 1000),
        safe_to_preview=safe_to_preview,
        routine=str(proc_name),
        object_count=len(output_values) if output_values else None,
        **context,
    )
    return output_values


def _execute_sql(
    ctx: Any,
    conn: Any,
    map_name: str,
    name: str,
    sql_text: str,
    result_mode: str,
    inputs: Any,
    output: Any,
    values: dict[str, Any],
    run_ctx: Any,
) -> dict[str, Any]:
    """Execute mapped/ad hoc SQL with bound inputs and a result mode."""
    named_params = _bind_inputs(inputs, values, run_ctx, map_name, name)
    _guard_no_interpolation(sql_text, named_params, map_name, name)
    if result_mode == "scalar_result" and not _get(output, "variable"):
        raise ConfigError(
            f"procedure_map: sql operation '{name}' in map '{map_name}' is "
            "'scalar_result' but has no 'output.variable'."
        )

    t0 = time.monotonic()
    outputs: dict[str, Any] = {}
    rows: Optional[list[dict[str, Any]]] = None
    execution_target = "adhoc_sql" if name == "adhoc_sql" else "mapped_sql"
    try:
        data = _db.execute_sql(conn, sql_text, named_params, result_mode)
        if result_mode == "scalar_result":
            outputs = _apply_output(data, output, values, run_ctx)
        elif result_mode == "dataset_result":
            rows = data
    except Exception as exc:
        log_sql_execution(
            ctx,
            sql_label=name,
            operation=execution_target,
            status="failed",
            duration_ms=int((time.monotonic() - t0) * 1000),
            error_message=str(exc),
            safe_to_preview=False,
            procedure_map=map_name,
            result_mode=result_mode,
        )
        raise

    log_sql_execution(
        ctx,
        sql_label=name,
        operation=execution_target,
        status="success",
        duration_ms=int((time.monotonic() - t0) * 1000),
        safe_to_preview=False,
        procedure_map=map_name,
        result_mode=result_mode,
        row_count=len(rows) if rows is not None else None,
        object_count=len(outputs) if outputs else None,
    )

    return {
        "procedure_map": map_name,
        "routine_name": name,
        "execution_target": execution_target,
        "result_mode": result_mode,
        "outputs": outputs,
        "rows": rows,
        "status": "success",
    }


def _apply_output(scalar: Any, output: Any, values: dict[str, Any], run_ctx: Any) -> dict[str, Any]:
    """Store a captured scalar into values[output.variable] and run_ctx[load_to_ctx]."""
    variable = str(_get(output, "variable"))
    if isinstance(values, dict):
        values[variable] = scalar
    load_to_ctx = _get(output, "load_to_ctx")
    if load_to_ctx and run_ctx is not None:
        setattr(run_ctx, str(load_to_ctx), scalar)
    return {variable: scalar}


def _bind_inputs(inputs: Any, values: dict[str, Any], run_ctx: Any,
                 map_name: str, name: str) -> dict[str, Any]:
    """Bind ``db/sql parameter -> runtime value`` from the input map.

    Fails closed when ``input`` is present but not a mapping, or when a mapped
    runtime variable is absent from both the supplied values and the run
    context (a present ``None`` value is a valid SQL NULL).
    """
    if inputs is None:
        return {}
    if not _is_mapping(inputs):
        raise ConfigError(
            f"procedure_map: binding '{name}' in map '{map_name}' has a "
            "non-mapping 'input'."
        )
    named: dict[str, Any] = {}
    for param, var_name in _items(inputs):
        named[str(param)] = _resolve_value(str(var_name), values, run_ctx, map_name, name)
    return named


def _resolve_value(var_name: str, values: dict[str, Any], run_ctx: Any,
                   map_name: str, name: str) -> Any:
    """Resolve a runtime value from supplied values then run context (fail-closed)."""
    if isinstance(values, dict) and var_name in values:
        return values[var_name]
    if run_ctx is not None and hasattr(run_ctx, var_name):
        return getattr(run_ctx, var_name)
    raise ConfigError(
        f"procedure_map: required input '{var_name}' for binding '{name}' in map "
        f"'{map_name}' is missing from the runtime context."
    )


def _guard_no_interpolation(sql_text: str, named_params: dict[str, Any],
                            map_name: str, name: str) -> None:
    """Fail closed if the SQL interpolates an input (``{param}``) instead of binding."""
    for param in named_params:
        if "{" + param + "}" in sql_text:
            raise ConfigError(
                f"procedure_map: sql operation '{name}' in map '{map_name}' appears "
                f"to interpolate '{param}'; use ':{param}' bind parameters instead."
            )


def call_action(
    ctx: Any,
    conn: Any,
    map_name: str,
    action_name: str,
    variables: dict[str, Any],
) -> Optional[Any]:
    """Deprecated scalar-returning shim over :func:`execute_mapped_routine`.

    Returns the captured scalar (functions) or None (procedures). New callers
    should use ``execute_mapped_routine`` or ``execute_operation``.
    """
    result = execute_mapped_routine(ctx, conn, map_name, action_name, variables, run_ctx=ctx)
    outputs = result.get("outputs") or {}
    return next(iter(outputs.values()), None)


# ---------------------------------------------------------------------------
# Mapping/attribute access helpers (accept dict- or Namespace-like inputs)
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Return obj[key] / obj.key, or default."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _is_mapping(obj: Any) -> bool:
    """Return True for a dict- or Namespace-like mapping."""
    if isinstance(obj, dict):
        return True
    if isinstance(obj, (list, tuple, str, bytes)):
        return False
    return hasattr(obj, "items") or hasattr(obj, "__dict__")


def _items(obj: Any) -> list[tuple[Any, Any]]:
    """Return (key, value) pairs from a dict- or Namespace-like mapping."""
    if obj is None:
        return []
    if isinstance(obj, dict):
        return list(obj.items())
    if hasattr(obj, "items"):
        try:
            return list(obj.items())
        except Exception:  # noqa: BLE001 — fall through to attribute view
            pass
    if hasattr(obj, "__dict__"):
        return [(k, v) for k, v in vars(obj).items() if not k.startswith("_")]
    return []

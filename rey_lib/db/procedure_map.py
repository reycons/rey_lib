"""
Generic procedure-map loader and routine dispatcher.

Reads named procedure maps from ``ctx.procedure_maps``, resolves the bound
database connection from ``ctx.db_connections``, maps Rey internal variable
names to database parameter names, and dispatches to ``DBAdapter``.

This module is generic database infrastructure. It has no knowledge of Rey
control concepts (batches, steps, artifacts, contracts). Control lifecycle
logic lives in ``rey_lib.control.control_utils`` and calls into this module.

Config shapes (list-based named records, per the DB Settings contract):

    db_settings.yaml:
        db_connections:
          - name: control
            provider: postgres
            ...

    procedure_maps.yaml:
        procedure_maps:
          - name: control
            connection_name: control
            actions:
              start_batch:
                routine: control.f_start_batch
                call_type: function          # function | procedure
                return_variable: batch_id    # function only
                inputs:
                  p_db_param: rey_variable

Dispatcher rules:
    call_type: function  -> execute_function  -> SELECT routine(...) -> scalar
    call_type: procedure -> execute_procedure -> CALL routine(...)   -> None
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from rey_lib.config.ctx import find_by_name
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import ConfigError

__all__ = ["get_procedure_map", "get_connection_config", "call_action"]

_logger = logging.getLogger(__name__)

_db = DBAdapter()


def get_procedure_map(ctx: Any, map_name: str) -> Any:
    """
    Return the named procedure-map record from ``ctx.procedure_maps``.

    Parameters
    ----------
    ctx : Any
        Application context. Must expose ``ctx.procedure_maps`` as a list of
        named procedure-map records.
    map_name : str
        Procedure map name (e.g. 'control').

    Returns
    -------
    Any
        The matching procedure-map Namespace record.

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
    """
    Resolve the ``db_connections`` record bound to the named procedure map.

    The procedure map binds to a connection via its ``connection_name`` field,
    which must match a ``db_connections[].name`` value in db_settings.yaml.

    Parameters
    ----------
    ctx : Any
        Application context. Must expose ``ctx.db_connections`` as a list of
        named connection records.
    map_name : str
        Procedure map name whose bound connection to resolve.

    Returns
    -------
    Any
        The matching connection-config Namespace record.

    Raises
    ------
    ConfigError
        If the map has no ``connection_name``, ``ctx.db_connections`` is
        absent, or the bound connection is not found.
    """
    map_cfg = get_procedure_map(ctx, map_name)

    conn_name = getattr(map_cfg, "connection_name", None)
    if not conn_name:
        raise ConfigError(
            f"procedure_map: map '{map_name}' has no connection_name."
        )

    conns = getattr(ctx, "db_connections", None)
    if not conns:
        raise ConfigError(
            "procedure_map: ctx.db_connections is not configured. "
            "Add db_settings.yaml to the config include path."
        )

    conn_cfg = find_by_name(list(conns), conn_name)
    if conn_cfg is None:
        raise ConfigError(
            f"procedure_map: connection '{conn_name}' (bound by map "
            f"'{map_name}') not found in ctx.db_connections."
        )
    return conn_cfg


def call_action(
    ctx: Any,
    conn: Any,
    map_name: str,
    action_name: str,
    variables: dict[str, Any],
) -> Optional[Any]:
    """
    Execute one mapped action against an open database connection.

    Looks up the action in the named procedure map, maps Rey variables to DB
    parameter names, and dispatches to ``execute_function`` or
    ``execute_procedure`` on the ``DBAdapter``.

    Parameters
    ----------
    ctx : Any
        Application context carrying procedure maps.
    conn : Any
        Open DB connection (resolved via ``get_connection_config``).
    map_name : str
        Procedure map name (e.g. 'control').
    action_name : str
        Action name within the map (e.g. 'start_batch', 'end_step').
    variables : dict[str, Any]
        Rey internal variable names → values for this call.

    Returns
    -------
    Any | None
        Scalar return value for function actions; None for procedures.

    Raises
    ------
    ConfigError
        If the action is missing, malformed, or has an invalid call_type.
    DatabaseError
        If the DB call fails (propagated from the provider utils).
    """
    map_cfg = get_procedure_map(ctx, map_name)

    actions = getattr(map_cfg, "actions", None)
    if actions is None:
        raise ConfigError(
            f"procedure_map: map '{map_name}' has no actions."
        )

    action_cfg = getattr(actions, action_name, None)
    if action_cfg is None:
        raise ConfigError(
            f"procedure_map: action '{action_name}' not found in "
            f"map '{map_name}'."
        )

    routine   = getattr(action_cfg, "routine", None)
    call_type = getattr(action_cfg, "call_type", None)
    inputs_ns = getattr(action_cfg, "inputs", None)

    if not routine or not call_type:
        raise ConfigError(
            f"procedure_map: action '{action_name}' in map '{map_name}' "
            "must define both 'routine' and 'call_type'."
        )

    # Build DB-param → value dict using the inputs mapping.
    named_params: dict[str, Any] = {}
    if inputs_ns is not None:
        for db_param, rey_var in inputs_ns.items():
            named_params[db_param] = variables.get(rey_var)

    _logger.debug(
        "procedure_map: %s.%s → %s(%s)",
        map_name, action_name, routine, list(named_params),
    )

    if call_type == "function":
        return _db.execute_function(conn, routine, named_params)

    if call_type == "procedure":
        _db.execute_procedure(conn, routine, named_params)
        return None

    raise ConfigError(
        f"procedure_map: unknown call_type '{call_type}' for "
        f"action '{action_name}' in map '{map_name}'."
    )

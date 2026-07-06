"""
Control database utilities — public API.

Provides the public control interface for registering batches, steps, log
events, config snapshots, artifacts, and LLM contract runs against the
optional Rey control database.

The control database is optional. All functions return None and continue
silently when control is disabled or unavailable. Applications must never
call control database routines directly — all control behavior must pass
through this module.

Layering
--------
app code / log_utils
    ↓
control_utils  (this module)
    ↓
procedure_map → DBAdapter
    ↓
postgres_utils / sqlserver_utils / …
    ↓
control database functions and procedures

ctx fields read
---------------
ctx.control.enabled                         bool
ctx.control.procedure_map                   str   (procedure_maps[].name, e.g. "control")
ctx.control.behavior.fail_app_on_control_error  bool
ctx.control.behavior.fallback_to_local_log      bool
ctx.procedure_maps                          list  (named maps; the control map
                                                   carries connection_name)
ctx.db_connections                          list  (named connection records)
ctx.run_id                                  str   (set by ensure_run_id)
ctx.batch_id                                int | None  (set by start_batch)
ctx.batch_step_id                           int | None  (set by start_step)
ctx.control_available                       bool  (set False on failure)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from rey_lib.db.db_adapter import DBAdapter
from rey_lib.errors.error_utils import ConfigError, DatabaseError

from rey_lib.db.procedure_map import execute_mapped_routine, get_connection_config
from rey_lib.logs import resolve_run_identity

__all__ = [
    "ensure_run_id",
    "ensure_run_timestamp",
    "get_provider",
    "start_batch",
    "end_batch",
    "start_step",
    "end_step",
    "log_event",
    "save_config_snapshot",
    "get_or_create_artifact",
    "register_artifact_version",
    "register_batch_artifact",
    "get_or_create_contract",
    "register_contract_version",
    "start_contract_run",
    "end_contract_run",
    "save_contract_review",
    "run_logged_sql",
]

_logger = logging.getLogger(__name__)
_db = DBAdapter()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def ensure_run_id(ctx: Any) -> str:
    """
    Ensure ctx.run_id exists (UUID), generating the standard run identity if absent.

    The run_id is the authoritative execution identity and is a UUID
    (SGC_Rey_Run_ID_Standard). This delegates to the runner/logging layer's
    resolve_run_identity, which also establishes ctx.run_timestamp and
    ctx.run_started_at, so a single run identity is shared across logging and
    control-database records. Must be called before any logging or control DB
    interaction.

    Parameters
    ----------
    ctx : Any
        Application context.

    Returns
    -------
    str
        The run_id (existing or newly generated).
    """
    resolve_run_identity(ctx)
    return ctx.run_id


def ensure_run_timestamp(ctx: Any) -> str:
    """
    Ensure ctx.run_timestamp exists and return it (SGC_Rey_Run_ID_Standard).

    The run_timestamp is the human-readable, filename-safe ``YYYYMMDD_HHMMSS`` used
    for artifact filenames and operator display; it is separate from the UUID
    run_id. Delegates to the runner/logging layer's resolve_run_identity so run_id,
    run_timestamp, and run_started_at are established together and stay stable for
    the execution.

    Parameters
    ----------
    ctx : Any
        Application context.

    Returns
    -------
    str
        The run_timestamp (existing or newly generated).
    """
    resolve_run_identity(ctx)
    return ctx.run_timestamp


def get_provider(ctx: Any) -> Optional[str]:
    """
    Return the configured control DB provider name, or None if disabled.

    Parameters
    ----------
    ctx : Any
        Application context.

    Returns
    -------
    str | None
        Provider name (e.g. 'postgres'), or None.
    """
    if not _is_enabled(ctx):
        return None
    conn_cfg = _get_conn_cfg(ctx)
    return getattr(conn_cfg, "provider", None) if conn_cfg else None


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def start_batch(
    ctx: Any,
    batch_name: str,
    pipeline_name: Optional[str] = None,
    owner_app_name: Optional[str] = None,
    context_jsonb: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """
    Register a new batch and store the returned batch_id on ctx.

    Parameters
    ----------
    ctx : Any
        Application context.
    batch_name : str
        Human-readable batch label.
    pipeline_name : str, optional
        Pipeline name when running inside a pipeline.
    owner_app_name : str, optional
        App that owns this batch; defaults to ctx.app_name.
    context_jsonb : dict, optional
        Arbitrary context to store with the batch.

    Returns
    -------
    int | None
        batch_id from the database, or None if control is unavailable.
    """
    return _call(ctx, "start_batch", {
        "run_id":          getattr(ctx, "run_id", None),
        "batch_name":      batch_name,
        "pipeline_name":   pipeline_name,
        "owner_app_name":  owner_app_name or getattr(ctx, "app_name", None),
        "context_jsonb":   context_jsonb,
    }, set_ctx="batch_id")


def end_batch(
    ctx: Any,
    status: str,
    error_message: Optional[str] = None,
    context_jsonb: Optional[dict[str, Any]] = None,
) -> None:
    """
    Mark the current batch as complete.

    Parameters
    ----------
    ctx : Any
        Application context.
    status : str
        Final status (e.g. 'success', 'failed').
    error_message : str, optional
        Error description when status is 'failed'.
    context_jsonb : dict, optional
        Final context snapshot.
    """
    _call(ctx, "end_batch", {
        "batch_id":      getattr(ctx, "batch_id", None),
        "status":        status,
        "error_message": error_message,
        "context_jsonb": context_jsonb,
    })


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def start_step(
    ctx: Any,
    step_name: str,
    step_sequence: Optional[int] = None,
    step_type: Optional[str] = None,
    app_name: Optional[str] = None,
    git_commit_hash: Optional[str] = None,
    parent_batch_step_id: Optional[int] = None,
    context_jsonb: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """
    Register a new batch step and store the returned batch_step_id on ctx.

    Parameters
    ----------
    ctx : Any
        Application context.
    step_name : str
        Step identifier (e.g. pipeline step name or app operation).
    step_sequence : int, optional
        Ordinal position within the batch.
    step_type : str, optional
        Logical step type label.
    app_name : str, optional
        App executing this step; defaults to ctx.app_name.
    git_commit_hash : str, optional
        Git commit hash of the executing app.
    parent_batch_step_id : int, optional
        Parent step ID for nested steps.
    context_jsonb : dict, optional
        Arbitrary context for this step.

    Returns
    -------
    int | None
        batch_step_id from the database, or None.
    """
    return _call(ctx, "start_step", {
        "batch_id":             getattr(ctx, "batch_id", None),
        "step_sequence":        step_sequence,
        "step_name":            step_name,
        "step_type":            step_type,
        "app_name":             app_name or getattr(ctx, "app_name", None),
        "git_commit_hash":      git_commit_hash,
        "parent_batch_step_id": parent_batch_step_id,
        "context_jsonb":        context_jsonb,
    }, set_ctx="batch_step_id")


def end_step(
    ctx: Any,
    status: str,
    message: Optional[str] = None,
    metrics_jsonb: Optional[dict[str, Any]] = None,
    context_jsonb: Optional[dict[str, Any]] = None,
) -> None:
    """
    Mark the current step as complete.

    Parameters
    ----------
    ctx : Any
        Application context.
    status : str
        Final status (e.g. 'success', 'failed').
    message : str, optional
        Completion message.
    metrics_jsonb : dict, optional
        Runtime metrics for this step.
    context_jsonb : dict, optional
        Final context snapshot.
    """
    _call(ctx, "end_step", {
        "batch_step_id": getattr(ctx, "batch_step_id", None),
        "status":        status,
        "message":       message,
        "metrics_jsonb": metrics_jsonb,
        "context_jsonb": context_jsonb,
    })


# ---------------------------------------------------------------------------
# Log events
# ---------------------------------------------------------------------------


def log_event(
    ctx: Any,
    severity: str,
    event_name: str,
    message: str,
    event_jsonb: Optional[dict[str, Any]] = None,
) -> None:
    """
    Write a log event to the control database.

    Parameters
    ----------
    ctx : Any
        Application context.
    severity : str
        Severity level (e.g. 'INFO', 'ERROR', 'WARNING').
    event_name : str
        Short event identifier.
    message : str
        Human-readable message.
    event_jsonb : dict, optional
        Structured event payload.
    """
    _call(ctx, "log_event", {
        "batch_id":      getattr(ctx, "batch_id", None),
        "batch_step_id": getattr(ctx, "batch_step_id", None),
        "severity":      severity,
        "event_name":    event_name,
        "message":       message,
        "event_jsonb":   event_jsonb,
    })


# ---------------------------------------------------------------------------
# Config snapshots
# ---------------------------------------------------------------------------


def save_config_snapshot(
    ctx: Any,
    config_name: str,
    config_scope: str,
    config_format: str,
    config_hash: str,
    config_text: Optional[str] = None,
    config_jsonb: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """
    Save a config snapshot to the control database.

    Parameters
    ----------
    ctx : Any
        Application context.
    config_name : str
        Config identifier.
    config_scope : str
        Scope label (e.g. 'app', 'installation', 'step').
    config_format : str
        Format of the config (e.g. 'yaml', 'json').
    config_hash : str
        Hash of the config content for deduplication.
    config_text : str, optional
        Raw config text.
    config_jsonb : dict, optional
        Config as a structured object.

    Returns
    -------
    int | None
        batch_config_snapshot_id, or None.
    """
    return _call(ctx, "save_config_snapshot", {
        "batch_id":      getattr(ctx, "batch_id", None),
        "batch_step_id": getattr(ctx, "batch_step_id", None),
        "config_name":   config_name,
        "config_scope":  config_scope,
        "config_format": config_format,
        "config_hash":   config_hash,
        "config_text":   config_text,
        "config_jsonb":  config_jsonb,
    })


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def get_or_create_artifact(
    ctx: Any,
    artifact_type: str,
    artifact_name: str,
    metadata_jsonb: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """
    Get or create an artifact registry entry.

    Parameters
    ----------
    ctx : Any
        Application context.
    artifact_type : str
        Artifact type label (e.g. 'file', 'report', 'model').
    artifact_name : str
        Unique artifact name.
    metadata_jsonb : dict, optional
        Artifact metadata.

    Returns
    -------
    int | None
        artifact_id, or None.
    """
    return _call(ctx, "get_or_create_artifact", {
        "artifact_type":  artifact_type,
        "artifact_name":  artifact_name,
        "metadata_jsonb": metadata_jsonb,
    })


def register_artifact_version(
    ctx: Any,
    artifact_id: int,
    version_number: str,
    status: str,
    body_format: str,
    body_hash: str,
    body_text: Optional[str] = None,
    source_uri: Optional[str] = None,
    metadata_jsonb: Optional[dict[str, Any]] = None,
    set_current: bool = True,
) -> Optional[int]:
    """
    Register a new version of an artifact.

    Parameters
    ----------
    ctx : Any
        Application context.
    artifact_id : int
        ID from get_or_create_artifact.
    version_number : str
        Version label.
    status : str
        Version status (e.g. 'active', 'archived').
    body_format : str
        Format of the artifact body (e.g. 'yaml', 'sql', 'json').
    body_hash : str
        Hash of the artifact body content for deduplication.
    body_text : str, optional
        Raw artifact content.
    source_uri : str, optional
        Origin URI or file path.
    metadata_jsonb : dict, optional
        Version metadata.
    set_current : bool
        Mark this version as current. Default True.

    Returns
    -------
    int | None
        artifact_version_id, or None.
    """
    return _call(ctx, "register_artifact_version", {
        "artifact_id":    artifact_id,
        "version_number": version_number,
        "status":         status,
        "body_format":    body_format,
        "body_hash":      body_hash,
        "body_text":      body_text,
        "source_uri":     source_uri,
        "metadata_jsonb": metadata_jsonb,
        "set_current":    set_current,
    })


def register_batch_artifact(
    ctx: Any,
    artifact_id: int,
    artifact_role: str,
    artifact_name: str,
    artifact_hash: Optional[str] = None,
    artifact_uri: Optional[str] = None,
    artifact_version_id: Optional[int] = None,
    metadata_jsonb: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """
    Associate an artifact with the current batch and step.

    Parameters
    ----------
    ctx : Any
        Application context.
    artifact_id : int
        ID from get_or_create_artifact.
    artifact_role : str
        Role in this batch (e.g. 'input', 'output', 'log').
    artifact_name : str
        Display name for this usage.
    artifact_hash : str, optional
        Hash of the artifact at usage time.
    artifact_uri : str, optional
        Location of the artifact.
    artifact_version_id : int, optional
        Specific version ID used.
    metadata_jsonb : dict, optional
        Usage metadata.

    Returns
    -------
    int | None
        batch_artifact_id, or None.
    """
    return _call(ctx, "register_batch_artifact", {
        "batch_id":            getattr(ctx, "batch_id", None),
        "batch_step_id":       getattr(ctx, "batch_step_id", None),
        "artifact_id":         artifact_id,
        "artifact_version_id": artifact_version_id,
        "artifact_role":       artifact_role,
        "artifact_name":       artifact_name,
        "artifact_hash":       artifact_hash,
        "artifact_uri":        artifact_uri,
        "metadata_jsonb":      metadata_jsonb,
    })


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def get_or_create_contract(
    ctx: Any,
    contract_name: str,
    contract_type: str,
    metadata_jsonb: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """
    Get or create a contract registry entry.

    Parameters
    ----------
    ctx : Any
        Application context.
    contract_name : str
        Unique contract name.
    contract_type : str
        Contract type (e.g. 'llm', 'sql', 'etl').
    metadata_jsonb : dict, optional
        Contract metadata.

    Returns
    -------
    int | None
        contract_id, or None.
    """
    return _call(ctx, "get_or_create_contract", {
        "contract_name":  contract_name,
        "contract_type":  contract_type,
        "metadata_jsonb": metadata_jsonb,
    })


def register_contract_version(
    ctx: Any,
    contract_id: int,
    version_number: str,
    status: str,
    contract_hash: str,
    contract_md: Optional[str] = None,
    input_schema_jsonb: Optional[dict[str, Any]] = None,
    output_schema_jsonb: Optional[dict[str, Any]] = None,
    metadata_jsonb: Optional[dict[str, Any]] = None,
    set_current: bool = True,
) -> Optional[int]:
    """
    Register a new version of a contract.

    Parameters
    ----------
    ctx : Any
        Application context.
    contract_id : int
        ID from get_or_create_contract.
    version_number : str
        Version label.
    status : str
        Version status (e.g. 'active', 'draft').
    contract_hash : str
        Hash of the contract content.
    contract_md : str, optional
        Raw contract markdown.
    input_schema_jsonb : dict, optional
        JSON schema for contract input.
    output_schema_jsonb : dict, optional
        JSON schema for contract output.
    metadata_jsonb : dict, optional
        Version metadata.
    set_current : bool
        Mark this version as current. Default True.

    Returns
    -------
    int | None
        contract_version_id, or None.
    """
    return _call(ctx, "register_contract_version", {
        "contract_id":          contract_id,
        "version_number":       version_number,
        "status":               status,
        "contract_hash":        contract_hash,
        "contract_md":          contract_md,
        "input_schema_jsonb":   input_schema_jsonb,
        "output_schema_jsonb":  output_schema_jsonb,
        "metadata_jsonb":       metadata_jsonb,
        "set_current":          set_current,
    })


def start_contract_run(
    ctx: Any,
    contract_id: int,
    contract_version_id: int,
    input_jsonb: Optional[dict[str, Any]] = None,
    metrics_jsonb: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """
    Register the start of a contract execution run.

    Parameters
    ----------
    ctx : Any
        Application context.
    contract_id : int
        ID of the contract being executed.
    contract_version_id : int
        ID of the contract version being executed.
    input_jsonb : dict, optional
        Input payload for this run.
    metrics_jsonb : dict, optional
        Pre-run metrics.

    Returns
    -------
    int | None
        contract_run_id, or None.
    """
    return _call(ctx, "start_contract_run", {
        "batch_id":            getattr(ctx, "batch_id", None),
        "batch_step_id":       getattr(ctx, "batch_step_id", None),
        "contract_id":         contract_id,
        "contract_version_id": contract_version_id,
        "input_jsonb":         input_jsonb,
        "metrics_jsonb":       metrics_jsonb,
    })


def end_contract_run(
    ctx: Any,
    contract_run_id: int,
    status: str,
    output_jsonb: Optional[dict[str, Any]] = None,
    metrics_jsonb: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Mark a contract run as complete.

    Parameters
    ----------
    ctx : Any
        Application context.
    contract_run_id : int
        ID from start_contract_run.
    status : str
        Final status (e.g. 'success', 'failed').
    output_jsonb : dict, optional
        Structured output from the contract.
    metrics_jsonb : dict, optional
        Runtime metrics.
    error_message : str, optional
        Error description when status is 'failed'.
    """
    _call(ctx, "end_contract_run", {
        "contract_run_id": contract_run_id,
        "status":          status,
        "output_jsonb":    output_jsonb,
        "metrics_jsonb":   metrics_jsonb,
        "error_message":   error_message,
    })


def save_contract_review(
    ctx: Any,
    contract_run_id: int,
    review_status: str,
    review_score: Optional[float] = None,
    reviewer: Optional[str] = None,
    review_notes: Optional[str] = None,
    edited_output_jsonb: Optional[dict[str, Any]] = None,
    improvement_notes: Optional[str] = None,
) -> Optional[int]:
    """
    Save a human review of a contract run.

    Parameters
    ----------
    ctx : Any
        Application context.
    contract_run_id : int
        ID of the reviewed run.
    review_status : str
        Review outcome (e.g. 'approved', 'rejected').
    review_score : float, optional
        Numeric quality score.
    reviewer : str, optional
        Reviewer identifier.
    review_notes : str, optional
        Free-form review notes.
    edited_output_jsonb : dict, optional
        Human-corrected output when the original was wrong.
    improvement_notes : str, optional
        Suggestions for contract improvement.

    Returns
    -------
    int | None
        contract_review_id, or None.
    """
    return _call(ctx, "save_contract_review", {
        "contract_run_id":      contract_run_id,
        "review_status":        review_status,
        "review_score":         review_score,
        "reviewer":             reviewer,
        "review_notes":         review_notes,
        "edited_output_jsonb":  edited_output_jsonb,
        "improvement_notes":    improvement_notes,
    })


# ---------------------------------------------------------------------------
# Logged SQL
# ---------------------------------------------------------------------------


def run_logged_sql(
    ctx: Any,
    sql_text: str,
    sql_name: str,
    context_jsonb: Optional[dict[str, Any]] = None,
) -> None:
    """
    Execute SQL through the control database's logged execution routine.

    Parameters
    ----------
    ctx : Any
        Application context.
    sql_text : str
        SQL statement to execute and log.
    sql_name : str
        Logical name for this SQL statement (appears in logs).
    context_jsonb : dict, optional
        Execution context.
    """
    _call(ctx, "run_logged_sql", {
        "batch_id":      getattr(ctx, "batch_id", None),
        "batch_step_id": getattr(ctx, "batch_step_id", None),
        "sql_text":      sql_text,
        "sql_name":      sql_name,
        "context_jsonb": context_jsonb,
    })


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_enabled(ctx: Any) -> bool:
    """Return True if ctx.control.enabled is set."""
    control_cfg = getattr(ctx, "control", None)
    if control_cfg is None:
        return False
    return bool(getattr(control_cfg, "enabled", False))


def _is_available(ctx: Any) -> bool:
    """Return True if control is enabled and has not been marked unavailable."""
    if not _is_enabled(ctx):
        return False
    return bool(getattr(ctx, "control_available", True))


def _map_name(ctx: Any) -> Optional[str]:
    """Return the configured control procedure-map name, or None."""
    control_cfg = getattr(ctx, "control", None)
    if control_cfg is None:
        return None
    return getattr(control_cfg, "procedure_map", None) or None


def _get_conn_cfg(ctx: Any) -> Optional[Any]:
    """Resolve the control DB connection config from ctx.

    The connection is bound through the control procedure map's
    ``connection_name`` field, resolved against ``ctx.db_connections``.
    Returns None when control is misconfigured.
    """
    map_name = _map_name(ctx)
    if not map_name:
        return None
    try:
        return get_connection_config(ctx, map_name)
    except ConfigError:
        return None


def _open_connection(ctx: Any) -> Optional[Any]:
    """
    Open a connection to the control database.

    Returns None and marks control unavailable on any failure.
    """
    conn_cfg = _get_conn_cfg(ctx)
    if conn_cfg is None:
        _mark_unavailable(ctx, "control connection config not found")
        return None
    try:
        return _db.get_connection(conn_cfg)
    except Exception as exc:  # noqa: BLE001
        _mark_unavailable(ctx, str(exc))
        return None


def _mark_unavailable(ctx: Any, reason: str) -> None:
    """
    Mark control as unavailable and log the reason.

    Sets ctx.control_available = False. Raises DatabaseError only when
    ctx.control.behavior.fail_app_on_control_error is True.
    """
    ctx.control_available = False
    _logger.warning("control_utils: control database unavailable — %s", reason)

    behavior = getattr(getattr(ctx, "control", None), "behavior", None)
    if behavior and getattr(behavior, "fail_app_on_control_error", False):
        raise DatabaseError(
            f"control_utils: control database unavailable — {reason}"
        )


def _call(
    ctx: Any,
    action_name: str,
    variables: dict[str, Any],
    set_ctx: Optional[str] = None,
) -> Optional[Any]:
    """
    Core dispatcher — check availability, open connection, call action.

    Catches all control failures, marks control unavailable, and returns
    None so the calling app can continue without the control database.

    Parameters
    ----------
    ctx : Any
        Application context.
    action_name : str
        Control action name.
    variables : dict[str, Any]
        Rey internal variable name → value for this call.
    set_ctx : str, optional
        When provided, stores the return value on ctx under this name.

    Returns
    -------
    Any | None
        Return value from the action, or None if control is unavailable.
    """
    if not _is_available(ctx):
        return None

    map_name = _map_name(ctx)
    if not map_name:
        _mark_unavailable(ctx, "ctx.control.procedure_map is not set")
        return None

    conn = _open_connection(ctx)
    if conn is None:
        return None

    try:
        result = execute_mapped_routine(
            ctx=ctx, conn=conn, procedure_map=map_name,
            routine_name=action_name, values=variables, run_ctx=ctx,
        )
        outputs = result.get("outputs") or {}
        scalar = next(iter(outputs.values()), None)
        if set_ctx is not None and scalar is not None:
            setattr(ctx, set_ctx, scalar)
        return scalar

    except (ConfigError, DatabaseError) as exc:
        _mark_unavailable(ctx, str(exc))
        return None
    except Exception as exc:  # noqa: BLE001
        _mark_unavailable(ctx, f"unexpected error in '{action_name}': {exc}")
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

"""The control database, as one object.

``Control`` is the sole runtime control API. It owns the resolved procedure map
named by ``control.procedure_map``, the runtime batch state, and nothing else.

What it owns
------------
- the resolved ``control`` procedure map, taken off the context at construction
- ``batch_id`` and ``batch_step_id``, the runtime state of control persistence
- the control behaviour settings read at call time

What it does not own
--------------------
- ``run_id``. Execution identity is established at the launch boundary through
  ``rey_lib.run`` and read from the context; a control object that could set it
  would be a second minting site.
- the connection's lifecycle, logging behaviour, launch decisions, and any
  provider-specific database logic, all of which stay where they already are.

Binding target
--------------
Control passes *itself* as the procedure map's ``run_ctx``, so a binding's
``output.load_to_ctx`` writes results onto Control rather than onto the
application context. That is what keeps ``batch_id`` off ``ctx`` without
Control naming the field: the map still declares where a routine's result
lands, and Control is merely the object it lands on.

Attribute lookups Control does not answer fall through to the context it was
built from, so a binding declaring an input Control does not hold resolves
exactly as it did before.
"""

from __future__ import annotations

from typing import Any, Optional

from rey_lib.db.db_adapter import DBAdapter
from rey_lib.db.procedure_map import execute_mapped_routine, get_procedure_map
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import get_logger

__all__ = ["Control"]

_logger = get_logger(__name__)
_db = DBAdapter()


def _name_of(record: Any) -> str:
    """Return a config record's name, namespace or mapping."""
    value = getattr(record, "name", None)
    if value is None and isinstance(record, dict):
        value = record.get("name")
    return str(value or "")


class Control:
    """Runtime access to the control database through its procedure map."""

    def __init__(self, ctx: Any) -> None:
        """Take ownership of the control procedure map.

        The map is resolved once, retained here, and removed from
        ``ctx.procedure_maps``. Removing it is the point: while it stayed on the
        context, anything holding the context could reach control routines
        without going through this object.

        Raises
        ------
        ConfigError
            When no control procedure map is named or it declares SQL bindings.
        """
        self._ctx = ctx
        # Seeded from the launch input, owned here afterwards. A run continuing
        # someone else's batch has to learn that id from somewhere, and launch
        # is the only place it can come from; every later write lands on this
        # object, never back onto the context.
        self.batch_id: Optional[int] = getattr(ctx, "batch_id", None)
        self.batch_step_id: Optional[int] = None

        control_cfg = getattr(ctx, "control", None)
        map_name = getattr(control_cfg, "procedure_map", None)
        if not map_name:
            raise ConfigError(
                "control: ctx.control.procedure_map is not set. The routine "
                "contract for control database calls must be named."
            )
        self._map_name = str(map_name)
        self._map = get_procedure_map(ctx, self._map_name)
        self._refuse_sql_bindings()

        # Only the named map is taken; other maps stay for their own owners.
        maps = list(getattr(ctx, "procedure_maps", None) or [])
        ctx.procedure_maps = [m for m in maps if _name_of(m) != self._map_name]

    def __getattr__(self, name: str) -> Any:
        """Fall through to the context for anything Control does not hold.

        A procedure map binding may name any Rey value as an input. Control
        holds the batch state and the context holds the rest, so an input this
        object does not answer resolves where it always did.
        """
        # __getattr__ runs only when normal lookup fails, so this cannot shadow
        # batch_id or batch_step_id.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.__dict__["_ctx"], name)

    # -- map ownership ------------------------------------------------------

    @property
    def procedure_map(self) -> Any:
        """The resolved map this object owns."""
        return self._map

    @property
    def procedure_map_name(self) -> str:
        """The name the map was resolved under."""
        return self._map_name

    def _refuse_sql_bindings(self) -> None:
        """Refuse a control map that declares SQL bindings.

        The control database is written through stored routines and nothing
        else. ``sql_bindings`` carry SQL text the generic executor runs
        directly, which is legitimate for application databases and never for
        this one: it would put an INSERT into a control table in a
        configuration file, outside the routines that own those tables.
        """
        bindings = getattr(self._map, "sql_bindings", None)
        if bindings is None and isinstance(self._map, dict):
            bindings = self._map.get("sql_bindings")
        if bindings:
            names = [_name_of(b) or str(b) for b in bindings]
            raise ConfigError(
                f"control: procedure map '{self._map_name}' declares sql_bindings "
                f"({', '.join(names)}). The control database is written through "
                "stored routines only; move the statement into a routine and "
                "declare it under routine_bindings."
            )

    # -- connection ---------------------------------------------------------

    def _connection_name(self) -> Optional[str]:
        """Return the connection the run store is written to, or None.

        ``logging.db_connection`` names it directly. It is not read from the
        procedure map: a map is a routine contract and says nothing about which
        database those routines run on.
        """
        logging_cfg = getattr(self._ctx, "logging", None)
        if logging_cfg is None:
            return None
        name = getattr(logging_cfg, "db_connection", None)
        if name is None and isinstance(logging_cfg, dict):
            name = logging_cfg.get("db_connection")
        return str(name) if name else None

    def _connection_config(self) -> Optional[Any]:
        """Resolve the control connection config, or None when misconfigured."""
        from rey_lib.db.procedure_map import resolve_connection_config

        connection_name = self._connection_name()
        if not connection_name:
            return None
        try:
            return resolve_connection_config(self._ctx, connection_name)
        except ConfigError:
            return None

    def _open_connection(self, required: bool = False) -> Optional[Any]:
        """Acquire a connection for one control call.

        Acquisition is unchanged from the procedural implementation this object
        replaces: obtained per call and closed after it. Control does not own
        the lifecycle and does not cache, clone or reuse a connection.
        """
        conn_cfg = self._connection_config()
        if conn_cfg is None:
            if required:
                raise ConfigError(
                    "control: no connection config for run-log persistence. "
                    "logging.db_connection must name a connection in connections."
                )
            self._mark_unavailable("control connection config not found")
            return None
        try:
            return _db.get_connection(conn_cfg, ctx=self._ctx)
        except Exception as exc:  # noqa: BLE001
            if required:
                raise DatabaseError(
                    f"control: could not open the run-store connection — {exc}"
                ) from exc
            self._mark_unavailable(str(exc))
            return None

    # -- availability -------------------------------------------------------

    def _is_enabled(self) -> bool:
        """Return True if control.enabled is set."""
        control_cfg = getattr(self._ctx, "control", None)
        if control_cfg is None:
            return False
        return bool(getattr(control_cfg, "enabled", False))

    def _is_available(self) -> bool:
        """Return True if control is enabled and has not been marked unavailable."""
        if not self._is_enabled():
            return False
        return bool(getattr(self._ctx, "control_available", True))

    def _mark_unavailable(self, reason: str) -> None:
        """Mark control unavailable and log why.

        Raises DatabaseError only when control.behavior.fail_app_on_control_error
        is set.
        """
        self._ctx.control_available = False
        _logger.warning("control: control database unavailable — %s", reason)

        behavior = getattr(getattr(self._ctx, "control", None), "behavior", None)
        if behavior and getattr(behavior, "fail_app_on_control_error", False):
            raise DatabaseError(f"control: control database unavailable — {reason}")

    def provider(self) -> Optional[str]:
        """Return the configured control DB provider name, or None if disabled."""
        if not self._is_enabled():
            return None
        conn_cfg = self._connection_config()
        return getattr(conn_cfg, "provider", None) if conn_cfg else None

    # -- identity (read, never minted) --------------------------------------

    def run_id(self) -> str:
        """Return the execution's run identity, refusing when absent.

        Read from the context, never created here. Identity is established at
        the launch boundary through ``rey_lib.run``; control does not reach
        upward to have one minted.
        """
        run_id = getattr(self._ctx, "run_id", None)
        if not run_id:
            raise ConfigError(
                "control: no run identity on the context. A run is identified at "
                "its launch boundary through rey_lib.run before any control DB "
                "interaction."
            )
        return str(run_id)

    def run_timestamp(self) -> str:
        """Return the filename-safe run timestamp, refusing when absent."""
        value = getattr(self._ctx, "run_timestamp", None)
        if not value:
            raise ConfigError(
                "control: no run_timestamp on the context. Run identity is "
                "established at the launch boundary through rey_lib.run."
            )
        return str(value)

    # -- dispatcher ---------------------------------------------------------

    def _call(self, action_name: str, variables: dict[str, Any],
              required: bool = False) -> Optional[Any]:
        """Execute one control routine through the owned map.

        Two failure contracts, chosen by the caller. ``required=False`` is the
        historical one for the optional capabilities: catch everything, mark
        control unavailable, return None, and let the application continue.
        ``required=True`` is for run-log persistence, where the destination was
        explicitly configured, so a missing configuration, an unopenable
        connection or a failing routine is an error. It also ignores
        ``control.enabled``, which governs the optional capabilities and must
        not veto a destination logging was configured to use.

        Result placement belongs to the map. ``run_ctx=self`` makes this object
        the binding target, so ``output.load_to_ctx`` writes onto Control.
        """
        if not required and not self._is_available():
            return None

        conn = self._open_connection(required=required)
        if conn is None:
            return None

        try:
            result = execute_mapped_routine(
                ctx=self._ctx, conn=conn, procedure_map=self._map_name,
                routine_name=action_name, values=variables, run_ctx=self,
                map_cfg=self._map,
            )
            outputs = result.get("outputs") or {}
            return next(iter(outputs.values()), None)
        except (ConfigError, DatabaseError) as exc:
            # The existing error type carries the cause; a required call
            # re-raises rather than wrapping, so no second hierarchy appears.
            if required:
                raise
            self._mark_unavailable(str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            if required:
                raise DatabaseError(
                    f"control: required routine '{action_name}' failed — {exc}"
                ) from exc
            self._mark_unavailable(f"unexpected error in '{action_name}': {exc}")
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # -- batch --------------------------------------------------------------

    def start_batch(self, batch_name: str, owner_app_name: Optional[str] = None,
                    context_jsonb: Optional[dict[str, Any]] = None,
                    required: bool = False) -> Optional[int]:
        """Register a new batch; the map binds the returned id onto ``batch_id``.

        A batch is a grouping identity: it contains runs and is not one of them.
        Neither ``run_id`` nor ``pipeline_name`` is supplied -- a batch carrying
        one execution's identity was the encoded assumption that a batch *is* a
        run, and a pipeline is one kind of thing a batch may group rather than a
        property of grouping itself.
        """
        return self._call("start_batch", {
            "batch_name":      batch_name,
            "owner_app_name":  owner_app_name or getattr(self._ctx, "app_name", None),
            "context_jsonb":   context_jsonb,
        }, required=required)

    def end_batch(self, status: str, error_message: Optional[str] = None,
                  context_jsonb: Optional[dict[str, Any]] = None,
                  required: bool = False) -> None:
        """Mark the current batch complete."""
        self._call("end_batch", {
            "batch_id":      self.batch_id,
            "status":        status,
            "error_message": error_message,
            "context_jsonb": context_jsonb,
        }, required=required)

    # -- steps --------------------------------------------------------------

    def start_step(self, step_name: str, step_sequence: Optional[int] = None,
                   step_type: Optional[str] = None, app_name: Optional[str] = None,
                   git_commit_hash: Optional[str] = None,
                   parent_batch_step_id: Optional[int] = None,
                   context_jsonb: Optional[dict[str, Any]] = None,
                   required: bool = False) -> Optional[int]:
        """Register a batch step; the map binds the id onto ``batch_step_id``.

        Carries the canonical ``run_id`` so a batch containing several runs can
        still say which execution produced this step.
        """
        return self._call("start_step", {
            "batch_id":             self.batch_id,
            "run_id":               getattr(self._ctx, "run_id", None),
            "step_sequence":        step_sequence,
            "step_name":            step_name,
            "step_type":            step_type,
            "app_name":             app_name or getattr(self._ctx, "app_name", None),
            "git_commit_hash":      git_commit_hash,
            "parent_batch_step_id": parent_batch_step_id,
            "context_jsonb":        context_jsonb,
        }, required=required)

    def end_step(self, status: str, message: Optional[str] = None,
                 metrics_jsonb: Optional[dict[str, Any]] = None,
                 context_jsonb: Optional[dict[str, Any]] = None,
                 required: bool = False) -> None:
        """Mark the current step complete."""
        self._call("end_step", {
            "batch_id":      self.batch_id,
            "batch_step_id": self.batch_step_id,
            "status":        status,
            "message":       message,
            "metrics_jsonb": metrics_jsonb,
            "context_jsonb": context_jsonb,
        }, required=required)

    # -- events -------------------------------------------------------------

    def log_event(self, severity: str, event_name: str, message: str,
                  event_jsonb: Optional[dict[str, Any]] = None,
                  required: bool = False) -> None:
        """Write one control log event.

        Carries ``run_id`` whether or not a step is open, so events from
        different runs sharing a batch stay distinguishable when
        ``batch_step_id`` is null.
        """
        self._call("log_event", {
            "batch_id":      self.batch_id,
            "batch_step_id": self.batch_step_id,
            "run_id":        getattr(self._ctx, "run_id", None),
            "severity":      severity,
            "event_name":    event_name,
            "message":       message,
            "event_jsonb":   event_jsonb,
        }, required=required)

    # -- config snapshots ---------------------------------------------------

    def save_config_snapshot(self, config_name: str, config_scope: Optional[str] = None,
                             config_format: Optional[str] = None,
                             config_hash: Optional[str] = None,
                             config_text: Optional[str] = None,
                             config_jsonb: Optional[dict[str, Any]] = None,
                             required: bool = False) -> Optional[int]:
        """Store a configuration snapshot against the current batch/step."""
        return self._call("save_config_snapshot", {
            "batch_id":      self.batch_id,
            "batch_step_id": self.batch_step_id,
            "config_name":   config_name,
            "config_scope":  config_scope,
            "config_format": config_format,
            "config_hash":   config_hash,
            "config_text":   config_text,
            "config_jsonb":  config_jsonb,
        }, required=required)

    # -- artifacts ----------------------------------------------------------

    def get_or_create_artifact(self, artifact_type: str, artifact_name: str,
                               metadata_jsonb: Optional[dict[str, Any]] = None,
                               required: bool = False) -> Optional[int]:
        """Resolve an artifact by type and name, creating it when absent."""
        return self._call("get_or_create_artifact", {
            "artifact_type":  artifact_type,
            "artifact_name":  artifact_name,
            "metadata_jsonb": metadata_jsonb,
        }, required=required)

    def register_artifact_version(self, artifact_id: int, version_number: Optional[int] = None,
                                  status: Optional[str] = None,
                                  body_format: Optional[str] = None,
                                  body_hash: Optional[str] = None,
                                  body_text: Optional[str] = None,
                                  source_uri: Optional[str] = None,
                                  metadata_jsonb: Optional[dict[str, Any]] = None,
                                  set_current: Optional[bool] = None,
                                  required: bool = False) -> Optional[int]:
        """Register one version of an artifact."""
        return self._call("register_artifact_version", {
            "artifact_id":    artifact_id,
            "version_number": version_number,
            "status":         status,
            "body_format":    body_format,
            "body_hash":      body_hash,
            "body_text":      body_text,
            "source_uri":     source_uri,
            "metadata_jsonb": metadata_jsonb,
            "set_current":    set_current,
        }, required=required)

    def register_batch_artifact(self, artifact_id: Optional[int] = None,
                                artifact_version_id: Optional[int] = None,
                                artifact_role: Optional[str] = None,
                                artifact_name: Optional[str] = None,
                                artifact_hash: Optional[str] = None,
                                artifact_uri: Optional[str] = None,
                                metadata_jsonb: Optional[dict[str, Any]] = None,
                                required: bool = False) -> Optional[int]:
        """Attach an artifact to the current batch/step."""
        return self._call("register_batch_artifact", {
            "batch_id":            self.batch_id,
            "batch_step_id":       self.batch_step_id,
            "artifact_id":         artifact_id,
            "artifact_version_id": artifact_version_id,
            "artifact_role":       artifact_role,
            "artifact_name":       artifact_name,
            "artifact_hash":       artifact_hash,
            "artifact_uri":        artifact_uri,
            "metadata_jsonb":      metadata_jsonb,
        }, required=required)

    # -- contracts ----------------------------------------------------------

    def get_or_create_contract(self, contract_name: str, contract_type: Optional[str] = None,
                               metadata_jsonb: Optional[dict[str, Any]] = None,
                               required: bool = False) -> Optional[int]:
        """Resolve an LLM contract by name, creating it when absent."""
        return self._call("get_or_create_contract", {
            "contract_name":  contract_name,
            "contract_type":  contract_type,
            "metadata_jsonb": metadata_jsonb,
        }, required=required)

    def register_contract_version(self, contract_id: int,
                                  version_number: Optional[int] = None,
                                  status: Optional[str] = None,
                                  contract_hash: Optional[str] = None,
                                  contract_md: Optional[str] = None,
                                  input_schema_jsonb: Optional[dict[str, Any]] = None,
                                  output_schema_jsonb: Optional[dict[str, Any]] = None,
                                  metadata_jsonb: Optional[dict[str, Any]] = None,
                                  set_current: Optional[bool] = None,
                                  required: bool = False) -> Optional[int]:
        """Register one version of an LLM contract."""
        return self._call("register_contract_version", {
            "contract_id":          contract_id,
            "version_number":       version_number,
            "status":               status,
            "contract_hash":        contract_hash,
            "contract_md":          contract_md,
            "input_schema_jsonb":   input_schema_jsonb,
            "output_schema_jsonb":  output_schema_jsonb,
            "metadata_jsonb":       metadata_jsonb,
            "set_current":          set_current,
        }, required=required)

    def start_contract_run(self, contract_id: Optional[int] = None,
                           contract_version_id: Optional[int] = None,
                           input_jsonb: Optional[dict[str, Any]] = None,
                           metrics_jsonb: Optional[dict[str, Any]] = None,
                           required: bool = False) -> Optional[int]:
        """Open a contract run against the current batch/step."""
        return self._call("start_contract_run", {
            "batch_id":            self.batch_id,
            "batch_step_id":       self.batch_step_id,
            "contract_id":         contract_id,
            "contract_version_id": contract_version_id,
            "input_jsonb":         input_jsonb,
            "metrics_jsonb":       metrics_jsonb,
        }, required=required)

    def end_contract_run(self, contract_run_id: int, status: str,
                         output_jsonb: Optional[dict[str, Any]] = None,
                         metrics_jsonb: Optional[dict[str, Any]] = None,
                         error_message: Optional[str] = None,
                         required: bool = False) -> None:
        """Close a contract run."""
        self._call("end_contract_run", {
            "contract_run_id": contract_run_id,
            "status":          status,
            "output_jsonb":    output_jsonb,
            "metrics_jsonb":   metrics_jsonb,
            "error_message":   error_message,
        }, required=required)

    def save_contract_review(self, contract_run_id: int, review_status: Optional[str] = None,
                             review_score: Optional[float] = None,
                             reviewer: Optional[str] = None,
                             review_notes: Optional[str] = None,
                             edited_output_jsonb: Optional[dict[str, Any]] = None,
                             improvement_notes: Optional[str] = None,
                             required: bool = False) -> Optional[int]:
        """Record a human review of a contract run."""
        return self._call("save_contract_review", {
            "contract_run_id":      contract_run_id,
            "review_status":        review_status,
            "review_score":         review_score,
            "reviewer":             reviewer,
            "review_notes":         review_notes,
            "edited_output_jsonb":  edited_output_jsonb,
            "improvement_notes":    improvement_notes,
        }, required=required)

    # -- logged SQL ---------------------------------------------------------

    def run_logged_sql(self, sql_text: str, sql_name: Optional[str] = None,
                       context_jsonb: Optional[dict[str, Any]] = None,
                       required: bool = False) -> None:
        """Run a statement through the control routine that logs it."""
        self._call("run_logged_sql", {
            "batch_id":      self.batch_id,
            "batch_step_id": self.batch_step_id,
            "sql_text":      sql_text,
            "sql_name":      sql_name,
            "context_jsonb": context_jsonb,
        }, required=required)

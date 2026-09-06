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

from rey_lib.db.connection import shared_connection
from rey_lib.db.procedure_map import (
    execute_mapped_routine, get_procedure_map, resolve_routine_binding,
)
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.logs import get_logger

__all__ = ["Control"]

_logger = get_logger(__name__)


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
        # The root step p_batch_start returned, retained until the batch ends.
        # Held separately from the open step because closing a step clears that
        # one, and an application step still has to know what it hangs from --
        # the alternative is making the database rediscover which step was this
        # batch's original root on every call.
        self.batch_root_step_id: Optional[int] = None
        # The application step currently open, or None between steps.
        self.batch_step_id: Optional[int] = None
        # Set by the RunLog that adopts this Control. Control persists on that
        # run log's behalf, so the SQL it runs is instrumented against it. None
        # until adopted, which is the case for the optional capabilities that
        # are not part of run-log persistence.
        self.run_log: Any = None
        # Whether this execution started the batch, so completion knows whether
        # ending it is its business. A batch may hold several runs; closing one
        # because a single run finished would close it under the others.
        self.owns_batch: bool = False
        # A reference to the shared object, not a config or a raw handle.
        self.connection = self._resolve_connection()

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

    def close(self) -> None:
        """Release this object's references at runtime collection.

        Explicit rather than inherited from ``__getattr__``, which would send
        ``close`` to the context and fail the whole teardown.

        It does not close the connection: ``Connection`` owns the handle and is
        collected in its own right, and closing a shared handle here would take
        it from every other consumer. What ends here is this object's part --
        the batch state it held and the run log it served.
        """
        self.batch_step_id = None
        self.batch_root_step_id = None
        self.run_log = None
        self.connection = None

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
        """Return the connection the control database is reached through, or None.

        ``control.connection`` names it, beside the procedure map it is used
        with. It is not read from the map itself: a map is a routine contract
        and says nothing about which database those routines run on.

        It used to be read from ``logging.db_connection``, which was right while
        Control existed only to persist run-log records. Control is a launch
        dependency now -- every run is recorded in ``control.run_manifest``
        before logging opens -- so an installation that logs to JSONL and never
        sets a logging connection still reaches the control database, through
        the key that describes it. ``logging.db_connection`` keeps its own job:
        it says where run *records* go when the run store is a database.
        """
        control_cfg = getattr(self._ctx, "control", None)
        if control_cfg is None:
            return None
        name = getattr(control_cfg, "connection", None)
        if name is None and isinstance(control_cfg, dict):
            name = control_cfg.get("connection")
        return str(name) if name else None

    def _resolve_connection(self) -> Optional[Any]:
        """Take the shared Connection this control database is reached through.

        Asked for by name, so Control references the same instance every other
        consumer of that name holds. It resolves the object, never a config:
        opening, reuse and closing belong to Connection.

        An unconfigured name answers None rather than raising. Control's
        optional capabilities are allowed to be unavailable, and the required
        path reports that through ``_handle``.
        """
        name = self._connection_name()
        if not name:
            return None
        try:
            return shared_connection(self._ctx, name)
        except ConfigError:
            return None

    def _handle(self, required: bool = False) -> Optional[Any]:
        """Return the shared connection's live handle, or None when unusable.

        The handle is opened by Connection on first use and reused after.
        Control never closes it: the object is shared, and a consumer closing
        it would take the handle from every other holder.
        """
        if self.connection is None:
            if required:
                raise ConfigError(
                    "control: no connection for run-log persistence. "
                    "logging.db_connection must name a connection in connections."
                )
            self._mark_unavailable("control connection not found")
            return None
        try:
            return self.connection.handle()
        except Exception as exc:  # noqa: BLE001
            if required:
                raise DatabaseError(
                    f"control: could not open the run-store connection — {exc}"
                ) from exc
            self._mark_unavailable(str(exc))
            return None

    # -- availability -------------------------------------------------------

    def _is_enabled(self) -> bool:
        """Return True if control.enabled is set.

        The flag governs the optional control capabilities -- artifacts,
        contracts, config snapshots, run_logged_sql -- and never run-log
        persistence. ``logging.run_store`` is authoritative for that, so a
        required call ignores this: an operator who selected the database as a
        run-log destination has already said it must be written, and a second
        switch able to veto that is the disagreement this separation removes.
        """
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
        return self.connection.provider if self.connection else None

    # -- identity (read, never minted) --------------------------------------

    @property
    def run_id(self) -> Any:
        """Return the execution's run identity, refusing when absent.

        Read from the context, never created here. Identity is the manifest's
        ``run_manifest_id``, established at the launch boundary when the run was
        recorded; control does not reach upward to have one made.

        Returned as it is held, not stringified. It is an integer key, and a
        string copy of it would not match the column it is written to.

        A property because a procedure-map binding resolves an unsupplied input
        by attribute: ``input: {p_run_id: run_id}`` reads ``run_id`` off this
        object, and as a method that handed the driver a bound method --
        "can't adapt type 'method'" -- rather than the identity. Seven bindings
        declare it and were safe only because every caller passed the value
        explicitly.
        """
        run_id = self.__dict__.get("_run_id") or getattr(self._ctx, "run_id", None)
        if not run_id:
            raise ConfigError(
                "control: no run identity on the context. A run is identified at "
                "its launch boundary by recording it, before any other control "
                "DB interaction."
            )
        return run_id

    @run_id.setter
    def run_id(self, value: Any) -> None:
        """Take the identity the run manifest generated.

        Control is both the object that reads run identity and the object the
        map writes it onto: ``create_run_manifest`` declares
        ``output.load_to_ctx: run_id`` and the map sets that attribute on its
        binding target, which is this object. Control is built before the run
        exists -- the run is created *through* it -- so a getter alone made the
        launch boundary fail on its own result.

        Still mints nothing. The value is the one the database generated.

        Held under ``_run_id`` because :meth:`__getattr__` refuses names
        beginning with an underscore rather than falling through to the
        context, so a context attribute cannot shadow the slot.
        """
        self.__dict__["_run_id"] = value

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

        conn = self._handle(required=required)
        if conn is None:
            return None

        try:
            result = execute_mapped_routine(
                ctx=self._ctx, run_log=self.run_log, conn=conn,
                procedure_map=self._map_name, routine_name=action_name,
                values=variables, run_ctx=self, map_cfg=self._map,
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
        # No close: the Connection is shared and outlives this call. Its
        # lifetime belongs to runtime shutdown, not to one control routine.

    def _call_rows(self, action_name: str, variables: dict[str, Any],
                   required: bool = False) -> list[dict[str, Any]]:
        """Execute one mapped routine that returns rows, and return them.

        The same call as :meth:`_call` with a different result to carry. A
        routine bound ``dataset_result`` puts its rows under ``rows`` and leaves
        ``outputs`` empty, so a scalar-shaped reader would silently return None
        for every row it fetched.

        Kept separate rather than widening ``_call``'s return: every existing
        caller of ``_call`` expects one value or None, and a union return would
        push the discrimination into each of them.
        """
        if not required and not self._is_available():
            return []

        conn = self._handle(required=required)
        if conn is None:
            return []

        try:
            result = execute_mapped_routine(
                ctx=self._ctx, run_log=self.run_log, conn=conn,
                procedure_map=self._map_name, routine_name=action_name,
                values=variables, run_ctx=self, map_cfg=self._map,
            )
            return list(result.get("rows") or [])
        except (ConfigError, DatabaseError):
            if required:
                raise
            self._mark_unavailable(f"routine '{action_name}' failed")
            return []
        except Exception as exc:  # noqa: BLE001
            if required:
                raise DatabaseError(
                    f"control: required routine '{action_name}' failed — {exc}"
                ) from exc
            self._mark_unavailable(f"unexpected error in '{action_name}': {exc}")
            return []

    # -- batch --------------------------------------------------------------

    def start_batch(self, batch_name: str, owner_app_name: Optional[str] = None,
                    context_jsonb: Optional[dict[str, Any]] = None,
                    required: bool = False) -> Optional[int]:
        """Start a batch and its root step, binding both. Returns ``batch_id``.

        A batch is a grouping identity: it contains runs and is not one of them.
        Neither ``run_id`` nor ``pipeline_name`` is supplied -- a batch carrying
        one execution's identity was the encoded assumption that a batch *is* a
        run, and a pipeline is one kind of thing a batch may group rather than a
        property of grouping itself.

        Starting a batch creates its root step in the same operation, so a
        batch never exists without one. That step becomes the parent for work
        performed under the batch, which is why both ids are bound here.
        """
        rows = self._call_rows("start_batch", {
            "batch_name":      batch_name,
            "owner_app_name":  owner_app_name or getattr(self._ctx, "app_name", None),
            "context_jsonb":   context_jsonb,
            # Explicit: resolving this from the context would find Control's own
            # run_id method and bind the method object as a parameter.
            "run_id":          getattr(self._ctx, "run_id", None),
        }, required=required)
        if not rows:
            return None
        self.batch_id = rows[0].get("o_batch_id")
        self.batch_root_step_id = rows[0].get("o_batch_step_id")
        self.batch_step_id = self.batch_root_step_id
        return self.batch_id

    def end_batch(self, status: str, error_message: Optional[str] = None,
                  context_jsonb: Optional[dict[str, Any]] = None,
                  required: bool = False) -> None:
        """Mark the current batch complete and release its state.

        All three go together. A root left behind would be offered as the parent
        for the next batch's steps, putting them under a step belonging to a
        batch that has already ended.
        """
        self._call("end_batch", {
            "batch_id":      self.batch_id,
            "status":        status,
            "error_message": error_message,
            "context_jsonb": context_jsonb,
        }, required=required)
        self.batch_id = None
        self.batch_root_step_id = None
        self.batch_step_id = None

    # -- steps --------------------------------------------------------------

    def start_step(self, step_name: str, step_sequence: Optional[int] = None,
                   step_type: Optional[str] = None, app_name: Optional[str] = None,
                   git_commit_hash: Optional[str] = None,
                   parent_batch_step_id: Optional[int] = None,
                   context_jsonb: Optional[dict[str, Any]] = None,
                   call_text: Optional[str] = None,
                   required: bool = False) -> Optional[int]:
        """Register a batch step; the map binds the id onto ``batch_step_id``.

        Carries the canonical ``run_id`` so a batch containing several runs can
        still say which execution produced this step.

        ``call_text`` is what this step ran, recorded so it can be read back.
        An application step often has no SQL to show, and NULL is the honest
        answer there; the routines that render their own invocation fill it in
        for themselves.
        """
        return self._call("start_step", {
            "batch_id":             self.batch_id,
            "run_id":               getattr(self._ctx, "run_id", None),
            "step_sequence":        step_sequence,
            "step_name":            step_name,
            "step_type":            step_type,
            "app_name":             app_name or getattr(self._ctx, "app_name", None),
            "git_commit_hash":      git_commit_hash,
            # Application steps are siblings under the batch root, so the
            # fallback is the retained root and not the step that happens to be
            # open. Nesting one application step inside another is done by
            # naming the parent, never by leaving it out.
            "parent_batch_step_id": (parent_batch_step_id
                                     if parent_batch_step_id is not None
                                     else self.batch_root_step_id),
            "context_jsonb":        context_jsonb,
            "call_text":            call_text,
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

    def write_run_log_record(
        self, *, run_id: Any, parent_run_log_id: Optional[int],
        nest_level: int, record_type: str, record_group: str,
        record_schema_version: int,
        record_subgroup: Optional[str] = None, message: Optional[str] = None,
        app: Optional[str] = None, workflow_name: Optional[str] = None,
        pipeline_name: Optional[str] = None, step_id: Optional[str] = None,
        step_name: Optional[str] = None, step_sequence: Optional[int] = None,
        correlation_id: Optional[str] = None, parent_run_id: Optional[Any] = None,
        subject_type: Optional[str] = None, subject_id: Optional[str] = None,
        subject_name: Optional[str] = None, pipeline_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None, path: Optional[str] = None,
        file_id: Optional[int] = None, source_name: Optional[str] = None,
        checksum_sha256: Optional[str] = None, size_bytes: Optional[int] = None,
        exists: Optional[bool] = None, modified_at: Optional[str] = None,
        error_message: Optional[dict[str, Any]] = None,
        payloads: Optional[dict[str, Any]] = None,
        required: bool = False,
    ) -> Optional[int]:
        """Write one run-log record, and return the row's id.

        The database mints ``run_log_id`` and it is the record's only
        identity; ``parent_run_log_id`` is its only parent, null at the root.
        ``batch_step_id`` is correlation only -- it says the record was written
        inside a governed step, and it is null when none is open. It has no
        part in identity, order or parentage.

        ``payloads`` is every typed jsonb column with its value -- the one the
        record type selects, and None for the rest -- so a row's structure is
        what its record type says it is. Which column belongs to which record
        type is the run log's to know, not this object's; there is no generic
        field bucket either way.

        ``run_timestamp`` is not a parameter: the run's launch stamp belongs to
        the run, and ``run_id`` reaches it through control.run_manifest.

        ``created_ts`` is not a parameter. The database stamps it, so a writer
        cannot backdate a log record.

        The row's id is returned because a governed record points at it: the
        log record is written first, and the file mutation carries its id.
        """
        return self._call("write_run_log_record", {
            "run_id":                run_id,
            "parent_run_log_id":     parent_run_log_id,
            "nest_level":            nest_level,
            "record_type":           record_type,
            "record_group":          record_group,
            "record_subgroup":       record_subgroup,
            "message":               message,
            "app":                   app,
            "workflow_name":         workflow_name,
            "pipeline_name":         pipeline_name,
            "step_id":               step_id,
            "step_name":             step_name,
            "step_sequence":         step_sequence,
            "correlation_id":        correlation_id,
            "parent_run_id":         parent_run_id,
            "subject_type":          subject_type,
            "subject_id":            subject_id,
            "subject_name":          subject_name,
            "pipeline_id":           pipeline_id,
            "workflow_id":           workflow_id,
            "record_schema_version": record_schema_version,
            "status":                status,
            "path":                  path,
            "file_id":               file_id,
            "source_name":           source_name,
            "checksum_sha256":       checksum_sha256,
            "size_bytes":            size_bytes,
            "exists":                exists,
            "modified_at":           modified_at,
            "error_message":         error_message,
            "batch_step_id":         self.batch_step_id,
            **dict(payloads or {}),
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
        """Run a statement through the control routine that logs it.

        The batch and the parent step are not passed here: the binding names
        them as inputs and the map resolves them off this object, which is
        where they already live.
        """
        self._call("run_logged_sql", {
            "sql_text":      sql_text,
            "sql_name":      sql_name,
            "context_jsonb": context_jsonb,
        }, required=required)

    # -- run manifest -------------------------------------------------------

    def create_run_manifest(self, subject_type: Optional[str] = None,
                            subject_id: Optional[str] = None,
                            subject_name: Optional[str] = None,
                            app_name: Optional[str] = None,
                            parent_run_id: Optional[int] = None,
                            settings: Optional[dict[str, Any]] = None,
                            started_at: Optional[str] = None,
                            status: str = "RUNNING",
                            required: bool = True) -> Optional[int]:
        """Record a starting run and return the id it now has.

        That id **is** the Run identity. Nothing supplies it: the manifest row
        generates it, and the value returned here is what the application
        carries as ``run_id`` for the rest of the execution.

        ``required`` defaults True, unlike the optional capabilities. A run that
        cannot be recorded has no identity, so there is nothing to continue as.

        Not idempotent. Calling this twice creates two runs, which is why a
        pipeline's step subprocesses inherit ``run_id`` instead of calling it.
        """
        return self._call("create_run_manifest", {
            "started_at":    started_at,
            "status":        status,
            "subject_type":  subject_type,
            "subject_id":    subject_id,
            "subject_name":  subject_name,
            "app_name":      app_name or getattr(self._ctx, "app_name", None),
            "parent_run_id": parent_run_id,
            "settings":      settings,
        }, required=required)

    def finish_run_manifest(self, run_id: int, status: str,
                            finished_at: Optional[str] = None,
                            required: bool = True) -> None:
        """Close a run: terminal status and when it ended, and nothing else.

        Settings were written at create and are not restated. Silent when the
        run is unknown -- finishing is teardown, and a raising teardown would
        mask whatever ended the run.
        """
        self._call("finish_run_manifest", {
            "run_id":      run_id,
            "status":      status,
            "finished_at": finished_at,
        }, required=required)

    def find_run_manifest(self, *, app_name: str = "", subject_type: str = "",
                          subject_id: str = "", status: str = "",
                          limit: Optional[int] = None,
                          required: bool = True) -> list[dict[str, Any]]:
        """Return the recorded runs matching every criterion supplied.

        Reads rows and nothing else. Which runs are interesting, and what to do
        about them, is the caller's.

        ``required`` defaults True for the same reason ``get_run_manifest``
        does: ``control.enabled`` governs the optional capabilities and must
        not veto the run domain. With it false this returned no rows, and a run
        left open read as no run at all.
        """
        return self._call_rows("find_run_manifest", {
            "app_name":     app_name or None,
            "subject_type": subject_type or None,
            "subject_id":   subject_id or None,
            "status":       status or None,
            "parent_run_id": None,
            "started_from": None,
            "started_to":   None,
            "limit":        limit,
        }, required=required)

    def get_run_manifest(self, run_id: int,
                         required: bool = True) -> Optional[dict[str, Any]]:
        """Return one run's durable record, or None when it was never recorded.

        This is what answers a poll once the live run is gone. It reads the row
        and nothing else -- no log file, no evidence projection -- because
        telling a caller the run is finished needs only the row.

        ``required`` defaults True, like create and finish. ``control.enabled``
        governs the optional capabilities -- artifacts, contracts, config
        snapshots -- and must not veto the run domain: with it false this
        returned no rows and every finished run read as one that was never
        recorded, which is the answer the manifest exists to stop.
        """
        rows = self._call_rows("get_run_manifest", {"run_id": run_id},
                               required=required)
        return rows[0] if rows else None

    # -- file manifest ------------------------------------------------------

    def inventory_file(self, path: str, file_name: str, base_name: str,
                       file_extension: str, checksum_sha256: str,
                       size_bytes: int, source_name: Optional[str] = None,
                       evidence: Optional[dict[str, Any]] = None,
                       producer: Optional[dict[str, Any]] = None,
                       required: bool = True) -> Optional[int]:
        """Record a file and the start of its history, returning its id.

        One call, and the routine writes both the manifest row and the baseline
        mutation. A file never exists without the record of where it was found.
        """
        return self._call("insert_file_manifest", {
            "path":            path,
            "file_name":       file_name,
            "base_name":       base_name,
            "file_extension":  file_extension,
            "checksum_sha256": checksum_sha256,
            "size_bytes":      size_bytes,
            "source_name":     source_name,
            "evidence":        evidence,
            "producer":        producer,
        }, required=required)

    def update_file_manifest(self, file_manifest_id: int,
                             required: bool = True, **fields: Any) -> None:
        """Change a file's current state. Absent fields are left alone."""
        values = {"file_manifest_id": file_manifest_id}
        for name in ("path", "file_name", "base_name", "file_extension",
                     "checksum_sha256", "size_bytes", "source_name",
                     "evidence", "producer", "data_profile_key",
                     "data_profile_id"):
            values[name] = fields.get(name)
        self._call("update_file_manifest", values, required=required)

    def ai_configuration(self, installation: str,
                         required: bool = True) -> list[dict[str, Any]]:
        """One installation's resolved AI configuration.

        The view owns the group/task inheritance, so what comes back is already
        resolved. Nothing here re-derives it.
        """
        return [
            dict(row) for row in (self._call_rows("get_ai_configuration", {
                "installation": str(installation),
            }, required=required) or [])
        ]

    def ai_instructions(self, required: bool = True) -> list[dict[str, Any]]:
        """Every instruction the estate offers, with its contract body."""
        return [
            dict(row) for row in
            (self._call_rows("get_ai_instruction", {}, required=required) or [])
        ]

    def data_profile_for_key(self, data_profile_key: str,
                             required: bool = True) -> Optional[int]:
        """The profile this group already has, or None.

        None means the group has not been profiled. A group is profiled once and
        never again, so this is what decides whether the work is done rather
        than something re-derived from the files in it -- and the id it answers
        with is what a file's manifest is stamped with.
        """
        rows = self._call_rows("get_data_profile", {
            "data_profile_key": str(data_profile_key),
        }, required=required) or []
        for row in rows:
            return int(dict(row)["data_profile_id"])
        return None

    def insert_data_profile(self, data_profile_key: str,
                            clear_profile: dict[str, Any],
                            redacted_profile: dict[str, Any],
                            required: bool = True) -> Optional[int]:
        """Persist one group's profile, as produced, and answer with its identity.

        One identity holding both readings. They describe a single profiling
        event, so a profile carrying one and not the other would describe
        something that never happened.

        The objects are stored as they were handed over. Nothing here reads
        them, decides what they mean, or takes them apart beyond the field rows
        the routine writes so a field is addressable.
        """
        return self._call("insert_data_profile", {
            "data_profile_key": str(data_profile_key),
            "clear_profile":    clear_profile,
            "redacted_profile": redacted_profile,
        }, required=required)

    def append_file_mutation(self, file_manifest_id: int, record_type: str,
                             action: str, status: Optional[str] = None,
                             source_record_id: Optional[int] = None,
                             run_log_id: Optional[int] = None,
                             path: Optional[str] = None,
                             deleted_in: Optional[int] = None,
                             deleted_ts: Optional[str] = None,
                                   producer: Optional[dict[str, Any]] = None,
                             conversion: Optional[dict[str, Any]] = None,
                             # Text: the reason, as the column holds it.
                             result: Optional[str] = None,
                             rollback: Optional[dict[str, Any]] = None,
                             classification: Optional[dict[str, Any]] = None,
                             clear_profile: Optional[dict[str, Any]] = None,
                             redacted_profile: Optional[dict[str, Any]] = None,
                             base_path: Optional[str] = None,
                             required: bool = True) -> Optional[int]:
        """Append one event to a file's history.

        The step that executed the action is not passed. The routine opens its
        own batch step and records that on the row, so what is stored is the
        database call that created the mutation rather than the application
        step above it -- which is the parent, and travels as
        ``in_parent_batch_step_id`` like every other governed call.
        """
        return self._call("insert_file_mutation", {
            "file_manifest_id":  file_manifest_id,
            "source_record_id":  source_record_id,
            "record_type":       record_type,
            "action":            action,
            "run_log_id": run_log_id,
            "path":              path,
            "status":            status,
            "deleted_in":        deleted_in,
            "deleted_ts":        deleted_ts,
            "producer":          producer,
            "conversion":        conversion,
            "result":            result,
            "rollback":          rollback,
            "classification":    classification,
            "clear_profile":     clear_profile,
            "redacted_profile":  redacted_profile,
            "base_path":         base_path,
        }, required=required)

    def get_current_classification(self, file_manifest_id: Optional[int] = None,
                                   required: bool = True) -> list[dict[str, Any]]:
        """Return what a file is classified as now, or every classified file.

        Classification is computed from the file's history rather than stored on
        the file, so it is asked for by name. Which of a file's classification
        events is the current one is the database's answer, decided in one
        routine -- a caller that re-derived it here would be a second authority
        on the same question.
        """
        return self._call_rows("get_current_classification",
                               {"file_manifest_id": file_manifest_id},
                               required=required)

    def get_file_manifest(self, file_manifest_id: int,
                          required: bool = True) -> Optional[dict[str, Any]]:
        """Return one file's current state, or None when it was never recorded."""
        rows = self._call_rows("get_file_manifest",
                               {"file_manifest_id": file_manifest_id},
                               required=required)
        return rows[0] if rows else None

    def find_file_manifest(self, path: Optional[str] = None,
                           checksum_sha256: Optional[str] = None,
                           source_name: Optional[str] = None,
                           file_name: Optional[str] = None,
                           limit: int = 100,
                           required: bool = True) -> list[dict[str, Any]]:
        """Return files matching every filter given. A NULL filter is not one."""
        return self._call_rows("find_file_manifest", {
            "path": path, "checksum_sha256": checksum_sha256,
            "source_name": source_name, "file_name": file_name, "limit": limit,
        }, required=required)

    def list_file_manifest(self, required: bool = True) -> list[dict[str, Any]]:
        """Return every current file, in order. No filter, no cap."""
        return self._call_rows("list_file_manifest", {}, required=required)

    def find_files(self,
                   file_extensions: Optional[list[str]] = None,
                   classified: Optional[bool] = None,
                   action: Optional[str] = None,
                   status: Optional[str] = None,
                   current_only: bool = True,
                   converted: Optional[bool] = None,
                   record_type: Optional[str] = None,
                   conversion_operator: Optional[str] = None,
                   result_reason: Optional[str] = None,
                   source_name: Optional[str] = None,
                   required: bool = True) -> list[dict[str, Any]]:
        """Return governed files as the database sees them, filtered.

        Rows come from ``control.file_vw`` -- every mutation carrying the file
        it is a mutation of -- so a caller receives identity, static facts and
        current location together and joins nothing itself.

        ``current_only`` is what "where is this file now" means relationally:
        the latest surviving mutation per governed file. False returns the
        history instead.

        ``converted`` asks whether the file has ever been converted -- a fact
        about the file, recorded on the mutation that performed the conversion.
        False selects the work still to do. A NULL filter is not a filter.

        ``record_type`` names one kind of lifecycle record, and
        ``conversion_operator`` names the operator that performed a conversion.
        The second is about a mutation rather than a file: ``converted`` cannot
        say which mutation converted, or by what, and a caller wanting the
        output of one operator reads that mutation's ``path`` as the file.

        ``result_reason`` and ``source_name`` are the remaining facts a
        configured selection declares: why a mutation was written, and which
        configured source a file was inventoried from.
        """
        return self._call_rows("find_files", {
            "file_extensions": list(file_extensions) if file_extensions else None,
            "classified": classified,
            "action": action,
            "status": status,
            "current_only": current_only,
            "converted": converted,
            "record_type": record_type,
            "conversion_operator": conversion_operator,
            "result_reason": result_reason,
            "source_name": source_name,
        }, required=required)

    def call_rows(self, binding_name: str,
                  values: Optional[dict[str, Any]] = None
                  ) -> list[dict[str, Any]]:
        """Execute one row-producing binding the caller names, and return rows.

        The generic form of every read above: those name one binding each, and
        this takes the name from whoever is asking. It exists so a selector can
        be chosen by configuration -- a new one is then a routine and a binding,
        with no Python -- and the caller still never sees a database routine
        name. What that binding maps to, which parameters it has and how it is
        invoked stay in the procedure map.

        ``values`` are runtime variable names, as the map's ``input`` reads
        them, not database parameter names.

        The call is required. :meth:`_call_rows` returns no rows rather than
        raising when control is unavailable, which is right for logging -- a run
        does not stop because its log did -- but here it would report a
        disabled or unreachable database as an empty result set, which is
        exactly what "no work to do" looks like. A selection that cannot ask
        the question must not answer it.

        Parameters
        ----------
        binding_name : str
            A logical binding in the control procedure map.
        values : Optional[dict[str, Any]]
            Runtime values for that binding's declared inputs.

        Returns
        -------
        list[dict[str, Any]]
            The rows the routine returned.

        Raises
        ------
        ConfigError
            If the binding does not produce rows, or is not in the map.
        """
        binding = resolve_routine_binding(self._map, self._map_name, binding_name)
        if binding["result_mode"] != "dataset_result":
            raise ConfigError(
                f"control: binding '{binding_name}' declares result_mode "
                f"'{binding['result_mode']}', which produces no rows. "
                "call_rows requires a dataset_result binding."
            )
        return self._call_rows(binding_name, dict(values or {}), required=True)

    def run_log_record(self, run_log_id: int,
                       required: bool = True) -> Optional[dict[str, Any]]:
        """Return one run-log record, whole, or None.

        Carries the run it belongs to, because a reader looking at one record
        needs to know what it was part of.
        """
        rows = self._call_rows("find_run_log_record",
                               {"run_log_id": run_log_id},
                               required=required)
        return rows[0] if rows else None

    def file_tree_nodes(self, parent_key: Optional[str] = None,
                        required: bool = True) -> list[dict[str, Any]]:
        """Return the children of one node in the File Manifest tree.

        The same shape as :meth:`run_tree_nodes`: one argument, the parent,
        whose key carries its own kind -- ``feed:<feed>``, ``file:<id>``,
        ``stage:<id>`` -- so a caller walking down passes back the ``node_key``
        it was given and never states what level it is on. ``None`` is the root.

        Which files a feed holds, which stages a file has and what each is
        called are the routine's; nothing here decides them.
        """
        return self._call_rows("find_file_tree_nodes",
                               {"parent_key": parent_key},
                               required=required)

    def run_tree_nodes(self, parent_key: Optional[str] = None,
                       required: bool = True) -> list[dict[str, Any]]:
        """Return the children of one node in the Run History tree.

        One argument, the parent, because a tree asks one question at a time.
        The key carries its own kind -- ``installation:<name>``, ``run:<id>``,
        ``record:<id>``, ``group:<run>:<anchor>:<subgroup>`` -- so a caller
        walking down passes back the ``node_key`` it was given and never states
        what level it is on. ``None`` is the root.

        Visibility, ancestry and grouping are the routine's; nothing here
        decides which records are drawn or what they are called.
        """
        return self._call_rows("find_run_tree_nodes",
                               {"parent_key": parent_key},
                               required=required)

    def file_history(self, file_manifest_id: int,
                     required: bool = True) -> list[dict[str, Any]]:
        """Return one file's mutations, oldest first."""
        return self._call_rows("find_file_mutation",
                               {"file_manifest_id": file_manifest_id},
                               required=required)

    def list_file_mutations(self, file_manifest_id: Optional[int] = None,
                            file_mutation_id: Optional[int] = None,
                            required: bool = True) -> list[dict[str, Any]]:
        """Return mutations in order: one mutation, one file's, or every one.

        A mutation is addressable on its own because it is what a File Manifest
        row stands for. Naming one narrows to it; naming neither returns them
        all, as it always did.
        """
        return self._call_rows("list_file_mutation",
                               {"file_manifest_id": file_manifest_id,
                                "file_mutation_id": file_mutation_id},
                               required=required)

    def files_for_run(self, run_id: int,
                      required: bool = True) -> list[dict[str, Any]]:
        """Return the files one run recorded, by the run's durable identity."""
        return self._call_rows("find_file_manifest_for_run",
                               {"run_id": run_id}, required=required)

    # -- rollback -----------------------------------------------------------
    #
    # A rollback is a record of its own, in control.file_mutation_rollback. The
    # request marks the set and returns it; completion closes the request whose
    # reversals all succeeded. The mutation rows themselves carry no rollback
    # state, so there is no queue here to read.

    def request_file_rollback(self, *, dry_run: bool = True,
                              file_mutation_id: Optional[int] = None,
                              file_manifest_id: Optional[int] = None,
                              batch_step_id: Optional[int] = None,
                              batch_id: Optional[int] = None,
                              run_id: Optional[int] = None,
                              required: bool = True) -> list[dict[str, Any]]:
        """Return the rollback set for one scope, marking it unless previewing.

        Exactly one scope is supplied. Under ``dry_run`` nothing is written and
        the rows come back with no rollback identity; otherwise each row is a
        requested rollback record. Either way the shape is the same, so a
        preview and its execution cannot describe different reversals.

        A row that can be reversed carries the command that reverses it. One
        that cannot is still returned -- it is a fact about the rollback -- and
        carries no command.
        """
        # Scoped names, not the ambient ones. A supplied value outranks the run
        # context, so passing `batch_step_id` here would blank the governing
        # step the map reads under that name -- which is what the rollback's own
        # batch steps hang under.
        return self._call_rows("request_file_rollback", {
            "rollback_dry_run":         bool(dry_run),
            "file_mutation_id":         file_mutation_id,
            "rollback_file_manifest_id": file_manifest_id,
            "rollback_batch_step_id":   batch_step_id,
            "rollback_batch_id":        batch_id,
            "rollback_run_id":          run_id,
            "rollback_execution_run_id": self._execution_run_id(),
        }, required=required)

    def complete_file_rollback(self, file_mutation_ids: list[int],
                               required: bool = True) -> None:
        """Close the rollbacks whose reversals actually ran.

        Named row by row, never by request. A requested rollback is the durable
        record that the work is still owed: a reversal that failed keeps its
        row and its mutation, so the next run finds it. Closing the whole
        request would record work nobody did and delete the history that proves
        it is still owed.
        """
        self._call("complete_file_rollback", {
            "rollback_file_mutation_ids": [int(i) for i in file_mutation_ids],
            "rollback_execution_run_id":  self._execution_run_id(),
        }, required=required)

    def _execution_run_id(self) -> Optional[int]:
        """The run performing a rollback, or None when there is not one.

        Not :meth:`run_id`, which refuses when the context carries no run: an
        invocation with no execution behind it is an ordinary case here, and the
        routines take NULL for it. The run being rolled back is a separate
        identity and travels as a scope.
        """
        return getattr(self._ctx, "run_id", None)

"""
Shared startup bootstrap for Rey process entry points.

The bootstrap owns context acquisition and logging initialization, and every
Rey process goes through it. The only variation is whether the bootstrap
receives an existing context or builds one itself. Logging is never owned by the
application entry point.

    process entry point
        -> bootstrap
        -> ctx supplied?  yes: use it   no: load and resolve configuration
        -> initialize logging from ctx
        -> return ctx
        -> application logic

Logging is initialized after the context exists because the log destination is
configuration: there is nowhere to write until the context names it. A failure
resolving configuration therefore reaches the process stream and nothing else,
which is the one startup window no run log can cover.

Public API
----------
  build_ctx_for_app(config_path, app_name, ...) -> ctx, logging started
"""

from __future__ import annotations

import signal
import threading
import time

from contextlib import contextmanager
from typing import Iterator

from pathlib import Path
from typing import Optional

from rey_lib.config.config_utils import Namespace, build_ctx_from_path
from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.logs import (
    AppLogger, classified, finalize_run_phases,
)
from rey_lib.db.connection import build_connections, connection_owner
from rey_lib.run import Run, establish_run_identity
from rey_lib.runtime import collect_runtime, register_runtime_object

#: The id of the ad-hoc text instruction every runtime offers.
AD_HOC_INSTRUCTION = "__ad_hoc__"

#: The id of the "send no instruction" choice every runtime offers.
#:
#: Reserved for the same reason the ad-hoc id is: it is a canonical choice that
#: is not a contract, so it needs a name of its own. It carried the empty string
#: until 2026-09-01, which a task already spends on "inherit the default" -- so
#: "send nothing" and "use whatever the default says" were one value and a task
#: could only express the second. A package that supplies its own instruction
#: needs the first.
NO_INSTRUCTION = "__none__"

__all__ = [
    "ai_connections", "ai_control", "ai_query_operations",
    "app_runtime", "build_ctx_for_app", "open_shared_control",
]

_logger = AppLogger(__name__)


def build_ctx_for_app(
    installation_config_path: Optional[Path] = None,
    app_name: str = "",
    project_root: Optional[Path] = None,
    *,
    ctx: Optional[Namespace] = None,
    operation: str = "app",
    subject_type: str = "",
    subject_id: str = "",
    subject_name: str = "",
    parent_run_id: Optional[int] = None,
    settings: Optional[dict] = None,
) -> Namespace:
    """Start a Rey process: acquire the context, then start logging from it.

    Either supply a context that is already resolved, or supply the path to
    resolve one from. Whichever way the context arrives, logging is initialized
    here from the final context, so no entry point starts its own.

    If a directory is given, config.yaml is tried first, then app.yaml.

    Parameters
    ----------
    installation_config_path : Path, optional
        Path to a config.yaml, app.yaml, or the directory containing one.
        Required unless ``ctx`` is supplied.
    app_name : str
        Name of the active app. Informational only — not used for path
        lookup in the new contract.
    project_root : Path, optional
        Unused in the new contract; retained for API compatibility.
    ctx : Namespace, optional
        A context the caller has already resolved. Supplied by in-process
        callers that hold one; one process never hands a context to another.
    operation : str
        The operation name this process is starting, which names its run log.
    subject_type : str
        What kind of thing is running: ``workflow``, ``pipeline``, ``app``.
        Creation facts, supplied by the launch site because that is where they
        are known; they are never recovered later from the run log.
    subject_id : str
        The subject's identifier, stable across runs of the same subject.
    subject_name : str
        Display name; falls back to ``subject_id``.
    parent_run_id : int, optional
        The run this one belongs to. A child process supplies the parent's
        ``run_id`` and does not start a second run.
    settings : dict, optional
        The launch request, recorded once when the run is created.

    Returns
    -------
    Namespace
        The context, with logging initialized against it.

    Raises
    ------
    ConfigError
        If neither a context nor a config path is supplied, or the config path
        names no loadable file.
    """
    if ctx is None:
        if installation_config_path is None:
            raise ConfigError(
                "build_ctx_for_app requires either a resolved ctx or a config path."
            )
        ctx = _resolve_ctx(installation_config_path, app_name, project_root)

    # The configuration is checked here rather than at the first query: a
    # duplicate connection name fails at composition, where it can be read as a
    # configuration fault. The objects themselves belong to the runtime owner,
    # not to this context -- a context that held the only copy is what once
    # scoped sharing to a context instead of to the runtime.
    #
    # Checked before the run exists, because the run is created in the database
    # and cannot be created without a connection to create it in.
    build_connections(ctx)

    # One registration, for the owner. Collecting it closes every connection it
    # built: no consumer may close a shared object, so the boundary that owns
    # shutdown owns the last close.
    register_runtime_object(ctx, connection_owner())

    # The app's launch boundary. Recording the run is what creates its
    # identity: the manifest row generates run_manifest_id and the application
    # carries that value as run_id. A child process arrives with the parent's
    # run_id already set and does not start a second run.
    started_here = None
    if not getattr(ctx, "run_id", None):
        # What the batch is called. Read off the context by Control, because
        # `operation` is a parameter here and Control is built from the context
        # alone -- without this the batch takes the app's name and the run's
        # purpose is lost from the record.
        ctx.operation = operation
        # Building it starts its batch. The batch is not started here: it was
        # once, as a second call after this line, and a setting vetoed it
        # silently and left a Control whose batch_id was None until a workflow
        # step hit the NOT NULL on control.batch_step.
        ctx.shared_control = _open_control(ctx)
        # The installation this process runs under, recorded with the run. The
        # bootstrap is its authority: a caller's own "installation" is replaced.
        installation = getattr(ctx, "installation", None)
        if installation is not None and installation.name:
            settings = {**(settings or {}), "installation": installation.name}
        ctx.run = Run.start(
            ctx.shared_control,
            subject_type=subject_type or "app",
            subject_id=subject_id or app_name or str(getattr(ctx, "app_name", "") or ""),
            subject_name=subject_name or operation,
            app_name=str(getattr(ctx, "owner_app_name", "")
                         or getattr(ctx, "app_name", "") or app_name or ""),
            parent_run_id=parent_run_id,
            settings=settings,
        )
        ctx.run_id = ctx.run.run_id
        started_here = ctx.run
    elif getattr(ctx, "shared_control", None) is None:
        # A process that adopted its run still writes governed records under it.
        # Control was opened only when a run was started, so an adopting child
        # inherited the identity and no way to reach the database -- every
        # manifest write refused with "this context exposes no shared Control".
        ctx.shared_control = _open_control(ctx)

    # A run started here is this function's until the context is handed on: what
    # fails before then ends it, rather than leaving it RUNNING with nobody left
    # to finish it (#1182).
    try:
        # Display and filing only; the identity above is already settled.
        establish_run_identity(ctx)

        _settle_log_level(ctx)
        from rey_lib.logs.log_writer import build_log_writer

        AppLogger._configure(ctx, operation, build_log_writer(ctx, operation))
    except BaseException as exc:
        if started_here is not None:
            # A termination carries the status its application declared; anything
            # else ending the run here is a failure.
            _finish_unhanded(started_here,
                             exc.status if isinstance(exc, Terminated) else "FAILED")
        raise
    return ctx


def _finish_unhanded(run: Any, status: str) -> None:
    """Record the terminal status of a run that ended before its context was handed on.

    Never replaces the error that ended it: a run whose record cannot be closed is
    reported, and the original failure goes on.
    """
    try:
        run.finish(status)
    except Exception as exc:  # noqa: BLE001
        error = exc if isinstance(exc, AppError) else AppError(f"unexpected {type(exc).__name__}")
        if error is not exc:
            error.__cause__ = exc
        error.report(
            f"recording the terminal status of run {getattr(run, 'run_id', '?')}",
            source=__name__,
        )


#: What a level may be. Read from the one map that applies it, so this cannot
#: drift from what ``AppLogger._configure`` actually honours.
def _levels() -> tuple[str, ...]:
    from rey_lib.logs.app_logger import _LEVEL_MAP

    return tuple(sorted(_LEVEL_MAP))


#: What a process records when nothing declares otherwise.
#:
#: ERROR rather than INFO: a run should be quiet unless something asks it not to
#: be, and an application that operationally needs more says so in its own
#: declaration.
DEFAULT_LOG_LEVEL = "ERROR"


def _application_level(ctx: Namespace) -> Optional[str]:
    """The level the running application declares for itself, or None.

    Read from ``ctx.applications`` -- the canonical collection built once at the
    end of the config load -- by ``ctx.app_name``. Nothing here re-reads the
    installation's declaration: a second reader of application configuration is
    a second answer waiting to disagree with the first.

    Raises:
        ConfigError: when this context names an application and carries
            applications, but none of them is it. That is a broken context
            rather than an absent declaration, and reading it as "inherit"
            would hide the breakage behind a plausible level.
    """
    app_name = str(getattr(ctx, "app_name", "") or "")
    applications = getattr(ctx, "applications", None) or ()
    if not app_name or not applications:
        return None

    running = next(
        (one for one in applications if str(getattr(one, "name", "")) == app_name),
        None,
    )
    if running is None:
        known = ", ".join(sorted(str(getattr(one, "name", "")) for one in applications))
        raise ConfigError(
            f"This context runs as '{app_name}', which is not one of the "
            f"applications it carries: {known or 'none'}. An application cannot "
            "resolve its own configuration when it is not in it."
        )
    return str(getattr(running, "log_level", "") or "") or None


def _settle_log_level(ctx: Namespace) -> None:
    """Resolve the level this process logs at, once, and refuse an unknown name.

    THE ONE PLACE THE PRECEDENCE CHAIN IS APPLIED::

        --log-level  >  the application's own declaration
                     >  the installation's log_level  >  ERROR

    Each term is only reachable because the one before it can be absent, which
    is why ``--log-level`` is recorded on its own attribute rather than onto
    ``ctx.log_level``: sharing one attribute with configuration would make the
    first term win whenever either had spoken.

    What leaves here is a single settled ``ctx.log_level``. ``AppLogger._configure``
    reads that and nothing else -- it learns nothing about command lines,
    applications or installations, and the chain is not partly resolved anywhere
    upstream.

    Case is normalized at this boundary, so ``debug`` from YAML and ``DEBUG``
    from a command line are the same level. An unrecognised name is still
    refused: ``AppLogger._configure`` maps one it does not know to a default silently,
    so a typo in an installation's YAML would otherwise produce a run that looks
    entirely normal and records the wrong amount.

    Raises:
        ConfigError: when a level is declared that nothing can honour.
    """
    from rey_lib.config.cli import REQUESTED_LOG_LEVEL_ATTR

    for value, source in (
        (getattr(ctx, REQUESTED_LOG_LEVEL_ATTR, None), "--log-level"),
        (_application_level(ctx), f"application '{getattr(ctx, 'app_name', '')}'"),
        (getattr(ctx, "log_level", None), "the installation's log_level"),
    ):
        if value is None or str(value).strip() == "":
            continue
        if str(value).upper() not in _levels():
            raise ConfigError(
                f"log_level '{value}', declared by {source}, is not a level this "
                f"runtime knows. Valid levels are {', '.join(_levels())}."
            )
        object.__setattr__(ctx, "log_level", str(value).upper())
        return

    object.__setattr__(ctx, "log_level", DEFAULT_LOG_LEVEL)


def _resolve_ctx(
    installation_config_path: Path,
    app_name: str,
    project_root: Optional[Path],
) -> Namespace:
    """Load and resolve a context from a config.yaml or app.yaml path."""
    config_path = Path(installation_config_path).expanduser().resolve()

    if config_path.is_dir():
        for name in ("config.yaml", "app.yaml"):
            candidate = config_path / name
            if candidate.exists():
                config_path = candidate
                break
        else:
            raise ConfigError(
                f"No config.yaml or app.yaml found in: {config_path}"
            )

    return build_ctx_from_path(config_path, app_name=app_name, project_root=project_root)


def _open_control(ctx: Namespace) -> Any:
    """Build this process's one Control and register it for collection.

    One per process. The run is created through it before logging opens, and
    the run log reaches the control database through the same object, so a
    second Control would mean a second connection and two objects disagreeing
    about which batch is open.

    Control used to be built only when a database destination was selected,
    which made it a dependency of DB logging. It is a dependency of launching
    now: every run is recorded in ``control.run_manifest`` and that row is what
    mints the run's identity, so a process that cannot reach the control
    database has no id to run under. ``logging.run_store`` still decides where
    run *records* go and no longer decides whether Control exists.

    Raises
    ------
    ConfigError
        When the installation configures no control database. Reported as what
        it means -- this installation cannot record a run, so it cannot start
        one -- because the underlying message names a missing key and reads as
        a logging problem.
    """
    from rey_lib.control import Control

    try:
        control = Control(ctx)
    except ConfigError as exc:
        raise ConfigError(
            f"This installation cannot start a run: {exc} Every run is recorded "
            "in control.run_manifest and that row is what gives the run its id, "
            "so an installation that reaches no control database cannot launch "
            "one. Configure control and its connection for this app's scope."
        ) from exc
    register_runtime_object(ctx, control)
    # The one installation settlement (backlog 887): the Control exists and the
    # selected installation is known, so the name becomes its id here, once per
    # opened Control. A standalone CLI context declares no installation.
    installation = getattr(ctx, "installation", None)
    if installation is not None:
        control.set_installation(installation.name)
    return control


def open_shared_control(ctx: Namespace) -> Namespace:
    """Make a resolved context into a runtime by giving it its one Control.

    One seam: the assignment lives
    here rather than at each caller, so ``ctx.shared_control`` is written in one
    place however many kinds of runtime exist -- an application's, and a Console
    page session's for the installation it is on.

    **Exactly once per retained graph, and that is the point.** ``Control``
    takes the control procedure map off ``ctx.procedure_maps`` when it is
    built, deliberately, so nothing holding the context can reach control
    routines around the object. A second ``Control`` against the same context
    therefore refuses -- the map it needs is already owned. That is harmless
    where a context is resolved per request and fatal where one is retained and
    reused, which is what a Console page session does.

    Idempotent, so a runtime already carrying one is left alone rather than
    opening a second and orphaning the first.

    **Absence is an ordinary state, exactly as it is for the AI.** Not every
    installation configures a control database -- the Console's own does not --
    and a runtime without one is not broken. Opening it here must not decide
    that: a context that names no control map is left carrying ``None``, and a
    consumer that genuinely needs one still refuses at the point of use, with
    the message that names what is missing. Raising here instead turned a
    per-route refusal into a session that could not be built at all, so an
    installation without control could do nothing whatever.

    Returns
    -------
    Namespace
        The same context, now carrying ``shared_control`` -- ``None`` when this
        installation configures no control database.

    Raises
    ------
    ConfigError
        When control is configured and one could not be built.
    """
    if getattr(ctx, "shared_control", None) is not None:
        return ctx
    # The same test Control makes before it does anything: no named routine
    # contract means this installation has not asked for a control database.
    if not getattr(getattr(ctx, "control", None), "procedure_map", None):
        ctx.shared_control = None
        return ctx
    ctx.shared_control = _open_control(ctx)
    return ctx


def ai_connections(ctx: Any) -> tuple[str, ...]:
    """The connections this installation lets a language model query through.

    Read here because this is what composes both halves: the connections are the
    database layer's and whether a model may use one is each connection's own
    declaration, so the answer is resolved on this side and the names alone are
    handed over. The AI never sees a Connection.

    A configuration that declares none is the ordinary state -- most
    installations give a model no database at all.
    """
    from rey_lib.db.connection import build_connections  # noqa: PLC0415

    try:
        built = build_connections(ctx)
    except Exception:  # noqa: BLE001 - no usable connections is "none offered"
        _logger.debug("No connections could be built for AI use")
        return ()
    permitted = tuple(sorted(
        name for name, connection in built.items() if connection.llm
    ))
    # Said at startup, because the alternative is discovering it from a model
    # that answers without querying and never says why. Both halves: what was
    # configured, and what of it a model may use.
    _logger.info(
        "AI may query %d of %d configured connection(s): %s",
        len(permitted), len(built), ", ".join(permitted) or "none",
        extra=classified(False),
    )
    return permitted


def ai_query_operations(
    ctx: Any, permitted: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Per offered connection, the query operation a governed AI tool binds (backlog 741).

    Composition only. The connection is the database layer's, acquired here
    where the runtime is composed; execution is DatabaseQuery's; how many rows an
    answer may hold is the calling tool's governed ``result_row_limit``, passed
    straight through, and whether a result stands at that limit is read from the
    same value. Nothing else is decided here, and the AI receives only the
    callable -- never the connection.
    """
    from rey_lib.db.database_query import DatabaseQuery  # noqa: PLC0415
    from rey_lib.files.json import json_value  # noqa: PLC0415

    built = build_connections(ctx)

    def query_on(connection: Any) -> Any:
        def query(arguments: dict[str, Any], tool: Any) -> dict[str, Any]:
            limit = tool.result_row_limit
            if not limit:
                raise ConfigError(
                    f"AI tool '{tool.name}' states no result_row_limit, which a "
                    "query needs to say how many rows one answer may hold."
                )
            results = DatabaseQuery(str(arguments["sql"])).run(
                connection.handle(), limit=limit,
            )
            return {
                "results": [
                    {
                        "columns": list(result.columns),
                        "rows": [json_value(row) for row in result.rows],
                        "row_count": result.row_count,
                        "at_row_limit": len(result.rows) >= limit,
                    }
                    for result in results
                ],
                "row_limit": limit,
            }
        return query

    return {
        name: {"database.query": query_on(built[name])}
        for name in permitted if name in built
    }


def ai_control(ctx: Namespace) -> Any:
    """The Control this runtime reads its AI configuration through, if any.

    AI configuration is held in the control database, so a runtime exposing no
    Control has no engine set to read and therefore no AI. That is an absence,
    not a refusal: ``open_shared_control`` leaves a context carrying ``None``
    wherever an installation configures no control database -- the Console's
    own does not -- and refusing here would stop such a runtime from starting
    at all.

    Whether an installation configured AI used to be readable off the context
    before any database was reached. It is not any more, so a missing Control
    cannot be called a configuration defect; it is simply no engines.
    """
    return getattr(ctx, "shared_control", None)


def open_run_log(ctx: Namespace, *, phase_started: float | None = None) -> Any:
    """Construct the one run log this process writes through, and what it needs.

    Named for the run log because that is what it returns and what the caller
    wants. It also builds Control when -- and only when -- a database
    destination is selected, because Control exists in production for exactly
    one reason: it is how the run log reaches the control database. Building it
    anywhere else would either construct it for runs that never persist to a
    database, or split the destination decision from the dependency that
    decision creates.

    Control is registered for runtime collection in its own right. The run log
    holds a reference and does not close it.

    The composition root builds it, because this is where the finished context
    exists and where the objects a run owns are already assembled. Everything
    the run log needs is read once, here; the run log itself holds no context
    and reads none afterwards.

    Parameters
    ----------
    ctx : Namespace
        The launch context, with run identity established and logging set up.

    Returns
    -------
    Any
        The process's ``RunLog``.
    """
    from rey_lib.logs.app_logger import AppLogger
    from rey_lib.logs.record_enrichment import _lineage_value
    from rey_lib.logs.run_log import DOMAIN_FIELDS, LINEAGE_FIELDS, RunLog

    # REUSED, NEVER BUILT. The run was created through this Control before
    # logging opened, and it is the Control holding the open batch. Building a
    # second one here would open a second connection to the same database and
    # the two would disagree about which batch is open.
    #
    # The run log opens and closes its steps on it. Where its RECORDS go is the
    # logging writer's (logging.run_store), not this Control's.
    control = getattr(ctx, "shared_control", None)

    lineage = {}
    for field in (*LINEAGE_FIELDS, *DOMAIN_FIELDS):
        found = _lineage_value(ctx, field)
        if found:
            lineage[field] = found

    if AppLogger._writer is None:
        from rey_lib.errors.error_utils import StateError

        raise StateError(
            "open_run_log: logging is not configured. build_ctx_for_app assembles "
            "the logging writer before the run log opens; there is nothing to "
            "write this run's records through."
        )
    run_log = RunLog(
        phase_started=phase_started,
        app=str(getattr(ctx, "owner_app_name", "") or getattr(ctx, "app_name", "")
                or getattr(ctx, "name", "") or ""),
        run_id=ctx.run_id,
        run_timestamp=ctx.run_timestamp,
        # The one writer and builder AppLogger's configuration assembled: the
        # run's typed records and its application events share them.
        writer=AppLogger._writer,
        builder=AppLogger._builder,
        control=control,
        workflow=getattr(ctx, "workflow_name", None),
        pipeline=getattr(ctx, "pipeline_name", None),
        lineage=lineage,
    )
    _surrender_adopted_fields(ctx)
    return run_log


# Fields the run log adopts wholesale: after construction they exist on the run
# log and nowhere else. Adoption is a move, not a copy -- a value left on the
# context is a second place the same fact can be read from and a second place it
# can drift.
#
# Only fields with no remaining reader outside this construction are listed. The
# identity and naming facts the rest of the estate legitimately reads --
# run_id, run_timestamp, app_name, pipeline_name, workflow_name, log_file -- are
# execution facts the run log copied, not run-log state, and removing them is a
# wider change than run-log ownership.
_ADOPTED_FIELDS = (
    "owner_app_name",
    "parent_run_id",
    "subject_type",
    "subject_id",
    "subject_name",
    "pipeline_run_id",
    "workflow_run_id",
    "pipeline_id",
    "workflow_id",
)


def _surrender_adopted_fields(ctx: Namespace) -> None:
    """Remove from the context every field the run log has just taken over.

    Leaving them behind is how the migration would quietly reintroduce what it
    removed: a caller reads ``ctx.parent_run_id`` to stamp a record, the run log
    stamps its own, and the two disagree the moment one of them moves.

    ``ctx.runtime`` is left alone. It is the pipeline's inherited snapshot, not
    this context's own fields, and it is how a subprocess step receives the
    enclosing run in the first place.
    """
    for field in _ADOPTED_FIELDS:
        try:
            delattr(ctx, field)
        except (AttributeError, TypeError):
            # Absent, or a context that refuses attribute removal. Either way
            # there is nothing left to read from.
            pass


#: What SIGTERM ends an application's run as -- its entry point's termination
#: policy (#1182) -- and the exit code that termination leaves with.
TERMINATION_EXIT_CODES: dict[str, int] = {"SUCCEEDED": 0, "ABORTED": 143}


class Terminated(SystemExit):
    """SIGTERM, read as the application's own termination policy said (#1182).

    A SystemExit, so every ``except Exception`` lets it pass unconverted; it carries
    the run's terminal status, and leaves the process with that status's exit code.
    """

    def __init__(self, status: str) -> None:
        super().__init__(TERMINATION_EXIT_CODES[status])
        self.status = status


@contextmanager
def app_runtime(
    *args: Any, on_terminate: Optional[str] = None, **kwargs: Any,
) -> Iterator[Any]:
    """The process boundary (:func:`_app_runtime`), with its termination policy.

    ``on_terminate`` is the entry point's answer to what SIGTERM means for this
    application -- ``SUCCEEDED`` (a normal stop) or ``ABORTED`` (abandoned): the
    one mechanism installs it on the main thread BEFORE the run is created and
    restores what it replaced when the process boundary ends. None installs
    nothing, and SIGTERM keeps its default meaning.

    Raises:
        ConfigError: When ``on_terminate`` names no termination status.
    """
    restore = _terminate_as(on_terminate)
    try:
        with _app_runtime(*args, **kwargs) as opened:
            yield opened
    finally:
        restore()


def _terminate_as(policy: Optional[str]) -> Any:
    """Make SIGTERM raise ``Terminated(policy)``; answer the call that restores it.

    Only on the main thread, where Python runs signal handlers; elsewhere, or with
    no policy, nothing is installed and nothing needs restoring.
    """
    if policy is None or threading.current_thread() is not threading.main_thread():
        return lambda: None
    if policy not in TERMINATION_EXIT_CODES:
        raise ConfigError(
            f"on_terminate must be one of {sorted(TERMINATION_EXIT_CODES)}, not {policy!r}.")

    def _terminate(_signum: int, _frame: Any) -> None:
        raise Terminated(policy)

    previous = signal.signal(signal.SIGTERM, _terminate)
    return lambda: signal.signal(signal.SIGTERM, previous)


@contextmanager
def _app_runtime(*args: Any, **kwargs: Any) -> Iterator[Any]:
    """Compose the launch context and collect its shared objects when done.

    The standard entry point for an application process:

        with app_runtime(config_path, app_name, operation) as (ctx, run_log):
            ...application work...

    Takes the same arguments as :func:`build_ctx_for_app` and yields that
    context together with the one run log this process writes through. Both
    are yielded rather than one carrying the other: the context describes the
    execution, the run log owns run logging, and neither is reachable through
    the other. What this adds beyond composition is the other end -- the shared
    objects it created are closed when the block exits, however it exits.

    Collection lives here rather than in ``run_app_operation`` because that
    helper also wraps nested sub-app operations, and collecting there would
    close connections the surrounding run is still using. A process has one
    end; this is it.

    A cleanup failure is raised only when the application block succeeded. If
    the block is already failing, the failure is reported and the original
    exception continues -- cleanup never replaces the error that ended the run.
    """
    # The runtime boundary. Captured before context composition so the first
    # phase covers that work, and carried into the run log rather than taken
    # again there -- the timeline must start where the run started, not where
    # the object recording it happened to be built.
    #
    # Not the process boundary: argument pre-parsing and imports precede this,
    # and claiming to cover them would overstate what the mark measures.
    phase_started = time.monotonic()
    ctx = build_ctx_for_app(*args, **kwargs)
    # From here the run is this block's: everything after it is inside the
    # boundary below, so a failure opening what the run writes through still
    # ends the run (#1182).
    run_log = None
    failed = False
    # The status a termination declared, which nothing below may override.
    terminal: Optional[str] = None
    try:
        run_log = open_run_log(ctx, phase_started=phase_started)
        register_runtime_object(ctx, run_log)
        # The run this process started writes through the run log opened for it;
        # a child Run started under it writes through the same one.
        started = getattr(ctx, "run", None)
        if started is not None:
            started.log = run_log

        yield ctx, run_log
    except Terminated as exc:
        # SIGTERM, as this application's policy reads it (#1182).
        terminal = exc.status
        failed = exc.status != "SUCCEEDED"
        raise
    except SystemExit as exc:
        # Entry points end with sys.exit(code). A zero exit is a successful run
        # that happens to unwind through an exception, so a cleanup failure is
        # still worth raising; a non-zero one is the failure itself and must
        # not be replaced.
        failed = bool(exc.code)
        raise
    # THE APPLICATION ERROR BOUNDARY (backlog 703, rule 79). What crosses it is
    # an AppError, reported here once; anything else is normalised into one,
    # its cause kept. SystemExit, KeyboardInterrupt and other non-Exception
    # exits are not errors on this path.
    except AppError as exc:
        failed = True
        exc.report("the application run ended on an error", source=__name__)
        raise
    except Exception as exc:
        failed = True
        error = AppError(f"unexpected {type(exc).__name__}")
        error.__cause__ = exc
        error.report("the application run ended on an unexpected error", source=__name__)
        raise error from exc
    except BaseException:
        failed = True
        raise
    finally:
        # The run's timeline closes here and nowhere else. A finally is what
        # makes it every termination path rather than the four returns anyone
        # thought to annotate -- an unplanned exception ends a run too, and is
        # the run whose phases are worth the most. None when the run log itself
        # could not be opened.
        if run_log is not None:
            finalize_run_phases(run_log)
        # Terminal status first, before anything this run owns is collected.
        # A poll that arrives after the process is gone is answered from the
        # manifest, so the row has to say how the run ended before the run
        # stops being able to say it. A child process, which inherited its
        # run_id, does not finish a run it did not start.
        run = getattr(ctx, "run", None)
        if run is not None:
            try:
                # `failed` is what this block saw; run_failed is what the
                # application reported through its own exit code. Either is a
                # failed run.
                failed = failed or bool(getattr(ctx, "run_failed", False))
                run.finish(terminal or ("FAILED" if failed else "SUCCEEDED"))
            except Exception as exc:  # noqa: BLE001
                # Never replace the error that ended the run, and never turn a
                # successful run into a failed one because its record could not
                # be closed. The run still happened.
                error = exc if isinstance(exc, AppError) else AppError(
                    f"unexpected {type(exc).__name__}")
                if error is not exc:
                    error.__cause__ = exc
                error.report(
                    f"recording the terminal status of run {getattr(run, 'run_id', '?')}",
                    source=__name__,
                )

        # THE OUTCOME, AS A FACT -- not a shutdown instruction. Control closes
        # its own batch when it is collected, a few lines below; what it cannot
        # work out for itself is how the run ended. This is the only thing that
        # knows, so it says so and nothing more.
        #
        # Set outside the `run is not None` block: a process that opened a
        # Control and failed before creating a run still has an outcome, and
        # Control decides for itself whether it owns a batch to apply it to.
        control = getattr(ctx, "shared_control", None)
        if control is not None:
            control.run_outcome = "failed" if failed else "success"

        # The ambient run binding is process state, not a registered object:
        # a collected run log must not stay bound for whatever runs next.
        from rey_lib.logs.record_enrichment import reset_run_binding

        reset_run_binding()
        collect_runtime(ctx, suppress=failed)

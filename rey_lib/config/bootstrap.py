"""
Shared startup bootstrap for Rey process entry points.

The bootstrap owns context acquisition, logging initialization and the process
error boundary, and every Rey process goes through it. The only variation is
whether the bootstrap receives an existing context or builds one itself. Neither
logging nor process-level error handling is ever owned by the application entry
point.

    process entry point
        -> bootstrap
        -> ctx supplied?  yes: use it   no: load and resolve configuration
        -> initialize logging from ctx
        -> install the process error boundary
        -> return ctx
        -> application logic

Logging is initialized after the context exists because the log destination is
configuration: there is nowhere to write until the context names it. A failure
resolving configuration therefore reaches the process stream and nothing else,
which is the one startup window no run log can cover.

The error boundary is installed next, so it writes through the logger that now
exists. It covers only what nothing else caught; every error an application
handles stays inside the application.

Public API
----------
  build_ctx_for_app(config_path, app_name, ...) -> ctx, logging started
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from pathlib import Path
from typing import Optional

from rey_lib.config.config_utils import Namespace, build_ctx_from_path
from rey_lib.errors.error_utils import ConfigError, install_process_error_boundary
from rey_lib.logs import get_logger, setup_logging
from rey_lib.db.connection import build_connections, connection_owner
from rey_lib.run import Run, establish_run_identity
from rey_lib.runtime import collect_runtime, register_runtime_object

__all__ = ["build_ctx_for_app", "app_runtime"]

_logger = get_logger(__name__)


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
    if not getattr(ctx, "run_id", None):
        ctx.shared_control = _open_control(ctx)
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
    elif getattr(ctx, "shared_control", None) is None:
        # A process that adopted its run still writes governed records under it.
        # Control was opened only when a run was started, so an adopting child
        # inherited the identity and no way to reach the database -- every
        # manifest write refused with "this context exposes no shared Control".
        ctx.shared_control = _open_control(ctx)

    # Display and filing only; the identity above is already settled.
    establish_run_identity(ctx)

    setup_logging(ctx, operation=operation)
    install_process_error_boundary(ctx)
    return ctx


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
    return control


def _open_ai(ctx: Namespace) -> Any:
    """Build this runtime's one AI, when the installation configures one.

    **Optional, unlike Control.** Not every installation has AI configured, and
    the existence of an installation does not imply the existence of an AI. When
    ``ctx.llm`` names nothing, this returns ``None`` and the runtime simply has
    no AI -- an ordinary capability state, not a failure. A non-AI application
    boots and runs exactly as it did.

    Built here for the same reason RunLog is: one object per runtime invocation,
    from configuration read in one place, so the dependency surface is closed
    rather than ctx exploded across callers. Consumers reach it through
    ``ctx.shared_ai`` and never construct their own.

    ctx is a construction input only. Nothing this returns retains a reference
    to it -- a structural guard in the AI test suite enforces that the ``ctx``
    identifier appears nowhere in ``rey_lib.ai`` outside its construction seam.

    Returns
    -------
    Any
        The runtime's ``AI``, or ``None`` when no AI is configured.
    """
    configured = getattr(ctx, "llm", None)
    if not configured:
        return None

    from rey_lib.ai.construction import ai_from_ctx
    from rey_lib.ai.errors import AIError

    try:
        return ai_from_ctx(ctx, profiles=_ai_profiles(ctx))
    except AIError as exc:
        # Configuration that names an AI but cannot build one is a fault worth
        # reporting, not a silent absence -- an operator who configured a
        # provider should not discover at the first request that it never
        # existed. The run continues without AI rather than failing to launch,
        # because an application that does not use AI must not be stopped by
        # another application's misconfiguration.
        _logger.error("AI is configured for this runtime but could not be built: %s", exc)
        return None


def _ai_profiles(ctx: Namespace) -> tuple[Any, ...]:
    """The public selection projections this runtime offers.

    One profile per configured entry, carrying the access policy that entry
    declares. Capability is not read here: the adapter is the authority on what
    a configured provider can do, and a profile that stated its own could
    advertise something no adapter implements.
    """
    from rey_lib.ai.profiles import AIProfile

    configured = getattr(ctx, "llm", None) or {}
    names = (
        list(configured.keys()) if hasattr(configured, "keys")
        else [n for n in vars(configured) if not str(n).startswith("_")]
    )
    profiles: list[Any] = []
    for name in names:
        entry = (
            configured.get(name) if hasattr(configured, "get")
            else getattr(configured, name, None)
        )
        access = (
            entry.get("profile_access") if hasattr(entry, "get")
            else getattr(entry, "profile_access", None)
        )
        profiles.append(
            AIProfile(
                id=str(name),
                name=str(name),
                configured_provider_id=str(name),
                profile_access=access,
            ),
        )
    return tuple(profiles)


def open_run_log(ctx: Namespace) -> Any:
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
    from rey_lib.logs.record_enrichment import _lineage_value
    from rey_lib.logs.run_log import DOMAIN_FIELDS, LINEAGE_FIELDS, RunLog
    from rey_lib.logs.run_store import run_store_mode

    destination = run_store_mode(ctx)
    control = None
    if destination in ("db", "both"):
        # Reused, not rebuilt: the run was created through this Control before
        # logging opened. A second one would open a second connection to the
        # same database and the two would disagree about which batch is open.
        #
        # Still gated on the destination. The run log needs Control only when
        # it writes records to the database; the run needed it to exist at all,
        # which is a different requirement and is met at the launch boundary.
        control = getattr(ctx, "shared_control", None) or _open_control(ctx)

    lineage = {}
    for field in (*LINEAGE_FIELDS, *DOMAIN_FIELDS):
        found = _lineage_value(ctx, field)
        if found:
            lineage[field] = found

    run_log = RunLog(
        app=str(getattr(ctx, "owner_app_name", "") or getattr(ctx, "app_name", "")
                or getattr(ctx, "name", "") or ""),
        run_id=ctx.run_id,
        run_timestamp=ctx.run_timestamp,
        log_dir=getattr(ctx, "run_log_dir", None),
        path=getattr(ctx, "log_file", None),
        destination=destination,
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
    "run_log_dir",
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


@contextmanager
def app_runtime(*args: Any, **kwargs: Any) -> Iterator[Any]:
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
    ctx = build_ctx_for_app(*args, **kwargs)
    run_log = open_run_log(ctx)
    register_runtime_object(ctx, run_log)

    # Optional: present only when this installation configures AI. Absent is an
    # ordinary state, and no consumer may construct one lazily instead.
    ctx.shared_ai = _open_ai(ctx)
    failed = False
    try:
        yield ctx, run_log
    except SystemExit as exc:
        # Entry points end with sys.exit(code). A zero exit is a successful run
        # that happens to unwind through an exception, so a cleanup failure is
        # still worth raising; a non-zero one is the failure itself and must
        # not be replaced.
        failed = bool(exc.code)
        raise
    except BaseException:
        failed = True
        raise
    finally:
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
                run.finish("FAILED" if failed else "SUCCEEDED")
            except Exception as exc:  # noqa: BLE001
                # Never replace the error that ended the run, and never turn a
                # successful run into a failed one because its record could not
                # be closed. The run still happened.
                _logger.error("Could not record terminal status for run %s: %s",
                              getattr(run, "run_id", "?"), exc)

        # The ambient run binding is process state, not a registered object:
        # a collected run log must not stay bound for whatever runs next.
        from rey_lib.logs.record_enrichment import reset_run_binding

        reset_run_binding()
        collect_runtime(ctx, suppress=failed)

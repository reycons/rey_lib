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
from rey_lib.logs import setup_logging
from rey_lib.db.connection import build_connections
from rey_lib.run import establish_run_identity
from rey_lib.runtime import collect_runtime, register_runtime_object

__all__ = ["build_ctx_for_app", "app_runtime"]


def build_ctx_for_app(
    installation_config_path: Optional[Path] = None,
    app_name: str = "",
    project_root: Optional[Path] = None,
    *,
    ctx: Optional[Namespace] = None,
    operation: str = "app",
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

    # The app's launch boundary. Identity is established here, through the
    # subsystem that owns it, so logging receives a context already carrying
    # a run_id rather than reaching up to have one minted.
    establish_run_identity(ctx)

    # One Connection per configured connection, built once here so every
    # consumer that later names one receives the same instance rather than
    # opening its own. Each is registered for final collection: no consumer
    # may close a shared object, so the boundary that created them owns the
    # last close.
    ctx.shared_connections = build_connections(ctx)
    for connection in ctx.shared_connections.values():
        register_runtime_object(ctx, connection)

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
        from rey_lib.control import Control

        control = Control(ctx)
        register_runtime_object(ctx, control)

    lineage = {}
    for field in (*LINEAGE_FIELDS, *DOMAIN_FIELDS):
        found = _lineage_value(ctx, field)
        if found:
            lineage[field] = found

    return RunLog(
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
        # The ambient run binding is process state, not a registered object:
        # a collected run log must not stay bound for whatever runs next.
        from rey_lib.logs.record_enrichment import reset_run_binding

        reset_run_binding()
        collect_runtime(ctx, suppress=failed)

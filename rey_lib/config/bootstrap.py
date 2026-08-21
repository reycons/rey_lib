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


@contextmanager
def app_runtime(*args: Any, **kwargs: Any) -> Iterator[Any]:
    """Compose the launch context and collect its shared objects when done.

    The standard entry point for an application process:

        with app_runtime(config_path, app_name, operation) as ctx:
            ...application work...

    Takes the same arguments as :func:`build_ctx_for_app` and yields the same
    context. What it adds is the other end: the shared objects composition
    created are closed when the block exits, however it exits.

    Collection lives here rather than in ``run_app_operation`` because that
    helper also wraps nested sub-app operations, and collecting there would
    close connections the surrounding run is still using. A process has one
    end; this is it.

    A cleanup failure is raised only when the application block succeeded. If
    the block is already failing, the failure is reported and the original
    exception continues -- cleanup never replaces the error that ended the run.
    """
    ctx = build_ctx_for_app(*args, **kwargs)
    failed = False
    try:
        yield ctx
    except BaseException:
        failed = True
        raise
    finally:
        collect_runtime(ctx, suppress=failed)

"""
Shared startup bootstrap for Rey process entry points.

The bootstrap owns both context acquisition and logging initialization, and
every Rey process goes through it. The only variation is whether the bootstrap
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

from pathlib import Path
from typing import Optional

from rey_lib.config.config_utils import Namespace, build_ctx_from_path
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import setup_logging

__all__ = ["build_ctx_for_app"]


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

    setup_logging(ctx, operation=operation)
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

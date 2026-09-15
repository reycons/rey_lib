"""
Shared CLI argument helpers for Rey app entry points.

Centralizes the pre-parse/load_dotenv pattern and argparse argument
declarations that are otherwise duplicated across every Rey app.

Public API
----------
  preparse_config_args()              Pre-parse --config-path/--config-dir and call load_dotenv.
  add_config_args(parser)             Add shared config/pipeline args to a parser.
  apply_env_overrides(items)          Write --set KEY=VALUE pairs into os.environ.
  build_ctx_from_args(args, app_name) Build ctx from parsed args and app identity.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from dotenv import load_dotenv

if TYPE_CHECKING:
    from rey_lib.config.config_utils import Namespace

__all__ = [
    "preparse_config_args",
    "add_config_args",
    "apply_env_overrides",
    "build_ctx_from_args",
    "load_ctx_snapshot",
]

def preparse_config_args() -> None:
    """Pre-parse --config-path/--config-dir from sys.argv and load the declared env file.

    Must be called at module level in each app entry point, before any
    imports that depend on environment variables being set.

    THE ROOT CONFIG DECLARES THE FILE, here as everywhere. ``security.env_file``
    is read from whichever root config the command line identifies, and that
    declaration is loaded -- no directory is joined to it, no ``.env`` filename
    is assumed, and nothing is probed when an installation declares none. This
    runs before the config is built, so it reads the declaration itself rather
    than applying a convention the context build would then contradict.

    Which root config
    -----------------
    1. --config-path
    2. --config-dir, which names the directory holding it
    3. APP_CONFIG_DIR, the same
    Without any of them nothing is loaded: there is no config to read a
    declaration from, and guessing one is what this replaced.
    """
    from rey_lib.config.config_loader import declared_env_file

    pre = argparse.ArgumentParser(add_help=False)

    pre.add_argument("--config-path", dest="config_path", default=None)
    pre.add_argument("--config-dir",  dest="config_dir",  default=None)

    pre_args, _ = pre.parse_known_args()

    if pre_args.config_path:
        config_path: Optional[Path] = Path(pre_args.config_path).expanduser()
    else:
        config_dir = pre_args.config_dir or os.environ.get("APP_CONFIG_DIR")
        config_path = _root_config_in(Path(config_dir).expanduser()) if config_dir else None

    if config_path is None or not config_path.is_file():
        return

    env_file = declared_env_file(config_path)
    if env_file is not None and env_file.exists():
        load_dotenv(dotenv_path=env_file, override=False)


def _root_config_in(config_dir: Path) -> Optional[Path]:
    """Return the root config in a directory named without a file, or None.

    ``--config-dir`` and ``APP_CONFIG_DIR`` name a directory; the declaration
    lives in the config inside it. Only these two names are looked for, and a
    directory holding neither simply has no declaration to read -- that is a
    smaller assumption than the one this replaced, which read a file named
    ``.env`` from a directory nothing had declared.
    """
    for name in ("installation.yaml", "config.yaml"):
        candidate = config_dir / name
        if candidate.is_file():
            return candidate
    return None


def add_config_args(parser: argparse.ArgumentParser) -> None:
    """Add shared config/environment/pipeline args to a parser.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        Parser to augment in-place.
    """

    parser.add_argument(
        "--config-path",
        dest="config_path",
        default=None,
        help="Path to the app config.yaml or app.yaml file.",
    )

    parser.add_argument(
        "--config-dir",
        dest="config_dir",
        default=None,
        help="Path to the config directory (overrides APP_CONFIG_DIR).",
    )

    parser.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        dest="env_overrides",
        default=[],
        help="Override a .env variable for this run (repeatable).",
    )

    parser.add_argument(
        "--connection-alias",
        action="append",
        metavar="CONFIGURED=RUNTIME",
        dest="connection_aliases",
        default=[],
        help=(
            "Reach the connection configured as CONFIGURED through the connection "
            "named RUNTIME, for this run only (repeatable). Both must already be "
            "declared in the installation's connections; nothing in the "
            "configuration is changed. Anything resolving the configured name -- "
            "including the control database every run records itself in -- gets the "
            "replacement."
        ),
    )

    # ---------------------------------------------------------------------
    # Pipeline coordinator arguments
    # ---------------------------------------------------------------------

    parser.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        help=(
            "The run this process executes under, created by whoever launched "
            "it. Supplied, the process records its work against that run "
            "rather than starting a second one."
        ),
    )

    parser.add_argument(
        "--pipeline-name",
        dest="pipeline_name",
        default=None,
        help="Pipeline name provided by the pipeline coordinator.",
    )

    parser.add_argument(
        "--pipeline-run-id",
        dest="pipeline_run_id",
        default=None,
        help="Unique pipeline run identifier.",
    )

    parser.add_argument(
        "--pipeline-step-name",
        dest="pipeline_step_name",
        default=None,
        help="Pipeline step name.",
    )

    parser.add_argument(
        "--pipeline-step-id",
        dest="pipeline_step_id",
        default=None,
        help="Optional unique pipeline step identifier.",
    )

    parser.add_argument(
        "--log-level",
        dest="log_level",
        default=None,
        help=(
            "How much this run records: DEBUG, INFO, WARNING or ERROR. "
            "Overrides log_level in the installation configuration for this "
            "run only."
        ),
    )

    parser.add_argument(
        "--log-file",
        dest="log_file",
        default=None,
        help="Shared pipeline JSONL log file path.",
    )

    parser.add_argument(
        "--ctx-file",
        dest="ctx_file",
        default=None,
        help="Path to a pipeline step ctx snapshot (JSON). Mutually exclusive with --config-path.",
    )


def load_ctx_snapshot(ctx_file: str) -> "Namespace":
    """Load a pipeline step ctx snapshot from a JSON file.

    Plain JSON deserializer — does not read YAML, call config-loading
    machinery, discover config folders, or apply pipeline overrides.

    Validates ctx_schema_version == "1.0" and reconstructs a PathResolver
    from the serialized paths dict so apps can call ctx.paths.resolve().

    Parameters
    ----------
    ctx_file : str
        Path to the ctx snapshot JSON file written by the pipeline coordinator.

    Returns
    -------
    Namespace
        Context object equivalent to the one produced by build_ctx_from_path,
        with ctx.paths as a PathResolver over pre-resolved path strings.

    Raises
    ------
    RuntimeError
        If the file is missing, malformed, or has an unsupported schema version.
    """
    import json

    from rey_lib.config.config_utils import Namespace, PathResolver

    path = Path(ctx_file).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot load ctx snapshot {ctx_file!r}: {exc}") from exc

    version = data.get("ctx_schema_version")
    if version != "1.0":
        raise RuntimeError(f"Unsupported ctx snapshot version: {version!r}")

    ctx_data: dict = data["ctx"]
    ctx = Namespace(ctx_data)

    paths_raw: dict = ctx_data.get("paths") or {}
    path_resolver = PathResolver({k: Path(v) for k, v in paths_raw.items()})
    object.__setattr__(ctx, "paths", path_resolver)

    return ctx


def build_ctx_from_args(args: argparse.Namespace, app_name: str) -> "Namespace":
    """Build app context from parsed CLI args and the app's own identity.

    Validates that ``--config-path`` was supplied, resolves the path, and
    delegates to :func:`rey_lib.config.config_utils.build_ctx_from_path` with
    ``app_name`` so the correct include folders are selected.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed argument namespace.  Must have a ``config_path`` attribute
        (added by :func:`add_config_args`).
    app_name : str
        The app's own identity constant (e.g. ``"rey_console"``).

    Returns
    -------
    Namespace
        Fully populated context with ``ctx.config_path`` and ``ctx.app_name``.

    Raises
    ------
    SystemExit
        If ``--config-path`` was not provided.
    """
    from rey_lib.config.config_utils import build_ctx_from_path
    from rey_lib.errors.error_utils import ConfigError

    ctx_file = getattr(args, "ctx_file", None)
    config_path = getattr(args, "config_path", None)

    if ctx_file and config_path:
        raise RuntimeError("--ctx-file and --config-path are mutually exclusive")

    if ctx_file:
        try:
            ctx = load_ctx_snapshot(ctx_file)
        except (RuntimeError, OSError) as exc:
            raise SystemExit(f"FATAL: failed to load config - {exc}") from exc
        object.__setattr__(ctx, "app_name", app_name)
        log_file = getattr(args, "log_file", None)
        if log_file:
            resolved_log_file = str(Path(log_file).expanduser().resolve())
            object.__setattr__(ctx, "log_file", resolved_log_file)
            object.__setattr__(ctx, "jsonl_path", resolved_log_file)
            object.__setattr__(ctx, "run_log_path", resolved_log_file)
        _apply_log_level(ctx, args)
        _apply_connection_aliases(ctx, args)
        pipeline_name = getattr(args, "pipeline_name", None)
        if pipeline_name:
            object.__setattr__(ctx, "pipeline_name", pipeline_name)
        _adopt_run_id(ctx, args)
        return ctx

    if not config_path:
        raise SystemExit("--config-path is required.")

    try:
        ctx = build_ctx_from_path(
            Path(config_path).expanduser().resolve(),
            app_name=app_name,
        )
    except (ConfigError, OSError) as exc:
        raise SystemExit(f"FATAL: failed to load config - {exc}") from exc
    _apply_log_level(ctx, args)
    _apply_connection_aliases(ctx, args)
    _adopt_run_id(ctx, args)
    return ctx


def _adopt_run_id(ctx: Any, args: Any) -> None:
    """Adopt the run this process was launched under, when it was given one.

    A launcher that already recorded the run passes its id down, and
    ``app_runtime`` starts no second run for a context that arrives carrying
    one. Without this the launcher's run and the process's run are two
    manifests for one execution: the launcher's never reaches a terminal
    status, and the run log belongs to the other.
    """
    run_id = getattr(args, "run_id", None)
    if run_id:
        object.__setattr__(ctx, "run_id", int(run_id))


def apply_env_overrides(overrides: list[str]) -> None:
    """Write --set KEY=VALUE pairs into os.environ.

    Parameters
    ----------
    overrides : list[str]
        Strings in KEY=VALUE format, typically from the ``--set`` argument.

    Raises
    ------
    SystemExit
        If any item does not contain ``=``.
    """

    for item in overrides:

        if "=" not in item:
            raise SystemExit(
                f"--set requires KEY=VALUE format, got: {item!r}"
            )

        key, _, value = item.partition("=")

        os.environ[key.strip()] = value


def _apply_connection_aliases(ctx: "Namespace", args: "Namespace") -> None:
    """Record this run's connection aliases on the context, and prove them usable.

    Applied in both arrival paths -- a context resolved from a config path and
    one restored from a pipeline step snapshot. A snapshot already carries the
    launcher's aliases, because the coordinator deep-copies the whole context
    into each step; those are the base, and an alias this process was given
    merges over them. A child overriding one alias keeps the rest.

    Validated here rather than at first use: a run that cannot route its
    connections should fail at launch, not part-way through, having already done
    work it cannot record.

    Raises
    ------
    SystemExit
        When an alias is not in CONFIGURED=RUNTIME form.
    ConfigError
        When the resulting map is unusable -- an unknown name on either side, a
        self-alias, or a chain.
    """
    from rey_lib.db.connection import (
        CONNECTION_ALIASES_ATTR,
        validate_connection_aliases,
    )

    asked = getattr(args, "connection_aliases", None) or []
    inherited = getattr(ctx, CONNECTION_ALIASES_ATTR, None)
    aliases: dict[str, str] = dict(inherited.items()) if inherited else {}

    for item in asked:
        if "=" not in item:
            raise SystemExit(
                f"--connection-alias requires CONFIGURED=RUNTIME format, got: {item!r}"
            )
        configured, _, runtime = item.partition("=")
        configured, runtime = configured.strip(), runtime.strip()
        if not configured or not runtime:
            raise SystemExit(
                f"--connection-alias requires a name on both sides, got: {item!r}"
            )
        aliases[configured] = runtime

    if aliases:
        object.__setattr__(ctx, CONNECTION_ALIASES_ATTR, aliases)
        validate_connection_aliases(ctx)


def _apply_log_level(ctx: "Namespace", args: "Namespace") -> None:
    """Let this run's ``--log-level`` override what configuration declared.

    Applied in both arrival paths -- a context resolved from a config path and
    one restored from a pipeline step snapshot -- because a level that stopped
    at the coordinator would mean turning a pipeline up did nothing to the step
    that actually failed.

    Only what was asked for is written. Absent, the configured level stands and
    the default behind it is unchanged.
    """
    asked = getattr(args, "log_level", None)
    if asked:
        object.__setattr__(ctx, "log_level", str(asked))

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
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

if TYPE_CHECKING:
    from rey_lib.config.config_utils import Namespace

__all__ = [
    "preparse_config_args",
    "add_config_args",
    "apply_env_overrides",
    "build_ctx_from_args",
]

def preparse_config_args() -> None:
    """Pre-parse --config-path/--config-dir from sys.argv and call load_dotenv.

    Must be called at module level in each app entry point, before any
    imports that depend on environment variables being set.

    .env resolution priority
    ------------------------
    1. Parent directory of --config-path
    2. --config-dir value
    3. APP_CONFIG_DIR environment variable
    4. load_dotenv default (searches upward from cwd)
    """
    pre = argparse.ArgumentParser(add_help=False)

    pre.add_argument("--config-path", dest="config_path", default=None)
    pre.add_argument("--config-dir",  dest="config_dir",  default=None)

    pre_args, _ = pre.parse_known_args()

    config_dir_str: Optional[str] = (
        str(Path(pre_args.config_path).expanduser().parent)
        if pre_args.config_path
        else pre_args.config_dir
        or os.environ.get("APP_CONFIG_DIR")
    )

    load_dotenv(
        Path(config_dir_str).expanduser() / ".env"
        if config_dir_str
        else None
    )


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

    # ---------------------------------------------------------------------
    # Pipeline coordinator arguments
    # ---------------------------------------------------------------------

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
        "--log-file",
        dest="log_file",
        default=None,
        help="Shared pipeline JSONL log file path.",
    )



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

    if not getattr(args, "config_path", None):
        raise SystemExit("--config-path is required.")
    return build_ctx_from_path(
        Path(args.config_path).expanduser().resolve(),
        app_name=app_name,
    )


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

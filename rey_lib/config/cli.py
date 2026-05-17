"""
Shared CLI argument helpers for Rey app entry points.

Centralizes the pre-parse/load_dotenv pattern and argparse argument
declarations that are otherwise duplicated across every Rey app.

Public API
----------
  preparse_config_args()    Pre-parse --config-path / --config-dir and call load_dotenv.
  add_config_args(parser)   Add shared config/env/set args to an argparse parser.
  apply_env_overrides(items) Write --set KEY=VALUE pairs into os.environ.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

__all__ = [
    "preparse_config_args",
    "add_config_args",
    "apply_env_overrides",
]

_VALID_ENVS: tuple[str, ...] = ("dev", "prod")


def preparse_config_args() -> None:
    """Pre-parse --config-path / --config-dir from sys.argv and call load_dotenv.

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
        str(Path(pre_args.config_path).expanduser().parent) if pre_args.config_path
        else pre_args.config_dir
        or os.environ.get("APP_CONFIG_DIR")
    )
    load_dotenv(Path(config_dir_str).expanduser() / ".env" if config_dir_str else None)


def add_config_args(parser: argparse.ArgumentParser) -> None:
    """Add shared config/env/override arguments to an argparse parser.

    Adds ``--config-path``, ``--config-dir``, ``--env``, and ``--set``.
    App-specific arguments should be added separately after this call.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        Parser to augment in-place.
    """
    parser.add_argument(
        "--config-path",
        dest="config_path",
        default=None,
        help=(
            "Path to the app config file (e.g. config.dev.yaml). "
            "Derives env from filename; supersedes --env and --config-dir."
        ),
    )
    parser.add_argument(
        "--config-dir",
        dest="config_dir",
        default=None,
        help="Path to the config directory (overrides APP_CONFIG_DIR).",
    )
    parser.add_argument(
        "--env",
        required=False,
        default=None,
        choices=list(_VALID_ENVS),
        help="Target environment. Required when --config-path is not provided.",
    )
    parser.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        dest="env_overrides",
        default=[],
        help="Override a .env variable for this run (repeatable): --set KEY=VALUE",
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
            raise SystemExit(f"--set requires KEY=VALUE format, got: {item!r}")
        key, _, value = item.partition("=")
        os.environ[key.strip()] = value

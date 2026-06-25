"""rey_lib.artifacts — command-line entry point.

Exposes the same artifact-processing formatter outside the pipeline so external
tools (DBeaver external formatter, terminal use, future Rey Console) share one
set of rules. The CLI never calls a formatter engine directly and never reads
config YAML directly — it builds the application context from the installation
config and reads the artifact-processing routing config from the ctx, exactly
like the pipeline.

Usage
-----
    rey-format-sql --config-path <installation config.yaml> [--app rey_console]
                   [--artifact-type sql] [--in-place | --output <path>] <file | ->

Reads from a file (or stdin when the path is ``-``) and writes formatted output
to stdout, an ``--output`` path, or back in place with ``--in-place``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rey_lib.artifacts.api import artifact_config_from_ctx, process_artifact
from rey_lib.artifacts.errors import ArtifactProcessingError
from rey_lib.files.file_utils import read_text_file, write_file


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, format the input via the ctx config, and write output.

    Parameters
    ----------
    argv : list[str] | None
        Argument vector (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        Process exit code (0 success, non-zero on error).
    """
    args = _parse_args(argv)

    # Build the application context from the installation config and read the
    # artifact-processing routing config from it (same source as the pipeline).
    from rey_lib.config.config_utils import build_ctx_from_path  # noqa: PLC0415

    ctx = build_ctx_from_path(args.config_path, app_name=args.app)
    config = artifact_config_from_ctx(ctx)
    if not config:
        print(
            "error: no artifact_processing config found in ctx "
            f"(config-path={args.config_path}, app={args.app})",
            file=sys.stderr,
        )
        return 2

    # stdin/stdout are streams (no file); actual files go through the shared
    # file utilities so file handling is consistent across the system.
    if args.file == "-":
        content = sys.stdin.read()
    else:
        content = read_text_file(args.file)

    try:
        result = process_artifact(content, args.artifact_type, config)
    except ArtifactProcessingError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.in_place and args.file != "-":
        write_file(Path(args.file), result, file_type="TEXT", reason="format_artifact")
    elif args.output:
        write_file(Path(args.output), result, file_type="TEXT", reason="format_artifact")
    else:
        sys.stdout.write(result)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and parse the CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="rey-format-sql",
        description="Format a generated artifact through rey_lib.artifacts.",
    )
    parser.add_argument(
        "--config-path", required=True, dest="config_path",
        help="Path to the installation config.yaml (artifact_processing is read "
             "from the resulting ctx).",
    )
    parser.add_argument(
        "--app", default="rey_console",
        help="App whose ctx is built to load shared config (default: rey_console).",
    )
    parser.add_argument(
        "--artifact-type", default="sql", dest="artifact_type",
        help="Artifact type to process (default: sql).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--in-place", "-i", action="store_true", dest="in_place",
        help="Rewrite the input file in place.",
    )
    group.add_argument(
        "--output", "-o", default="",
        help="Write formatted output to this path instead of stdout.",
    )
    parser.add_argument("file", help="Input file path, or '-' for stdin.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())

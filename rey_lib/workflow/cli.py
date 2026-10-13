"""
The workflow runner's standard command-line controls, defined once.

Step selection belongs to the shared engine: ``run_workflow`` takes ``step``,
``from_step`` and ``to_step`` and resolves them against the workflow's ordered
steps (``coordinator._select_steps``), including every refusal. What an
application cannot do on its own is accept them: its parser rejects an unknown
option before the engine ever sees it. So the command interface for those
controls lives here, beside the engine that gives them meaning, and an
application consumes it rather than declaring its own copy.

    Console  --step / --from-step / --to-step
      -> application parser       add_workflow_selection_args(parser)
      -> application registration WORKFLOW_SELECTION_PARAMETERS
      -> engine keyword arguments workflow_selection(args)
      -> run_workflow(..., step=, from_step=, to_step=)

Nothing here validates a selection. Whether a step exists, whether ``step``
combines with a range and whether a range is reversed are the engine's
questions, answered once, in one place.

Public API
----------
  add_workflow_selection_args(parser)  Add --step, --from-step, --to-step.
  WORKFLOW_SELECTION_PARAMETERS        Their registration entries.
  workflow_selection(args)             Parsed arguments -> engine keywords.
"""

from __future__ import annotations

import argparse
from typing import Any, Optional

__all__ = [
    "WORKFLOW_SELECTION_PARAMETERS",
    "add_workflow_selection_args",
    "workflow_selection",
]

#: Each control: its option name, the engine keyword it becomes, and what it
#: does. One table, so the parser, the registration and the mapping cannot
#: drift apart.
_CONTROLS: tuple[tuple[str, str, str], ...] = (
    ("step", "step",
     "Run only one workflow step (with run-workflow)."),
    ("from-step", "from_step",
     "Run from one workflow step through the end (with run-workflow)."),
    ("to-step", "to_step",
     "Run from the beginning through one workflow step (with run-workflow)."),
)

#: The registration entries an application publishes for its run-workflow
#: command, in the format its registration already uses. Spread them into the
#: command's parameters; never copy them.
WORKFLOW_SELECTION_PARAMETERS: tuple[dict[str, Any], ...] = tuple(
    {
        "name": option,
        "required": False,
        "value_type": "string",
        "description": description,
    }
    for option, _keyword, description in _CONTROLS
)


def add_workflow_selection_args(parser: argparse.ArgumentParser) -> None:
    """Add the runner's step-selection options to an application's parser.

    Args:
        parser: The application's argument parser (or its run-workflow
            sub-parser).
    """
    for option, keyword, description in _CONTROLS:
        parser.add_argument(f"--{option}", dest=keyword, default=None, help=description)


def workflow_selection(args: argparse.Namespace) -> dict[str, Optional[str]]:
    """The engine's selection keywords, read from parsed arguments.

    A blank value is no selection, as an absent one is: an empty ``--step ""``
    would otherwise reach the engine as a step named nothing.

    Args:
        args: Arguments parsed by a parser that
            :func:`add_workflow_selection_args` extended.

    Returns:
        ``{"step", "from_step", "to_step"}``, each a step identifier or None,
        ready for ``run_workflow(**selection)``.
    """
    return {
        keyword: (str(getattr(args, keyword, None) or "").strip() or None)
        for _option, keyword, _description in _CONTROLS
    }

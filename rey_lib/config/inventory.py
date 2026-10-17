"""The one way configuration becomes plain data.

One responsibility, and deliberately no second. This module used to also build
an ``InstallationInventory`` -- a whole-installation aggregate of apps,
workflows, pipelines, contracts, connections and their derived relationships --
and resolve the legacy workflow run action (retired with the legacy workflow
system, backlog 1011). A second answer to "what is the runtime model of an
installation?" is not harmless: the canonical answer is the objects on the
context, and an unused aggregate beside them makes obsolete architecture look
supported.

Whatever needs configuration as plain data asks ``to_plain_data``; nothing here
reconstructs an installation.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

from rey_lib.config.config_utils import Namespace

__all__ = [
    "to_plain_data",
]


def to_plain_data(value: Any) -> Any:
    """Return configuration as plain JSON-safe data.

    The one answer to "serialize this config object", for every surface that
    hands configuration to something outside the process -- an API response, a
    Tree payload, a run action. A second normalizer would be a second
    answer, and the two would drift.

    The conversion is structural and nothing more. It reads no environment and
    resolves nothing, so a value naming an environment variable comes out as
    the name it went in as. That is what makes these surfaces safe by
    construction rather than by a redaction rule applied afterwards: there is
    no resolved value in a context to leak.

    Parameters
    ----------
    value : Any
        Namespace, mapping, sequence, Path, an object exposing ``to_dict()``,
        or a scalar.

    Returns
    -------
    Any
        Dicts, lists, strings and scalars only. Sequences become lists so the
        result is JSON-safe; callers wanting immutability freeze it themselves.
    """
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_plain_data(to_dict())
    if isinstance(value, Namespace):
        return {k: to_plain_data(v) for k, v in value.items()}
    if isinstance(value, (dict, MappingProxyType)):
        return {k: to_plain_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain_data(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value

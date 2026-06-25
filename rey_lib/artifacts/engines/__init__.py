"""rey_lib.artifacts.engines — engine registry.

Engines are registered by name so the artifact-processing layer can select one
from configuration (``engine: sqlfluff``) without importing the engine in
application code.
"""

from __future__ import annotations

from typing import Optional

from rey_lib.artifacts.engines.base import ArtifactEngine
from rey_lib.artifacts.engines.sqlfluff_engine import SqlFluffEngine

__all__ = ["ArtifactEngine", "get_engine", "register_engine"]

_ENGINES: dict[str, ArtifactEngine] = {}


def register_engine(engine: ArtifactEngine) -> None:
    """Register an engine adapter under its ``name``.

    Parameters
    ----------
    engine : ArtifactEngine
        The engine adapter instance.
    """
    _ENGINES[engine.name] = engine


def get_engine(name: str) -> Optional[ArtifactEngine]:
    """Return the registered engine for ``name``, or None if unknown.

    Parameters
    ----------
    name : str
        Engine name from config (e.g. ``"sqlfluff"``).

    Returns
    -------
    Optional[ArtifactEngine]
        The engine adapter, or None when not registered.
    """
    return _ENGINES.get(name)


# Built-in engines.
register_engine(SqlFluffEngine())

"""rey_lib.artifacts.engines — engine adapter interface.

Each formatter/linter engine (SQLFluff today, ruff/shfmt/... later) is exposed
through this adapter interface so application and pipeline code never call a
specific engine directly. The Rey artifact-processing layer selects an engine
by name and calls ``process``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ArtifactEngine(Protocol):
    """Adapter interface every artifact engine must implement."""

    name: str

    def process(self, content: str, artifact_type: str, config: dict[str, Any]) -> str:
        """Format/normalise ``content`` for ``artifact_type`` and return it.

        Parameters
        ----------
        content : str
            Clean artifact content (already extracted from the LLM envelope).
        artifact_type : str
            The artifact type (e.g. ``"sql"``).
        config : dict[str, Any]
            The resolved per-artifact-type processing config. Must include the
            engine-native ``config_path`` when the engine needs one.

        Returns
        -------
        str
            The processed (formatted) content.

        Raises
        ------
        Exception
            On a hard engine failure. The post-processor decides whether to
            raise or pass through based on ``fail_on_error``.
        """
        ...

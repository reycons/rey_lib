"""rey_lib.artifacts.engines — SQLFluff engine adapter.

Wraps SQLFluff behind the ArtifactEngine interface. All SQLFluff-specific
details live here; the Rey artifact-processing layer and application code never
import or call SQLFluff directly. Formatting rules are owned by the native
SQLFluff config file referenced by ``config_path`` (Rey passes it explicitly,
so SQLFluff auto-discovery is not relied upon).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from rey_lib.artifacts.errors import ArtifactProcessingError
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

# Narrow gap SQLFluff cannot close: it keeps "... AS SELECT" on one line for a
# CREATE VIEW. Put SELECT on its own line. A bare "AS SELECT" (AS directly
# followed by the SELECT keyword, no opening paren) only occurs in a view
# header — column aliases are "AS <identifier>" and subqueries are "AS (SELECT".
_VIEW_AS_SELECT = re.compile(r"\bAS[ \t]+SELECT\b", re.IGNORECASE)


class SqlFluffEngine:
    """ArtifactEngine adapter that formats SQL with SQLFluff's ``fix``."""

    name = "sqlfluff"

    def process(self, content: str, artifact_type: str, config: dict[str, Any]) -> str:
        """Format SQL content using the SQLFluff native config at config_path.

        Parameters
        ----------
        content : str
            Clean SQL content extracted from the LLM envelope.
        artifact_type : str
            Artifact type (``"sql"``).
        config : dict[str, Any]
            Resolved processing config. Requires ``config_path`` pointing at a
            SQLFluff-native config file.

        Returns
        -------
        str
            Formatted SQL.

        Raises
        ------
        ArtifactProcessingError
            If SQLFluff is not installed, the config_path is missing, or
            formatting raises.
        """
        config_path = str(config.get("config_path") or "")
        if not config_path or not Path(config_path).is_file():
            raise ArtifactProcessingError(
                "Artifact post-processing failed. Artifact type: "
                f"{artifact_type}. Engine: sqlfluff. Reason: SQLFluff config "
                f"file not found at config_path '{config_path}'. No final "
                "output file was written."
            )

        try:
            import sqlfluff  # noqa: PLC0415 — optional dependency, imported lazily
        except ImportError as exc:
            raise ArtifactProcessingError(
                "Artifact post-processing failed. Artifact type: "
                f"{artifact_type}. Engine: sqlfluff. Reason: the 'sqlfluff' "
                "package is not installed (rey_lib[artifacts]). No final output "
                "file was written."
            ) from exc

        try:
            formatted = sqlfluff.fix(content, config_path=config_path)
        except Exception as exc:  # noqa: BLE001 — surfaced as a clear error
            raise ArtifactProcessingError(
                "Artifact post-processing failed. Artifact type: "
                f"{artifact_type}. Engine: sqlfluff. Reason: SQLFluff could not "
                f"format the SQL using the configured dialect/style: {exc}. No "
                "final output file was written."
            ) from exc

        formatted = _VIEW_AS_SELECT.sub("AS\nSELECT", formatted)
        return formatted.strip() + "\n"

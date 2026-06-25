"""rey_lib.artifacts.engines — Rey YAML validation engine.

Lints/validates a standalone (generated) YAML artifact in isolation. Unlike the
SQL engine it does not reformat or rewrite — it parses and checks, then returns
the content unchanged on success or raises a clear failure.

YAML is never read or parsed with ad hoc calls: the artifact content arrives
already read through Rey file utilities, and parsing goes through the
``config_utils`` gateway (no direct ``yaml.*``). No external YAML dependency is
added in this first pass — yamllint/ruamel are deferred.

Checks (first pass):
  - no tabs used for indentation
  - valid YAML (parse via config_utils)
  - Rey header comment block present (when require_header is set)
  - required top-level keys present (when required_keys are provided)
"""

from __future__ import annotations

from typing import Any

from rey_lib.artifacts.errors import ArtifactProcessingError
from rey_lib.config import config_utils
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

# Header marker for Rey config YAML (see the YAML owner-header convention).
_HEADER_MARKER = "# App:"
_HEADER_SCAN_LINES = 12


class ReyYamlValidateEngine:
    """ArtifactEngine adapter that lints/validates an isolated YAML artifact."""

    name = "rey_yaml_validate"

    def process(self, content: str, artifact_type: str, config: dict[str, Any]) -> str:
        """Validate YAML content and return it unchanged, or raise on failure.

        Parameters
        ----------
        content : str
            The YAML artifact text (already read via Rey file utilities).
        artifact_type : str
            Artifact type (``"yaml"``).
        config : dict[str, Any]
            Resolved route config. Optional keys:
            ``require_header`` (bool) and ``required_keys`` (list[str]).

        Returns
        -------
        str
            The original content (lint-only; no rewrite).

        Raises
        ------
        ArtifactProcessingError
            On tab indentation, parse failure, missing header, or missing keys.
        """
        self._check_no_tabs(content, artifact_type)
        data = self._parse(content, artifact_type)

        if bool(config.get("require_header", False)):
            self._check_header(content, artifact_type)

        required_keys = list(config.get("required_keys") or [])
        if required_keys:
            self._check_required_keys(data, required_keys, artifact_type)

        return content

    def _check_no_tabs(self, content: str, artifact_type: str) -> None:
        """Reject tab characters used for indentation (YAML disallows them)."""
        for line_no, line in enumerate(content.splitlines(), start=1):
            indent = line[: len(line) - len(line.lstrip())]
            if "\t" in indent:
                raise ArtifactProcessingError(
                    "YAML validation failed. Artifact type: "
                    f"{artifact_type}. Reason: tab character used for "
                    f"indentation at line {line_no}. YAML must use spaces. "
                    "No final YAML artifact was written."
                )

    def _parse(self, content: str, artifact_type: str) -> Any:
        """Parse YAML through the config_utils gateway; raise a clear error."""
        try:
            return config_utils.parse_yaml(content)
        except Exception as exc:  # noqa: BLE001 — surfaced as a clear error
            raise ArtifactProcessingError(
                "YAML validation failed. Artifact type: "
                f"{artifact_type}. Reason: YAML parser error: {exc}. "
                "No final YAML artifact was written."
            ) from exc

    def _check_header(self, content: str, artifact_type: str) -> None:
        """Require the Rey App/Purpose/Used-by header comment block."""
        head = "\n".join(content.splitlines()[:_HEADER_SCAN_LINES])
        if _HEADER_MARKER not in head:
            raise ArtifactProcessingError(
                "YAML validation failed. Artifact type: "
                f"{artifact_type}. Reason: missing Rey header comment block. "
                "Expected an App / Purpose / Used by header at top of file. "
                "No final YAML artifact was written."
            )

    def _check_required_keys(
        self,
        data:          Any,
        required_keys: list[str],
        artifact_type: str,
    ) -> None:
        """Require the given top-level keys to be present in the document."""
        if not isinstance(data, dict):
            raise ArtifactProcessingError(
                "YAML validation failed. Artifact type: "
                f"{artifact_type}. Reason: expected a top-level mapping with "
                f"keys {required_keys}. No final YAML artifact was written."
            )
        missing = [key for key in required_keys if key not in data]
        if missing:
            raise ArtifactProcessingError(
                "YAML validation failed. Artifact type: "
                f"{artifact_type}. Reason: missing required top-level keys: "
                f"{missing}. No final YAML artifact was written."
            )

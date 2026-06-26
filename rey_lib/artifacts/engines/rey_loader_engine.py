"""rey_lib.artifacts.engines — rey_loader config validation engine.

Validates a generated ``rey_loader`` data-source YAML config (artifact_type
``rey_loader_yaml``). It validates the YAML first (parse via config_utils, no
tabs, Rey header), then applies a lightweight rey_loader field check — minimum
safe validation, since rey_loader does not yet expose a formal config-shape
validator. Lint-only: the content is returned unchanged on success, or a clear
failure is raised so no loader config is written.

YAML is parsed through the config_utils gateway — no direct ``yaml.*`` or file
reads here.
"""

from __future__ import annotations

from typing import Any

from rey_lib.artifacts.errors import ArtifactProcessingError
from rey_lib.config import config_utils
from rey_lib.logs import get_logger

_logger = get_logger(__name__)

_HEADER_MARKER = "# App:"
_HEADER_SCAN_LINES = 12

# Only these top-level sections are expected in a rey_loader data-source config.
_ALLOWED_TOP_LEVEL = frozenset({"data_sources"})


class ReyLoaderValidateEngine:
    """ArtifactEngine adapter validating a generated rey_loader YAML config."""

    name = "rey_loader_validate"

    def process(self, content: str, artifact_type: str, config: dict[str, Any]) -> str:
        """Validate rey_loader YAML and return it unchanged, or raise.

        Parameters
        ----------
        content : str
            The generated rey_loader YAML (already read via Rey file utilities).
        artifact_type : str
            Artifact type (``"rey_loader_yaml"``).
        config : dict[str, Any]
            Route config. Optional: ``require_header`` (bool).

        Returns
        -------
        str
            The original content (lint-only; no rewrite).

        Raises
        ------
        ArtifactProcessingError
            On tab indentation, invalid YAML, missing header, or a missing/
            unsupported rey_loader field. No loader config should be written.
        """
        self._check_no_tabs(content)
        data = self._parse(content)
        if bool(config.get("require_header", False)):
            self._check_header(content)
        self._validate_rey_loader(data)
        return content

    def _check_no_tabs(self, content: str) -> None:
        """Reject tab characters used for indentation."""
        for line_no, line in enumerate(content.splitlines(), start=1):
            indent = line[: len(line) - len(line.lstrip())]
            if "\t" in indent:
                raise ArtifactProcessingError(
                    "Generated rey_loader YAML config failed validation. "
                    f"Reason: tab character used for indentation at line {line_no}; "
                    "YAML must use spaces. No loader config was written."
                )

    def _parse(self, content: str) -> Any:
        """Parse YAML via the config_utils gateway (YAML validation first)."""
        try:
            return config_utils.parse_yaml(content)
        except Exception as exc:  # noqa: BLE001 — surfaced as a clear error
            raise ArtifactProcessingError(
                "Generated rey_loader YAML config failed validation. Reason: the "
                f"generated artifact is not valid YAML: {exc}. No loader config "
                "was written."
            ) from exc

    def _check_header(self, content: str) -> None:
        """Require the Rey App/Purpose/Used-by header comment block."""
        head = "\n".join(content.splitlines()[:_HEADER_SCAN_LINES])
        if _HEADER_MARKER not in head:
            raise ArtifactProcessingError(
                "Generated rey_loader YAML config failed validation. Reason: "
                "missing Rey header comment block (App / Purpose / Used by). "
                "No loader config was written."
            )

    def _validate_rey_loader(self, data: Any) -> None:
        """Lightweight rey_loader field validation (minimum safe checks)."""
        if not isinstance(data, dict):
            raise self._fail("expected a top-level mapping with 'data_sources'")

        unsupported = sorted(set(data) - _ALLOWED_TOP_LEVEL)
        if unsupported:
            raise self._fail(f"unsupported rey_loader option: {unsupported[0]}")

        sources = data.get("data_sources")
        if not isinstance(sources, list) or not sources:
            raise self._fail("missing required rey_loader field: data_sources")

        for source in sources:
            if not isinstance(source, dict):
                raise self._fail("each data_sources entry must be a mapping")
            for field in ("name", "paths", "transforms", "loads"):
                if field not in source:
                    raise self._fail(f"missing required rey_loader field: data_sources[].{field}")

            transforms = source.get("transforms")
            if not isinstance(transforms, list) or not transforms:
                raise self._fail("missing required rey_loader field: data_sources[].transforms")
            for transform in transforms:
                columns = transform.get("columns") if isinstance(transform, dict) else None
                if not isinstance(columns, list) or not columns:
                    raise self._fail("transforms[].columns are required and must be a non-empty list")

            loads = source.get("loads")
            if not isinstance(loads, list) or not loads:
                raise self._fail("missing required rey_loader field: data_sources[].loads")
            for load in loads:
                block = load.get("load") if isinstance(load, dict) else None
                if not isinstance(block, dict):
                    raise self._fail("missing required rey_loader field: loads[].load")
                if "connection" not in block:
                    raise self._fail("missing required rey_loader field: loads[].load.connection")
                if "destination_table" not in block:
                    raise self._fail("missing required rey_loader field: loads[].load.destination_table")

    @staticmethod
    def _fail(reason: str) -> ArtifactProcessingError:
        """Build a clear rey_loader validation failure."""
        return ArtifactProcessingError(
            "Generated rey_loader YAML config failed validation. "
            f"Reason: {reason}. No loader config was written."
        )

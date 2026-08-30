"""Turning what a provider said into the output the caller asked for.

Parsing and validation are execution's, not an application's. A caller that
asked for a shape gets the shape or a failure -- never text it has to parse
itself, and never a value that was not checked.

One behaviour of the old subsystem is deliberately **not** reproduced: it
warned and skipped validation when ``jsonschema`` was absent, so a structured
result could pass unverified while looking verified. Here the absence of the
validator is a refusal, because an unchecked result presented as checked is
worse than a failure.
"""

from __future__ import annotations

import json
from typing import Any

from rey_lib.ai.errors import AIOutputError
from rey_lib.ai.requests import AIOutputKind, ResolvedAIRequest
from rey_lib.ai.results import AIOutput

__all__ = ["OutputParser"]


class OutputParser:
    """Produces the caller's output form, or refuses.

    Args:
        validate_schema: Whether a schema, when one is required, must actually
            be checked. Left true; a runtime that turns it off is stating that
            it accepts unvalidated structure.
    """

    def __init__(self, *, validate_schema: bool = True) -> None:
        self._validate = validate_schema

    def parse(self, request: ResolvedAIRequest, text: str, value: Any = None) -> AIOutput:
        """The output, in the form the request required."""
        if not request.output.is_structured():
            return AIOutput.of_text(text)

        parsed = value if value is not None else self._decode(text)
        schema = request.schema

        if request.output.kind is AIOutputKind.SCHEMA and schema is None:
            raise AIOutputError(
                "A schema output was requested and no schema was supplied."
            )
        if schema is not None and self._validate:
            self._check(parsed, schema)

        return AIOutput.of_value(parsed, text=text)

    @staticmethod
    def _decode(text: str) -> Any:
        """The structured value the provider's text carries.

        Raises:
            AIOutputError: when it carries none. Never retried.
        """
        stripped = (text or "").strip()
        if not stripped:
            raise AIOutputError("A structured output was requested and nothing came back.")
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
        # A model often wraps its answer in prose or a fence. One more attempt,
        # at the outermost object or array, before refusing.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = stripped.find(opener)
            end = stripped.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(stripped[start:end + 1])
                except json.JSONDecodeError:
                    continue
        raise AIOutputError("The output was not the structured value that was asked for.")

    @staticmethod
    def _check(parsed: Any, schema: dict[str, Any]) -> None:
        """Validate against the schema, or refuse to claim it was validated."""
        try:
            import jsonschema  # noqa: PLC0415 -- optional, and only where used
        except ImportError as exc:
            raise AIOutputError(
                "A schema was required and jsonschema is not installed, so the "
                "output cannot be validated. Refusing rather than returning an "
                "unchecked result as a checked one.",
                cause=exc,
            ) from exc

        errors = sorted(
            jsonschema.Draft7Validator(schema).iter_errors(parsed),
            key=lambda error: list(error.path),
        )
        if errors:
            first = errors[0]
            where = "/".join(str(part) for part in first.path) or "(root)"
            raise AIOutputError(
                f"The output does not satisfy its schema at {where}: {first.message}"
                + (f" ({len(errors)} problems in all)" if len(errors) > 1 else "")
            )

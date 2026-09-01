"""Turning validated output into the caller's own canonical payload.

The rule, stated once because everything here follows from it:

    **Normalization removes only the envelope that resolution itself
    introduced.**

Resolution records that envelope on ``ResolvedAIRequest.envelope_key``. This
removes exactly that key and nothing else. It never inspects the caller's schema,
never matches on key names, and never guesses -- so a legitimate payload whose
own shape happens to use ``result``, ``content`` or ``data`` is returned intact.
If resolution introduced no envelope, this removes nothing.

That is what separates it from the defect it exists to prevent. Unwrapping by
recognising familiar-looking keys would silently strip real caller data, and the
failure would look like success.

Three mechanisms, deliberately not one:

    validation      does the output satisfy the contract
    correction      the model-visible turn taken when it does not
    normalization   what the caller finally receives

None implies ownership of the others. This module is the third only.

What the caller receives includes the canonical rendering of the requested
representation, prepared here because this is where the contract that asked for
it is still in hand. A consumer that rendered an AI result itself would be the
second place deciding what the answer looks like, and two consumers rendering
the same Markdown can disagree.
"""

from __future__ import annotations

from typing import Any

from rey_lib.ai.errors import AIOutputError
from rey_lib.ai.requests import ResolvedAIRequest
from rey_lib.ai.results import AIOutput

__all__ = ["OutputNormalizer"]


class OutputNormalizer:
    """Produces the caller-facing payload from validated output.

    It runs where both the resolved contract and the raw successful output are
    still in hand, which is why the direction is ``contract -> normalization ->
    AIResult`` and never ``AIResult -> inspect itself -> unwrap``.
    """

    def normalize(self, request: ResolvedAIRequest, output: AIOutput) -> AIOutput:
        """The output as the caller asked for it.

        Args:
            request: The resolved contract, which alone knows what envelope was
                introduced and what representation was asked for.
            output: Validated output, still carrying whatever envelope
                resolution added.

        Returns:
            The output with that envelope removed and the contract's
            representation attached.

        Raises:
            AIOutputError: when an envelope was introduced and the output does
                not carry it -- a failure to produce what was asked for, not
                something to paper over by returning the wrapper.
        """
        value = self._unwrapped(request, output.value)
        media_type = request.output.media_type or output.media_type
        html = self._rendered(media_type, output.text)

        if (output.value is value and media_type == output.media_type
                and html == output.html):
            return output
        if output.form is AIOutput().form and value is None:
            return AIOutput.of_text(output.text, media_type=media_type, html=html)
        if value is None:
            return AIOutput.of_text(output.text, media_type=media_type, html=html)
        return AIOutput.of_value(
            value, text=output.text, media_type=media_type, html=html,
        )

    @staticmethod
    def _rendered(media_type: str, text: str) -> str:
        """The canonical rendering of one representation, or nothing.

        Only representations with a defined rendering acquire one. Markdown is
        converted through the estate's single shared converter, so tables and
        every other supported construct behave the same here as anywhere else.
        HTML is already its own rendering and is carried unchanged.

        Everything else -- YAML, JSON, SQL, Python, plain text -- keeps its
        source and gains nothing. Inventing a rendering for them would be this
        layer deciding a presentation no contract asked for.
        """
        if not text:
            return ""
        if media_type == "text/markdown":
            from rey_lib.formatting import markdown_to_html

            return markdown_to_html(text)
        if media_type == "text/html":
            return text
        return ""

    @staticmethod
    def _unwrapped(request: ResolvedAIRequest, value: Any) -> Any:
        """The caller's payload, with only a Rey-introduced envelope removed."""
        key = request.envelope_key
        if not key or value is None:
            return value
        if not isinstance(value, dict):
            raise AIOutputError(
                f"The output contract wrapped the value in '{key}', so an object "
                f"was expected back and {type(value).__name__} came instead."
            )
        if key not in value:
            raise AIOutputError(
                f"The output contract wrapped the value in '{key}' and the "
                "output does not carry it."
            )
        return value[key]

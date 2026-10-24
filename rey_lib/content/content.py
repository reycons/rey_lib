"""Content: typed, viewer-ready content, whatever produced it (#1220).

    producer -> Content -> projection -> tree.subject_panel -> viewer

A physical file, a recipe's manufactured text, a profile's clear or redacted
reading: each is presented as Content. Content describes itself -- what it is
called, its text, the language that text is in, the HTML its producer prepared
for it, and why it may not be shown. It does not know which application holds
it, which subject key it is presented under, or which panel shows it: hosting
belongs to its producer, and the panel is tree.subject_panel's answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rey_lib.errors.error_utils import ConfigError
from rey_lib.viewers.language import APPROVED_TEXT_LANGUAGES

__all__ = ["Content"]


@dataclass(frozen=True)
class Content:
    """One piece of viewer-ready content.

    Attributes:
        name: What the content is called.
        text: The content itself; empty when it is refused.
        language: The language the text is in, from the approved text-language
            vocabulary (``rey_lib.viewers.language.APPROVED_TEXT_LANGUAGES``).
        html: The HTML its producer prepared for it, where one did.
        html_trusted: Whether that HTML is Rey's own rendering, drawn in the
            themed page; HTML that is not is kept isolated.
        refusal: Why the text may not be shown, or empty when it may.
        truncated: Whether the text is a bounded part of something larger.

    Raises:
        ConfigError: When it is unnamed, its language is not in the approved
            vocabulary, trusted HTML is declared without HTML, or a refusal
            carries text.
    """

    name: str
    text: str
    language: str
    html: str = ""
    html_trusted: bool = False
    refusal: str = ""
    truncated: bool = False

    def __post_init__(self) -> None:
        """Refuse Content that does not describe itself consistently."""
        if not str(self.name or "").strip():
            raise ConfigError("Content needs a name.")
        if self.language not in APPROVED_TEXT_LANGUAGES:
            raise ConfigError(
                f"Content language {self.language!r} is not an approved text language."
            )
        if self.html_trusted and not self.html:
            raise ConfigError("Content declares trusted HTML but carries none.")
        if self.refusal and self.text:
            raise ConfigError("Refused Content carries no text.")

    def projection(self) -> dict[str, Any]:
        """This Content's projected state, under one ``content`` key.

        What a host hands to ``Projection.for_subject`` as the subject's content;
        nothing here names a host, a subject key or a panel.
        """
        return {
            "content": {
                "name": self.name,
                "text": self.text,
                "language": self.language,
                "html": self.html,
                "html_trusted": self.html_trusted,
                "refusal": self.refusal,
                "truncated": self.truncated,
            },
        }

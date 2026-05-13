"""
Input redaction for LLM orchestration.

Redaction filters are applied to input text before it is sent to any provider,
ensuring sensitive data is masked or removed at the system boundary.  The
runner accepts an optional RedactionFilter via RunRequest and applies it
after document loading but before the provider call.

To use redaction, subclass RedactionFilter and pass an instance via
RunRequest.redaction_filter (field not yet on RunRequest — add when wiring).

Public API
----------
RedactionFilter
    Abstract base class.  Subclass and implement redact().
NoopRedactor
    Passthrough — applies no redaction.  Default when no filter is configured.
PatternRedactor
    Replaces text matching a list of compiled regex patterns with a mask.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

__all__ = ["RedactionFilter", "NoopRedactor", "PatternRedactor"]


class RedactionFilter(ABC):
    """Abstract base class for input redaction filters.

    Implementations must be stateless and idempotent.
    """

    @abstractmethod
    def redact(self, text: str) -> str:
        """Apply redaction to text and return the sanitised version.

        Parameters
        ----------
        text : str
            Raw input text that will be sent to the LLM provider.

        Returns
        -------
        str
            Text with sensitive content masked or removed.
        """


class NoopRedactor(RedactionFilter):
    """Passthrough redactor — applies no redaction.

    Used as the default when no filter is configured so the runner
    code path is the same regardless of whether redaction is active.
    """

    def redact(self, text: str) -> str:
        """Return text unchanged."""
        return text


class PatternRedactor(RedactionFilter):
    """Replace text matching a list of compiled regex patterns with a mask.

    Parameters
    ----------
    patterns : list[re.Pattern[str]]
        Compiled patterns whose matches will be replaced.
    mask : str
        Replacement string.  Defaults to '[REDACTED]'.

    Example
    -------
    ::

        import re
        from rey_lib.llm.redaction import PatternRedactor

        redactor = PatternRedactor(
            patterns=[
                re.compile(r'\\b\\d{3}-\\d{2}-\\d{4}\\b'),   # SSN
                re.compile(r'\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b', re.IGNORECASE),
            ],
            mask='[REDACTED]',
        )
    """

    def __init__(
        self,
        patterns: list[re.Pattern[str]],
        mask:     str = "[REDACTED]",
    ) -> None:
        """Initialise with compiled patterns and a replacement mask."""
        self._patterns = patterns
        self._mask     = mask

    def redact(self, text: str) -> str:
        """Apply all patterns in order and return the masked text."""
        for pattern in self._patterns:
            text = pattern.sub(self._mask, text)
        return text

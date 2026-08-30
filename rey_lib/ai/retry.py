"""When a failed attempt is worth repeating.

Execution policy, not settings. It belongs to the execution owner and is chosen
per request, so nothing an operator selects in an interface can change how a
governed execution retries.

Three things were learned from the old policy and kept, because each was a
decision rather than an accident:

- a total attempt count, and separate ceilings for the failure kinds that
  otherwise consume it silently;
- provider and output-parse failures are transient; nothing else is;
- **a schema violation is never retried.** The old policy refused at
  construction to accept it as retryable, and the reasoning holds: output that
  does not satisfy its shape is a failed execution, not a slow one. Repeating it
  asks the same question and pays twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rey_lib.ai.errors import AIError, AIOutputError, AIProviderError

__all__ = ["AIRetryPolicy", "DEFAULT_RETRY_POLICY", "NO_RETRY"]


@dataclass(frozen=True)
class AIRetryPolicy:
    """How many times, and for what.

    Attributes
    ----------
    max_attempts:
        The first try plus its retries. One means no retry.
    retry_on:
        The failures worth repeating. ``AIOutputError`` is deliberately absent
        by default -- see the module docstring.
    provider_failure_limit:
        A ceiling on provider failures alone, where one kind should not be
        allowed to spend every attempt. ``None`` is no separate ceiling.
    """

    max_attempts: int = 3
    retry_on: tuple[type[AIError], ...] = field(default=(AIProviderError,))
    provider_failure_limit: int | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(
                f"AIRetryPolicy.max_attempts must be >= 1, got {self.max_attempts}."
            )
        if AIOutputError in self.retry_on:
            raise ValueError(
                "AIOutputError must not be retryable. Output that does not "
                "satisfy its shape is a failed execution, not a transient one."
            )
        if self.provider_failure_limit is not None and self.provider_failure_limit < 1:
            raise ValueError(
                "AIRetryPolicy.provider_failure_limit must be >= 1, got "
                f"{self.provider_failure_limit}."
            )

    def retryable(self, failure: BaseException) -> bool:
        """Whether this failure is one worth repeating."""
        return isinstance(failure, self.retry_on)


#: What applies when a request names no policy.
DEFAULT_RETRY_POLICY = AIRetryPolicy()

#: One attempt, for a caller that would rather fail than wait.
NO_RETRY = AIRetryPolicy(max_attempts=1)

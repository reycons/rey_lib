"""What can go wrong, in the estate's own vocabulary.

One hierarchy, so an application never catches a provider's exception. A
provider failure is translated at the adapter boundary and what escapes says
what happened in domain terms.

The categories are the frozen part; they came from the semantic distinctions the
old subsystem already drew across sixteen classes, kept where they earned their
place and dropped where they did not.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AICancelled",
    "AICapabilityError",
    "AIConfigurationError",
    "AIContractError",
    "AIError",
    "AIExecutionError",
    "AIOutputError",
    "AIProviderError",
    "AIRequestError",
    "AISelectionError",
    "AIToolError",
    "AIUnavailableError",
]


class AIError(Exception):
    """Anything the AI subsystem refuses or cannot complete.

    Carries the provider's own message as ``cause`` where one exists, so an
    operator keeps the diagnosis, without an application having to know what
    kind of object produced it.
    """

    def __init__(self, message: str, *, cause: Any = None) -> None:
        super().__init__(message)
        self.cause = cause


class AIConfigurationError(AIError):
    """The configuration this runtime was built from cannot be used."""


class AISelectionError(AIError):
    """A profile or instruction was named that this runtime does not offer."""


class AIRequestError(AIError):
    """The request itself is not answerable as stated."""


class AICapabilityError(AIError):
    """The effective profile cannot do what the request needs.

    Raised before the provider is reached. The old subsystem established that
    order and it is kept: a capability that is missing is refused rather than
    discovered halfway through a call.
    """


class AIContractError(AIError):
    """The instruction or contract could not be resolved or satisfied."""


class AIProviderError(AIError):
    """The provider refused, failed, or could not be reached."""


class AIExecutionError(AIError):
    """Execution failed for a reason that is neither the provider's nor the
    caller's alone -- including retries exhausted."""


class AIToolError(AIError):
    """A tool call could not be carried out."""


class AIOutputError(AIError):
    """The output could not be parsed, or did not satisfy its schema.

    Never retried. An output that violates its schema is a failure of the
    execution, not a transient condition -- the old ``RetryPolicy`` enforced
    that at construction and the reasoning survives.
    """


class AICancelled(AIError):
    """The caller withdrew the work.

    A withdrawal, not a fault. It is an error type so it interrupts, and it is
    named so a caller can tell it apart from one.
    """


class AIUnavailableError(AIError):
    """The capability exists in principle but nothing can serve it now."""

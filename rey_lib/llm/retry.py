"""
Retry policy for LLM provider calls.

RetryPolicy is a frozen dataclass so it can be embedded in RunRequest and
stored in execution records without mutation risk.

The default policy retries on ProviderFailure and ParseFailure but never
on SchemaMismatch — schema failures are execution failures, not transient
errors.  Callers should construct an explicit RetryPolicy rather than relying
on runner-level defaults.

Public API
----------
RetryPolicy
    Frozen dataclass declaring retry behaviour for a single stage.
DEFAULT_RETRY_POLICY
    Baseline policy: 3 attempts, retry on provider and parse failures only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rey_lib.llm.exceptions import ParseFailure, ProviderFailure

__all__ = ["RetryPolicy", "DEFAULT_RETRY_POLICY"]


@dataclass(frozen=True)
class RetryPolicy:
    """Retry behaviour for a single execution stage.

    Attributes
    ----------
    max_attempts : int
        Total attempts allowed (first try + retries).  Must be >= 1.
    retry_on : tuple[type[Exception], ...]
        Exception types that trigger a retry.  Any exception type not in
        this tuple causes immediate failure.  SchemaMismatch must never
        appear here — schema failures are not retryable by design.
    timeout_limit : Optional[int]
        Maximum TimeoutFailure occurrences before the run is promoted to
        failure, regardless of remaining attempts.  None = no separate limit.
    rate_limit_limit : Optional[int]
        Maximum RateLimitFailure occurrences before the run is promoted to
        failure, regardless of remaining attempts.  None = no separate limit.
    """

    max_attempts:     int                          = 3
    retry_on:         tuple[type[Exception], ...] = field(
        # ParseFailure and ProviderFailure are transient; SchemaMismatch is not.
        default=(ProviderFailure, ParseFailure)
    )
    timeout_limit:    Optional[int]               = None
    rate_limit_limit: Optional[int]               = None

    def __post_init__(self) -> None:
        """Validate the policy on construction."""
        if self.max_attempts < 1:
            raise ValueError(
                f"RetryPolicy.max_attempts must be >= 1, got {self.max_attempts}."
            )
        from rey_lib.llm.exceptions import SchemaMismatch  # noqa: PLC0415
        if SchemaMismatch in self.retry_on:
            raise ValueError(
                "SchemaMismatch must not appear in RetryPolicy.retry_on. "
                "Schema failures are execution failures, not transient errors."
            )
        if self.timeout_limit is not None and self.timeout_limit < 1:
            raise ValueError(
                f"RetryPolicy.timeout_limit must be >= 1, got {self.timeout_limit}."
            )
        if self.rate_limit_limit is not None and self.rate_limit_limit < 1:
            raise ValueError(
                f"RetryPolicy.rate_limit_limit must be >= 1, got {self.rate_limit_limit}."
            )


# Sensible baseline used when no explicit policy is provided.
DEFAULT_RETRY_POLICY = RetryPolicy(
    max_attempts = 3,
    retry_on     = (ProviderFailure, ParseFailure),
)

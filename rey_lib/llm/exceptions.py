"""
Typed failure taxonomy for the LLM orchestration framework.

Every failure type is a distinct class so callers can catch only what they
handle and retry policies can be precise about which errors are retryable.

Hierarchy
---------
OrchestratorError
    ProviderFailure
        TimeoutFailure
        RateLimitFailure
    ValidationFailure
    SchemaMismatch
    ParseFailure
    ContractViolation
    ConfigurationFailure
    LockConflict
    ApprovalRejected
    CancellationFailure
    ArtifactFailure
    RedactionFailure
    PermissionFailure
    ToolFailure
"""

from __future__ import annotations

from rey_lib.errors.error_utils import AppError, ConfigError

__all__ = [
    "OrchestratorError",
    "ProviderFailure",
    "ValidationFailure",
    "SchemaMismatch",
    "ParseFailure",
    "TimeoutFailure",
    "RateLimitFailure",
    "ContractViolation",
    "ConfigurationFailure",
    "LockConflict",
    "ApprovalRejected",
    "CancellationFailure",
    "ArtifactFailure",
    "RedactionFailure",
    "PermissionFailure",
    "ToolFailure",
]


class OrchestratorError(AppError):
    """Base class for all orchestrator failures."""


class ProviderFailure(OrchestratorError):
    """Provider API call failed (network, rate-limit, server error, etc.)."""


class ValidationFailure(OrchestratorError):
    """Input or output failed a validation check before or after the provider call."""


class SchemaMismatch(OrchestratorError):
    """Provider response did not conform to the declared output schema."""


class ParseFailure(OrchestratorError):
    """Provider response could not be parsed into the expected structure."""


class TimeoutFailure(ProviderFailure):
    """Provider call or stage execution exceeded the configured timeout."""


class RateLimitFailure(ProviderFailure):
    """Provider returned a rate-limit response (HTTP 429 or equivalent)."""


class ContractViolation(OrchestratorError):
    """Provider output violated an explicit constraint declared in the contract."""


class ConfigurationFailure(OrchestratorError, ConfigError):
    """Orchestrator or provider configuration is missing, invalid, or inconsistent."""


class LockConflict(OrchestratorError):
    """Execution was blocked by an idempotency or concurrency lock."""


class ApprovalRejected(OrchestratorError):
    """A human reviewer explicitly rejected a stage result."""


class CancellationFailure(OrchestratorError):
    """Execution was cancelled before completion."""


class ArtifactFailure(OrchestratorError):
    """An artifact could not be stored, retrieved, or referenced."""


class RedactionFailure(OrchestratorError):
    """Required redaction could not be applied before provider submission."""


class PermissionFailure(OrchestratorError):
    """Caller lacks permission to perform the requested operation."""


class ToolFailure(OrchestratorError):
    """A registered tool raised an error during execution."""

"""Messaging exception hierarchy."""

from __future__ import annotations

from rey_lib.errors.error_utils import AppError

__all__ = [
    "MessagingError",
    "MessageApprovalError",
    "MessageDeliveryError",
    "MessageValidationError",
]


class MessagingError(AppError):
    """Base exception for messaging lifecycle failures."""


class MessageValidationError(MessagingError):
    """Raised when a message request or draft is invalid."""


class MessageApprovalError(MessagingError):
    """Raised when approval policy prevents delivery."""


class MessageDeliveryError(MessagingError):
    """Raised when a delivery provider fails."""

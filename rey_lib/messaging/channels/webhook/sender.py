"""Webhook delivery placeholder."""

from __future__ import annotations

from typing import Any

from rey_lib.messaging.errors import MessageDeliveryError
from rey_lib.messaging.models import DeliveryResult, Message

__all__ = ["send_webhook"]


def send_webhook(ctx: Any, message: Message) -> DeliveryResult:
    """Dry-run webhook messages; real webhook providers are future work."""
    provider = message.provider or "webhook"
    if message.request.dry_run:
        return DeliveryResult(message.message_id, "webhook", provider, "sent", True, "dry-run")
    raise MessageDeliveryError("Webhook delivery provider is not implemented in v1.")

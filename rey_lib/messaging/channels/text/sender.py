"""Text message delivery placeholder."""

from __future__ import annotations

from typing import Any

from rey_lib.messaging.errors import MessageDeliveryError
from rey_lib.messaging.models import DeliveryResult, Message

__all__ = ["send_text"]


def send_text(ctx: Any, message: Message) -> DeliveryResult:
    """Dry-run text messages; real SMS providers are future work."""
    provider = message.provider or "text"
    if message.request.dry_run:
        return DeliveryResult(message.message_id, "text", provider, "sent", True, "dry-run")
    raise MessageDeliveryError("Text delivery provider is not implemented in v1.")

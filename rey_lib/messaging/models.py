"""DB-ready message models for the rey_lib messaging ecosystem."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

__all__ = [
    "Attachment",
    "DeliveryResult",
    "Message",
    "MessageApproval",
    "MessageContent",
    "MessageEvent",
    "MessageRequest",
    "utc_now",
]


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Attachment:
    """File attachment metadata."""

    path: Path
    content_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe attachment data."""
        data = asdict(self)
        data["path"] = str(self.path)
        return data


@dataclass
class MessageRequest:
    """Application-submitted request for a controlled message lifecycle."""

    message_type: str
    audience: str
    channel: str
    data: dict[str, Any] = field(default_factory=dict)
    recipient_group: str = ""
    recipients: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: str = ""
    subject: str = ""
    template: str = ""
    generation_mode: str = "template"
    body: str = ""
    markdown: str = ""
    html: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    approval_required: bool | None = None
    dry_run: bool = False
    provider: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageContent:
    """Generated and rendered message content."""

    subject: str = ""
    body_text: str = ""
    body_markdown: str = ""
    body_html: str = ""


@dataclass
class MessageApproval:
    """Approval state for a message."""

    status: str = "not_required"
    reviewer: str = ""
    reason: str = ""
    approved_at: str = ""


@dataclass
class Message:
    """Complete message state across generation, approval, rendering, and delivery."""

    request: MessageRequest
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "requested"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    content: MessageContent = field(default_factory=MessageContent)
    approval: MessageApproval = field(default_factory=MessageApproval)
    provider: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe message data."""
        request = asdict(self.request)
        request["attachments"] = [item.to_dict() for item in self.request.attachments]
        return {
            "message_id": self.message_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "request": request,
            "content": asdict(self.content),
            "approval": asdict(self.approval),
            "provider": self.provider,
            "error": self.error,
        }


@dataclass
class DeliveryResult:
    """Provider delivery outcome."""

    message_id: str
    channel: str
    provider: str
    status: str
    dry_run: bool
    detail: str = ""


@dataclass
class MessageEvent:
    """Auditable lifecycle event."""

    message_id: str
    event_type: str
    status: str
    timestamp: str = field(default_factory=utc_now)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe event data."""
        return asdict(self)

"""Public messaging lifecycle router."""

from __future__ import annotations

from typing import Any, Callable

from rey_lib.messaging.approvals import approve_message as approve_message_state
from rey_lib.messaging.approvals import mark_approval_required
from rey_lib.messaging.audit import audit_event
from rey_lib.messaging.channels.email import send_email
from rey_lib.messaging.channels.text import send_text
from rey_lib.messaging.channels.webhook import send_webhook
from rey_lib.messaging.config import message_archive_path, message_provider, resolve_recipient_group
from rey_lib.messaging.errors import MessageApprovalError
from rey_lib.messaging.generation import generate_message as generate_message_draft
from rey_lib.messaging.generation import validate_message as validate_generated_message
from rey_lib.messaging.models import DeliveryResult, Message, MessageContent, MessageEvent, MessageRequest, utc_now
from rey_lib.messaging.policy import approval_required as policy_approval_required
from rey_lib.messaging.rendering import render_message as render_generated_message
from rey_lib.messaging.repository import FileMessageRepository

__all__ = [
    "approve_message",
    "create_message",
    "render_message",
    "send_message",
    "validate_message",
]


def create_message(
    ctx: Any,
    *,
    message_type: str,
    audience: str,
    channel: str,
    data: dict[str, Any] | None = None,
    recipient_group: str = "",
    recipients: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: str = "",
    subject: str = "",
    template: str = "",
    generation_mode: str = "template",
    body: str = "",
    markdown: str = "",
    html: str = "",
    attachments: list[Any] | None = None,
    approval_required: bool | None = None,
    dry_run: bool = False,
    provider: str = "",
    metadata: dict[str, Any] | None = None,
    llm_drafter: Callable[[MessageRequest], MessageContent] | None = None,
) -> Message:
    """Create, generate, validate, apply approval policy, and render a message."""
    resolved = resolve_recipient_group(ctx, recipient_group, channel) if recipient_group else {}
    request = MessageRequest(
        message_type=message_type,
        audience=audience,
        channel=channel,
        data=data or {},
        recipient_group=recipient_group,
        recipients=recipients if recipients is not None else resolved.get("recipients", []),
        cc=cc if cc is not None else resolved.get("cc", []),
        bcc=bcc if bcc is not None else resolved.get("bcc", []),
        reply_to=reply_to or resolved.get("reply_to", ""),
        subject=subject,
        template=template,
        generation_mode=generation_mode,
        body=body,
        markdown=markdown,
        html=html,
        attachments=list(attachments or []),
        approval_required=approval_required,
        dry_run=dry_run,
        provider=provider,
        metadata=metadata or {},
    )
    message = generate_message_draft(ctx, request, llm_drafter=llm_drafter)
    message.provider = message_provider(ctx, channel, provider)
    _persist(ctx, message, "message_requested")
    validate_message(ctx, message)
    mark_approval_required(message, policy_approval_required(ctx, request))
    if message.status != "approval_required":
        render_message(ctx, message)
    _persist(ctx, message, "message_created")
    return message


def validate_message(ctx: Any, message: Message) -> Message:
    """Validate a generated message and audit the result."""
    try:
        result = validate_generated_message(ctx, message)
    except Exception as exc:
        message.status = "failed"
        message.error = str(exc)
        _persist(ctx, message, "message_validation_failed", str(exc))
        raise
    _persist(ctx, result, "message_validated")
    return result


def approve_message(ctx: Any, message: Message, reviewer: str = "", reason: str = "") -> Message:
    """Approve a message and render it if it was waiting on approval."""
    approve_message_state(message, reviewer=reviewer, reason=reason)
    render_message(ctx, message)
    _persist(ctx, message, "message_approved")
    return message


def render_message(ctx: Any, message: Message) -> Message:
    """Render a message and audit the result."""
    result = render_generated_message(message)
    result.updated_at = utc_now()
    _persist(ctx, result, "message_rendered")
    return result


def send_message(ctx: Any, message: Message) -> DeliveryResult:
    """Deliver or dry-run a rendered message through its configured channel."""
    if message.approval.status == "required":
        message.status = "failed"
        message.error = "Message requires approval before delivery."
        _persist(ctx, message, "message_delivery_blocked", message.error)
        raise MessageApprovalError(message.error)
    if message.status != "rendered":
        render_message(ctx, message)

    try:
        result = _send_by_channel(ctx, message)
    except Exception as exc:
        message.status = "failed"
        message.error = str(exc)
        _persist(ctx, message, "message_delivery_failed", str(exc))
        raise

    message.status = result.status
    message.updated_at = utc_now()
    _persist(ctx, message, "message_sent")
    return result


def _send_by_channel(ctx: Any, message: Message) -> DeliveryResult:
    """Route delivery by channel."""
    if message.request.channel == "email":
        return send_email(ctx, message)
    if message.request.channel == "text":
        return send_text(ctx, message)
    if message.request.channel == "webhook":
        return send_webhook(ctx, message)
    raise ValueError(f"Unsupported message channel: {message.request.channel}")


def _persist(ctx: Any, message: Message, event_type: str, error: str = "") -> None:
    """Persist and audit a message lifecycle event."""
    audit_event(message, event_type, error)
    archive_path = message_archive_path(ctx)
    if not archive_path:
        return
    repo = FileMessageRepository(archive_path)
    repo.save_event(MessageEvent(message.message_id, event_type, message.status, detail={"error": error}))
    repo.save_message(message)

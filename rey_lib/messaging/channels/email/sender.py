"""SMTP email delivery."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from rey_lib.messaging.channels.email.attachments import attach_files
from rey_lib.messaging.errors import MessageDeliveryError
from rey_lib.messaging.models import DeliveryResult, Message

__all__ = ["send_email"]


def send_email(ctx: Any, message: Message) -> DeliveryResult:
    """Send or dry-run an email message through SMTP."""
    request = message.request
    provider = message.provider or "smtp"
    if request.dry_run:
        return DeliveryResult(message.message_id, "email", provider, "sent", True, "dry-run")

    smtp_cfg = getattr(getattr(getattr(ctx, "messaging", None), "providers", None), "email", None)
    if smtp_cfg is None:
        raise MessageDeliveryError("messaging.providers.email is required for SMTP delivery.")

    email = EmailMessage()
    email["To"] = ", ".join(request.recipients)
    email["Subject"] = message.content.subject
    from_address = str(getattr(smtp_cfg, "from_address", ""))
    if not from_address:
        raise MessageDeliveryError("SMTP from_address is required.")
    email["From"] = from_address
    if request.cc:
        email["Cc"] = ", ".join(request.cc)
    if request.bcc:
        email["Bcc"] = ", ".join(request.bcc)
    if request.reply_to:
        email["Reply-To"] = request.reply_to
    email.set_content(message.content.body_text)
    if message.content.body_html:
        email.add_alternative(message.content.body_html, subtype="html")
    attach_files(email, request.attachments)

    host = str(getattr(smtp_cfg, "host", ""))
    port = int(getattr(smtp_cfg, "port", 25))
    if not host:
        raise MessageDeliveryError("SMTP host is required.")
    with smtplib.SMTP(host, port, timeout=int(getattr(smtp_cfg, "timeout_seconds", 30))) as smtp:
        if getattr(smtp_cfg, "starttls", False):
            smtp.starttls()
        username = getattr(smtp_cfg, "username", "")
        password = getattr(smtp_cfg, "password", "")
        if username:
            smtp.login(str(username), str(password))
        smtp.send_message(email)
    return DeliveryResult(message.message_id, "email", provider, "sent", False)

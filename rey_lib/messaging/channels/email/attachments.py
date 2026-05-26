"""Email attachment helpers."""

from __future__ import annotations

from email.message import EmailMessage
from mimetypes import guess_type
from pathlib import Path

from rey_lib.files.file_utils import read_bytes_file
from rey_lib.messaging.models import Attachment

__all__ = ["attach_files"]


def attach_files(email: EmailMessage, attachments: list[Attachment]) -> None:
    """Attach files to an email message."""
    for attachment in attachments:
        path = Path(attachment.path).expanduser()
        content_type = attachment.content_type or guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = content_type.split("/", 1)
        email.add_attachment(
            read_bytes_file(path),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

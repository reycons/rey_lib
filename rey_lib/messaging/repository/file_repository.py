"""File-backed messaging repository."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from rey_lib.files.file_utils import open_text_file, read_text_file
from rey_lib.messaging.models import Message, MessageEvent

__all__ = ["FileMessageRepository"]


class FileMessageRepository:
    """Append-only JSONL repository for message state and lifecycle events."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def save_message(self, message: Message) -> None:
        """Append a message snapshot."""
        with open_text_file(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "message", **message.to_dict()}, default=str, sort_keys=True) + "\n")

    def save_event(self, event: MessageEvent) -> None:
        """Append a lifecycle event."""
        with open_text_file(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "event", **event.to_dict()}, default=str, sort_keys=True) + "\n")

    def records(self) -> Iterator[dict]:
        """Yield raw JSONL records."""
        if not self.path.exists():
            return
        for line in read_text_file(self.path, encoding="utf-8").splitlines():
            if not line.strip():
                continue
            yield json.loads(line)

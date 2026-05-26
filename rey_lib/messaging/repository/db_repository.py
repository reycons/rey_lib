"""Database repository placeholder for future messaging persistence."""

from __future__ import annotations

from rey_lib.messaging.errors import MessagingError

__all__ = ["DbMessageRepository"]


class DbMessageRepository:
    """Reserved database-backed repository implementation."""

    def __init__(self, *_args, **_kwargs) -> None:
        raise MessagingError("Database-backed messaging repository is not implemented in v1.")

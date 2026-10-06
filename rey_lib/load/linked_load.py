"""One Source and the Transform it feeds, held together (backlog 681).

    Source -> selected Transform -> preview result

**THE PAIR IS THE MODEL'S.** Which Transform a Source feeds, telling that
Transform what the Source now carries, and previewing the records it would
produce are this object's -- never a surface's. A surface holds a reference to
it, calls it, and relays what it answers.

**THE HELD OBJECTS, NEVER COPIES.** Both methods read the live Source and
Transform, so what a preview executes is exactly what the Transform shows.

**NO PREVIEW STATE.** :meth:`LinkedLoad.preview` returns a fresh result and
keeps nothing: a preview is derived output, not configuration.
"""

from __future__ import annotations

from typing import Any

from rey_lib.errors.error_utils import AppError
from rey_lib.load import load_operation
from rey_lib.load.execution import preview_selected, source_columns
from rey_lib.load.source import Source
from rey_lib.load.transform import Transform
from rey_lib.logs import get_logger

__all__ = ["LinkedLoad"]


_logger = get_logger(__name__)


class LinkedLoad:
    """One Source and the Transform its records go through."""

    def __init__(self, source: Source, transform: Transform) -> None:
        """Hold the live Source and Transform; read nothing.

        Args:
            source: The canonical Source.
            transform: The canonical Transform it feeds.
        """
        self.source = source
        self.transform = transform

    def observe_source(self, ctx: Any) -> bool:
        """Tell the Transform which columns the Source now carries.

        Read through the one existing reading, ``source_columns``. A Source that
        cannot be read yet -- incomplete, or not resolvable -- tells the
        Transform nothing.

        Args:
            ctx: The runtime context, to resolve the Source.

        Returns:
            Whether the Transform was told.
        """
        try:
            columns = source_columns(ctx, self.source)
        except (AppError, ValueError, OSError) as exc:
            _logger.debug(
                "linked load: the source could not be read for its columns (%s)",
                type(exc).__name__,
            )
            return False
        self.transform.observe_source_columns(columns)
        return True

    def preview(self, ctx: Any, limit: int) -> load_operation.LoadPreview:
        """The first records the Source carries through the selected Transform.

        The one preview path, ``preview_selected``, on the held objects. A
        Transform that is not complete is refused, never passed through.

        Args:
            ctx: The runtime context, for configured connections.
            limit: How many records at most.

        Returns:
            A fresh result; nothing about it is kept.

        Raises:
            ConfigError: If the Transform is not complete, or either object
                cannot be resolved.
        """
        return preview_selected(ctx, self.source, self.transform, limit=limit)

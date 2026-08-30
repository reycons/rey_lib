"""The current selection, and the one place it is answered.

An immutable value. The root owns it and replaces it; nothing else holds a
second copy, and a consumer that wants to react observes the root rather than
keeping its own.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

__all__ = ["AISettings"]


@dataclass(frozen=True)
class AISettings:
    """Which profile and which instruction are selected by default.

    Selection identity only. No provider client, no callback, no loaded session,
    no editor state -- those belong to whoever owns them, and putting any of
    them here is how a settings value becomes a second runtime.
    """

    profile_id: str = ""
    instruction_id: str = ""

    def with_profile(self, profile_id: str) -> "AISettings":
        """The same settings with a different profile selected."""
        return replace(self, profile_id=str(profile_id or ""))

    def with_instruction(self, instruction_id: str) -> "AISettings":
        """The same settings with a different instruction selected."""
        return replace(self, instruction_id=str(instruction_id or ""))

"""Resolving an instruction to the text and shape an execution needs.

A contract may be carried as text or named by reference. Loading it is this
mechanism's, so the root does not read files and execution does not learn where
a contract lives.

The loader is supplied. Nothing here opens a path on its own, because where
contracts live is configuration's answer and this subsystem is constructed from
resolved inputs rather than discovering them.
"""

from __future__ import annotations

from typing import Callable

from rey_lib.ai.errors import AIContractError
from rey_lib.ai.instructions import AIInstruction, AIInstructionKind

__all__ = ["ContractResolver"]


class ContractResolver:
    """Turns an instruction into the body an execution can send.

    Args:
        loader: How a referenced contract's body is obtained. Absent, an
            instruction that only names a reference is refused rather than
            silently sent without its body.
    """

    def __init__(self, *, loader: Callable[[str], str] | None = None) -> None:
        self._loader = loader
        self._loaded: dict[str, str] = {}

    def body_of(self, instruction: AIInstruction) -> str:
        """The instruction text to send, or "" when there is none.

        Raises:
            AIContractError: when a contract names a reference that cannot be
                resolved.
        """
        if instruction.kind is AIInstructionKind.NONE:
            return ""
        if instruction.text:
            return instruction.text
        if not instruction.reference:
            if instruction.kind is AIInstructionKind.RAW:
                return ""
            raise AIContractError(
                f"AI instruction '{instruction.id}' carries neither text nor a "
                "reference to load one from."
            )
        return self._load(instruction)

    def _load(self, instruction: AIInstruction) -> str:
        """One contract body, read once per runtime."""
        cached = self._loaded.get(instruction.reference)
        if cached is not None:
            return cached
        if self._loader is None:
            raise AIContractError(
                f"AI instruction '{instruction.id}' must be loaded from "
                f"'{instruction.reference}', and this runtime was given no "
                "contract loader."
            )
        try:
            body = str(self._loader(instruction.reference))
        except AIContractError:
            raise
        except Exception as exc:  # noqa: BLE001 -- any loader failure is one refusal
            raise AIContractError(
                f"AI instruction '{instruction.id}' could not be loaded from "
                f"'{instruction.reference}': {exc}",
                cause=exc,
            ) from exc
        self._loaded[instruction.reference] = body
        return body

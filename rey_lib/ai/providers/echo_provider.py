"""A provider that answers from the request itself.

Not a mock in a test file: a real adapter, so the subsystem can be constructed,
exercised and proven without a network, an API key or an optional SDK. The old
subsystem carried one for the same reason and it earned its place.

It implements the whole contract -- capability reporting, streaming, tools,
structured output, cancellation -- so what it proves about the boundary is the
same thing a network adapter would.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from rey_lib.ai.capabilities import AICapability, AICapabilitySet
from rey_lib.ai.errors import AIProviderError
from rey_lib.ai.providers.base import AIProvider, ProviderCall, ProviderReply
from rey_lib.ai.results import AIUsage
from rey_lib.ai.tools import AIToolCall

__all__ = ["EchoProvider"]

#: What this adapter can do. Everything, because it is the one adapter whose
#: limits are its own choice rather than a remote service's.
_CAPABILITY = AICapabilitySet.of(
    AICapability.TEXT,
    AICapability.STRUCTURED_OUTPUT,
    AICapability.STREAMING,
    AICapability.TOOLS,
    AICapability.VISION,
    AICapability.DOCUMENTS,
    AICapability.AUDIO,
)


class EchoProvider(AIProvider):
    """Answers with what it was asked, deterministically.

    Args:
        reply_with: What to answer instead of the request text, when a caller
            wants a fixed answer.
        tool_calls: Tool calls to request rather than answering, so the tool
            protocol can be exercised without a model that decides to use one.
        fail_times: Fail this many calls before succeeding, so retry behaviour
            can be proven without a flaky provider.
        chunk_size: How much text to report at a time while streaming.
    """

    def __init__(
        self,
        *,
        reply_with: str | None = None,
        value: Any = None,
        tool_calls: tuple[AIToolCall, ...] = (),
        fail_times: int = 0,
        chunk_size: int = 8,
    ) -> None:
        self._reply_with = reply_with
        self._value = value
        self._tool_calls = tool_calls
        self._remaining_failures = int(fail_times)
        self._chunk_size = max(1, int(chunk_size))
        self.calls = 0

    @property
    def name(self) -> str:
        return "echo"

    def capability_for(self, model: str) -> AICapabilitySet:  # noqa: ARG002
        """Every capability, whatever the model. It answers for itself."""
        return _CAPABILITY

    def invoke(
        self,
        call: ProviderCall,
        *,
        cancelled: Callable[[], bool] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> ProviderReply:
        """Answer the call, or fail while there are failures left to give."""
        self.calls += 1

        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise AIProviderError(
                f"echo: refusing call {self.calls} on purpose.",
                cause=RuntimeError("echo failure"),
            )

        if self._tool_calls:
            return ProviderReply(
                tool_calls=self._tool_calls,
                model=call.model,
                usage=AIUsage(input_tokens=self._count(call), output_tokens=0),
                raw={"provider": "echo", "tool_calls": len(self._tool_calls)},
            )

        text = self._reply_with if self._reply_with is not None else self._said(call)
        value = self._value

        if call.json_output and value is None:
            # A JSON answer is expected, so answer with one rather than text
            # that happens to look like it.
            value = {"echo": text}
            text = json.dumps(value)

        if on_text is not None:
            for start in range(0, len(text), self._chunk_size):
                if cancelled is not None and cancelled():
                    break
                on_text(text[start:start + self._chunk_size])

        return ProviderReply(
            text=text,
            value=value,
            model=call.model,
            usage=AIUsage(input_tokens=self._count(call), output_tokens=len(text.split())),
            raw={"provider": "echo", "model": call.model},
        )

    @staticmethod
    def _said(call: ProviderCall) -> str:
        """The last thing the caller said, which is what an echo answers with."""
        for message in reversed(call.messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    @staticmethod
    def _count(call: ProviderCall) -> int:
        """A word count, which is as honest as this adapter can be about usage."""
        return sum(len(str(message.get("content") or "").split()) for message in call.messages)

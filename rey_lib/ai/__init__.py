"""The canonical AI capability of the estate.

One ``AI`` per resolved runtime or installation context. Applications consume
it; none of them interprets AI configuration, resolves a provider, or holds a
second answer to what is selected.

    application
        |
       AI                 state and domain owner
        |
    AIExecutor            one execution's lifecycle
        |
    provider adapter      everything provider-specific stops here

`rey_lib.llm` is a separate, older subsystem. This one was built from evidence
about it and does not depend on it; nothing here imports it, and the two are
attached to different callers until a cutover says otherwise.
"""

from rey_lib.ai.ai import AI, AISnapshot
from rey_lib.ai.cancellation import CancellationToken, after, never
from rey_lib.ai.capabilities import AICapability, AICapabilitySet
from rey_lib.ai.content import (
    AIContent,
    AIContentKind,
    AIInput,
    AIMessage,
    AIRole,
    audio,
    document,
    image,
    structured,
    text,
)
from rey_lib.ai.construction import (
    ProviderFactory,
    ai_from_ctx,
    configured_providers_from_ctx,
)
from rey_lib.ai.contracts import ContractResolver
from rey_lib.ai.errors import (
    AICancelled,
    AICapabilityError,
    AIConfigurationError,
    AIContractError,
    AIError,
    AIExecutionError,
    AIOutputError,
    AIProviderError,
    AIRequestError,
    AISelectionError,
    AIToolError,
    AIUnavailableError,
)
from rey_lib.ai.execution import AIExecutor
from rey_lib.ai.instructions import AIInstruction, AIInstructionKind
from rey_lib.ai.output import OutputParser
from rey_lib.ai.profiles import AIProfile
from rey_lib.ai.providers import (
    AIProvider,
    ConfiguredProvider,
    EchoProvider,
    ProviderCall,
    ProviderCapabilities,
    ProviderReply,
)
from rey_lib.ai.registry import AIRegistry
from rey_lib.ai.requests import (
    AIOutputKind,
    AIOutputSpec,
    AIRequest,
    AIRequestOptions,
    ResolvedAIRequest,
)
from rey_lib.ai.results import (
    AIArtifact,
    AIAttempt,
    AIEvidence,
    AIExecutionInfo,
    AIFinishReason,
    AIOutput,
    AIOutputForm,
    AIResult,
    AIUsage,
)
from rey_lib.ai.normalization import OutputNormalizer
from rey_lib.ai.policies import (
    DEFAULT_EXECUTION_POLICY,
    NO_RETRY,
    AIExecutionPolicy,
    ExecutionBudget,
    FallbackPolicy,
    ReplayClassification,
    ReplayFacts,
    ReplaySafety,
    ToolCorrectionPolicy,
    TransportRetryPolicy,
    ValidationCorrectionPolicy,
)
from rey_lib.ai.sessions import AISession
from rey_lib.ai.state import ExecutionState
from rey_lib.ai.settings import AISettings
from rey_lib.ai.streaming import AIEvent, AIEventKind
from rey_lib.ai.tool_loop import CanonicalToolLoop, ToolLoop, ToolRunner
from rey_lib.ai.tools import AITool, AIToolCall, AIToolResult
from rey_lib.ai.turns import TurnExecutor

__all__ = [
    "AI",
    "AIArtifact",
    "AIAttempt",
    "AICancelled",
    "AICapability",
    "AICapabilityError",
    "AICapabilitySet",
    "AIConfigurationError",
    "AIContent",
    "AIContentKind",
    "AIContractError",
    "AIError",
    "AIEvent",
    "AIEventKind",
    "AIEvidence",
    "AIExecutionError",
    "AIExecutionInfo",
    "AIExecutionPolicy",
    "AIExecutor",
    "AIFinishReason",
    "AIInput",
    "AIInstruction",
    "AIInstructionKind",
    "AIMessage",
    "AIOutput",
    "AIOutputError",
    "AIOutputForm",
    "AIOutputKind",
    "AIOutputSpec",
    "AIProfile",
    "AIProvider",
    "AIProviderError",
    "AIRegistry",
    "AIRequest",
    "AIRequestError",
    "AIRequestOptions",
    "AIResult",
    "AIRole",
    "AISelectionError",
    "AISession",
    "AISettings",
    "AISnapshot",
    "AITool",
    "AIToolCall",
    "AIToolError",
    "AIToolResult",
    "AIUnavailableError",
    "AIUsage",
    "CancellationToken",
    "CanonicalToolLoop",
    "ConfiguredProvider",
    "ContractResolver",
    "DEFAULT_EXECUTION_POLICY",
    "EchoProvider",
    "ExecutionBudget",
    "ExecutionState",
    "FallbackPolicy",
    "NO_RETRY",
    "OutputNormalizer",
    "OutputParser",
    "ProviderCall",
    "ProviderCapabilities",
    "ProviderFactory",
    "ProviderReply",
    "ReplayClassification",
    "ReplayFacts",
    "ReplaySafety",
    "ResolvedAIRequest",
    "ToolCorrectionPolicy",
    "ToolLoop",
    "ToolRunner",
    "TransportRetryPolicy",
    "TurnExecutor",
    "ValidationCorrectionPolicy",
    "after",
    "ai_from_ctx",
    "audio",
    "configured_providers_from_ctx",
    "document",
    "image",
    "never",
    "structured",
    "text",
]

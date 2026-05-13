"""
Contract-driven LLM analysis framework.

Primary interface
-----------------
analysis        AnalysisContract, AnalysisResult, Analyzer — start here.
datasource      DataSource ABC + SQL, CSV, Excel, Text implementations.
preparation     Contract-driven data preparation pipeline (filter/sample/redact/render).

Supporting infrastructure
-------------------------
api             Internal models: RunRequest, RunResponse.
artifacts       ArtifactStore ABC + LocalArtifactStore.
cli             CLI: run/status/replay/approve/reject/cancel/test-contract.
contract        Versioned contract loading and hashing.
document_loader Low-level format helpers (CSV, Excel, query results, text).
exceptions      Typed failure taxonomy (ProviderFailure, SchemaMismatch, etc.).
locking         PID-file pipeline lock preventing concurrent execution.
pipeline        Multi-stage workflow with approval gates and hooks (internal).
providers       Provider abstraction layer (BaseProvider, registry, built-ins).
records         ExecutionRecord, ApprovalRecord, JSONL persistence.
redaction       RedactionFilter ABC + NoopRedactor + PatternRedactor.
retry           RetryPolicy dataclass.
runner          Stage executor: run() → RunResponse (internal).
adapters        ctx-aware helpers (ask_with_ctx) — app/library boundary.
llm_utils       Low-level direct_ask for standalone callers.
result          Backward-compat shim; prefer records module for new code.
"""

__all__: list[str] = []

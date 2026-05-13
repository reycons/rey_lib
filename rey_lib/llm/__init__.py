"""
LLM workflow utilities — contract-driven evaluation, structured results,
multi-stage pipelines, and provider abstraction.

Modules
-------
api             Public integration models: RunRequest, RunResponse.
contract        Versioned contract loading and hashing.
document_loader Format data (CSV, Excel, query results, text) for LLM input.
exceptions      Typed failure taxonomy (ProviderFailure, SchemaMismatch, etc.).
providers       Provider abstraction layer (BaseProvider, registry, built-ins).
records         ExecutionRecord, ApprovalRecord, JSONL persistence.
retry           RetryPolicy dataclass.
runner          Stage executor: run(RunRequest) -> RunResponse.
pipeline        Multi-stage ordered workflow with approval gates.
adapters        ctx-aware helpers (ask_with_ctx) — app/library boundary.
llm_utils       Low-level direct_ask for standalone callers.
result          Backward-compat shim; prefer records module for new code.
"""

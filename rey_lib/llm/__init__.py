"""
LLM workflow utilities — contract-driven evaluation, structured results,
multi-stage pipelines, and provider abstraction.

Modules
-------
contract        Versioned contract loading and hashing.
document_loader Format data (CSV, Excel, query results, text) for LLM input.
exceptions      Typed failure taxonomy (ProviderFailure, SchemaMismatch, etc.).
providers       Provider abstraction layer (BaseProvider, registry, built-ins).
result          EvaluationResult envelope, JSONL storage, approve/reject.
runner          Standalone single-stage evaluation (data + contract → result).
pipeline        Multi-stage ordered workflow with approval gates.
llm_utils       LLM dispatch via provider abstraction (ask, direct_ask).
"""

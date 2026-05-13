"""
LLM workflow utilities — contract-driven evaluation, structured results,
multi-stage pipelines.

Modules
-------
contract        Versioned contract loading and hashing.
document_loader Format data (CSV, Excel, query results, text) for LLM input.
result          EvaluationResult envelope, JSONL storage, approve/reject.
runner          Standalone single-stage evaluation (data + contract → result).
pipeline        Multi-stage ordered workflow with approval gates.
llm_utils       Low-level LLM dispatch (Anthropic, OpenAI).
"""

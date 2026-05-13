"""
Backward-compatibility shim.

New code should import from rey_lib.llm.records directly.

ExecutionRecord replaces EvaluationResult.  The old field names are gone;
callers must migrate to the ExecutionRecord field names.
"""

from __future__ import annotations

# Re-export the canonical types under their new names so existing imports
# of this module continue to resolve without error.
from rey_lib.llm.records import (  # noqa: F401
    ApprovalRecord,
    ExecutionRecord,
    approve,
    load_all_records as load_all,
    load_all_approvals,
    load_latest_record as load_latest,
    reject,
    store_approval,
    store_record as store,
)

# Alias for code that still references EvaluationResult.
EvaluationResult = ExecutionRecord

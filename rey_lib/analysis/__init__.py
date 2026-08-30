"""rey_lib.analysis — the contract-driven analysis domain.

A shared library domain, consumed by more than one application and by rey_lib
itself. It owns the analysis object model:

    AnalysisContractSpec   what an analysis is contracted to produce
    DataSource             where its raw data comes from
    PreparedInput          what a contract's rules made of that data
    ExecutionRecord        one immutable record of one stage execution
    package                the canonical provider-neutral analysis package

**It is a consumer of the canonical AI runtime, not a peer of it.** Provider and
model selection, request resolution, tools and continuation, retry, fallback,
replay safety, validation, streaming and canonical results all belong to
``rey_lib.ai``. This domain says what analysis to run; it never says who runs it,
and it holds no provider, runner or retry machinery of its own.

    rey_lib.analysis -> rey_lib.ai        always
    rey_lib.ai -> rey_lib.analysis        never

That direction is what makes this a domain on top of the runtime rather than the
retired ``rey_lib.llm`` under a new name.
"""

from __future__ import annotations

from rey_lib.analysis.analyzer import (
    AnalysisContractSpec,
    AnalysisResult,
    Analyzer,
    load_analysis_contract,
)
from rey_lib.analysis.api import RunRequest, RunResponse
from rey_lib.analysis.contract import Contract, load
from rey_lib.analysis.datasource import (
    CSVDataSource,
    DataSource,
    ExcelDataSource,
    SourceData,
    TextDataSource,
)
from rey_lib.analysis.execution import run as execute
from rey_lib.analysis.package import (
    LlmPackageContract,
    LlmPackageInput,
    build_package,
)
from rey_lib.analysis.preparation import PreparedInput, prepare
from rey_lib.analysis.records import (
    STATUS_FAILED,
    STATUS_PENDING_APPROVAL,
    STATUS_SUCCESS,
    ApprovalRecord,
    ExecutionRecord,
    approve,
    load_latest_record,
    reject,
    store_record,
)

__all__ = [
    "STATUS_FAILED",
    "STATUS_PENDING_APPROVAL",
    "STATUS_SUCCESS",
    "AnalysisContractSpec",
    "AnalysisResult",
    "Analyzer",
    "ApprovalRecord",
    "CSVDataSource",
    "Contract",
    "DataSource",
    "ExcelDataSource",
    "ExecutionRecord",
    "LlmPackageContract",
    "LlmPackageInput",
    "PreparedInput",
    "RunRequest",
    "RunResponse",
    "SourceData",
    "TextDataSource",
    "approve",
    "build_package",
    "execute",
    "load",
    "load_analysis_contract",
    "load_latest_record",
    "prepare",
    "reject",
    "store_record",
]

"""Build and append a configured LLM package to a completed run log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.logs.evidence_projection import _run_log_identity, read_run_log_sections

__all__ = ["create_llm_package"]


def create_llm_package(
    log_path: str | Path,
    analysis_name: str = "log_interpreter",
) -> dict[str, Any]:
    """Append the configured analysis contract and Results Summary as LLM_PACKAGE."""
    # Imports stay local because config/files import the public logs facade.
    from rey_lib.config.config_utils import build_ctx_from_path, parse_yaml
    from rey_lib.files import read_text_file
    from rey_lib.logs.record_enrichment import log_run_record

    path = Path(log_path).expanduser().resolve()
    run = read_run_log_sections(path)
    records = run["records"]

    config_record = next((
        record for record in records
        if str(record.get("record_type") or "").upper() == "CONFIG_FILE_REFERENCE"
        and record.get("load_order") == 0
        and str(record.get("configuration_layer") or record.get("config_type") or "").lower()
        == "installation"
    ), None)
    if config_record is None:
        raise ValueError(
            "Execution log has no load-order-zero installation CONFIG_FILE_REFERENCE"
        )

    ctx = build_ctx_from_path(Path(config_record["path"]), full_installation=True)
    analyses = getattr(ctx, "log_analysis", None)
    analysis = analyses.get(analysis_name) if analyses is not None else None
    if analysis is None:
        raise ValueError(f"log_analysis configuration not found: {analysis_name}")

    contract_path = Path(str(analysis.contract))
    if not contract_path.is_file():
        raise FileNotFoundError(
            f"Configured log_analysis contract not found: {contract_path}"
        )
    instructions = parse_yaml(read_text_file(contract_path))

    summary = next((
        record for record in reversed(records)
        if str(record.get("record_type") or "").upper() == "RESULTS_SUMMARY"
    ), None)
    if summary is None:
        raise ValueError("Execution log does not contain a canonical RESULTS_SUMMARY record")

    package = {"instructions": instructions, "results": summary}
    if any(
        str(record.get("record_type") or "").upper() == "LLM_PACKAGE"
        and record.get("instructions") == instructions
        and record.get("results") == summary
        for record in records
    ):
        return package

    identity = _run_log_identity(path, records, run["sections"])
    ctx.run_log_path = str(path)
    ctx.run_id = identity["run_id"]
    ctx.run_timestamp = identity["run_timestamp"]
    if identity["app"]:
        ctx.owner_app_name = identity["app"]
    if identity["pipeline"]:
        ctx.pipeline_name = identity["pipeline"]
    if identity["workflow"]:
        ctx.workflow_name = identity["workflow"]

    log_run_record(ctx, "LLM_PACKAGE", record_group="results", **package)
    return package

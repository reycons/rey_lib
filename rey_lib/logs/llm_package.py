"""Build and append a configured LLM package to a completed run log, and run the
configured log analysis over that package."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.logs.evidence_projection import _run_log_identity, read_run_log_sections

__all__ = ["create_llm_package", "run_configured_log_analysis"]


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


def run_configured_log_analysis(
    log_path: str | Path,
    analysis_name: str = "log_interpreter",
) -> dict[str, Any]:
    """Run the configured LLM log analysis over the existing LLM_PACKAGE record.

    Sends the complete LLM_PACKAGE unchanged to the configured LLM through
    ``direct_ask``, extracts and validates the configured JSON artifact from the
    standard rey_lib envelope, and writes the parsed structured result through the
    configured writer. The embedded contract is never reloaded — only existing
    rey_lib functions are composed.
    """
    import json

    from rey_lib.config.config_utils import build_ctx_from_path
    from rey_lib.config.ctx import find_in_ctx
    from rey_lib.files import write_file
    from rey_lib.llm.envelope import build_envelope_instruction, extract_artifact_envelope
    from rey_lib.llm.exceptions import ConfigurationFailure, ParseFailure, ProviderFailure
    from rey_lib.llm.llm_utils import direct_ask
    from rey_lib.logs.record_enrichment import log_run_record

    result: dict[str, Any] = {"result": None, "action": None, "skipped": [], "failures": []}

    path = Path(log_path).expanduser().resolve()
    run = read_run_log_sections(path)
    records = run["records"]

    # Newest existing LLM_PACKAGE — the complete, self-contained LLM input.
    package = next((
        record for record in reversed(records)
        if str(record.get("record_type") or "").upper() == "LLM_PACKAGE"
    ), None)
    if package is None:
        raise ValueError("Execution log does not contain an LLM_PACKAGE record")

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

    if not getattr(analysis, "enabled", False):
        result["skipped"].append("disabled")
        return result

    output = analysis.output
    record_type = str(output.record_type)
    destination = str(getattr(output, "destination", "stdout")).lower()

    # Idempotency: a prior configured result must not be duplicated on re-run.
    if destination == "file":
        if Path(str(output.path)).expanduser().exists():
            result["action"] = "existing"
            return result
    elif any(
        str(record.get("record_type") or "").upper() == record_type.upper()
        for record in records
    ):
        result["action"] = "existing"
        return result

    artifact_type = str(getattr(analysis, "artifact_type", ""))
    try:
        profile = find_in_ctx(ctx, "llm_profiles", str(analysis.llm_execution_profile))
        if profile is None:
            raise ConfigurationFailure(
                f"llm_execution_profile not found: {analysis.llm_execution_profile}"
            )
        raw = direct_ask(
            json.dumps(package) + build_envelope_instruction(artifact_type),
            model=profile.model,
            provider=profile.provider,
            api_key=getattr(profile, "api_key", ""),
        )
        content, _notes = extract_artifact_envelope(raw, artifact_type)
        parsed_result = json.loads(content)
    except (ProviderFailure, ParseFailure, ConfigurationFailure, json.JSONDecodeError) as exc:
        if getattr(analysis, "fail_on_error", False):
            raise
        result["failures"].append(str(exc))
        return result

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

    if destination == "file":
        write_file(Path(str(output.path)), parsed_result, file_type=str(output.format))
        result["action"] = "written_file"
    else:
        log_run_record(
            ctx, record_type, record_group=str(output.record_group), **parsed_result
        )
        result["action"] = "written_stdout"

    result["result"] = parsed_result
    return result

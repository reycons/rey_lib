"""Build and append a configured LLM package to a completed run log.

The log interpretation that once ran over the package was removed with the
legacy AI (#1025); running it on the live AI is #1284.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.logs.evidence_projection import read_run_log_sections
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "create_llm_package",
    "load_contract_references",
]


#: The finalization stage's own name, carried on its records so a summary can
#: attribute them. It is the stage's, not configuration's: there is no
#: log_analysis entry to look it up in, and the stage is not selectable.
LOG_INTERPRETER_ANALYSIS = "log_interpreter"


def create_llm_package(
    run_log: Any,
    *,
    source_record_type: str,
    package_record_type: str,
) -> dict[str, Any]:
    """Append the newest source record as the log interpreter's package.

    The situation the interpreter reads, and nothing else. It carries no
    instructions: what governs the analysis is the instruction the
    ``log_interpretation`` task resolves through AI settings, so embedding a
    contract here would send it twice and let this path disagree with the
    setting a reader can see.

    It resolves no configuration either. This stage was once selected by a
    ``log_analysis`` entry that bound it to a contract and a profile -- both of
    which the AI object already owns -- so the entry was removed along with the
    context rebuild that existed to find it.

    Appending is idempotent: a package already describing this source is not
    written twice.
    """
    # No JSONL file (logging.run_store writes none) reads exactly as an empty log.
    json_path = run_log.path()
    records = (
        read_run_log_sections(Path(json_path).expanduser().resolve())["records"]
        if json_path is not None else []
    )

    source_record = next((
        record for record in reversed(records)
        if str(record.get("record_type") or "").upper() == source_record_type.upper()
    ), None)
    if source_record is None:
        from rey_lib.data.errors import DataStructureError  # noqa: PLC0415 -- import cycle
        raise DataStructureError(
            f"Execution log does not contain source record: {source_record_type}"
        )

    package = {
        "analysis_name": LOG_INTERPRETER_ANALYSIS,
        "source_record_type": source_record_type,
        "source": source_record,
    }
    if any(
        str(record.get("record_type") or "").upper() == package_record_type.upper()
        and record.get("analysis_name") == LOG_INTERPRETER_ANALYSIS
        and record.get("source_record_type") == source_record_type
        and record.get("source") == source_record
        for record in records
    ):
        return package

    run_log.append(package_record_type, record_group="results", **package)
    return package


def _resolve_reference_path(ctx: Any, raw_path: str) -> Path:
    """Resolve a contract-declared reference path through the existing resolver.

    Reuses ctx.paths (the installation PathResolver): each ``{name}`` token is
    replaced with its resolved path. No new resolver or path system is created.
    """
    import re

    resolver = getattr(ctx, "paths", None)

    def _sub(match: "re.Match[str]") -> str:
        if resolver is not None:
            try:
                return str(resolver.resolve(match.group(1)))
            except Exception:
                return match.group(0)
        return match.group(0)

    return Path(re.sub(r"\{([^}]+)\}", _sub, str(raw_path))).expanduser()


def load_contract_references(ctx: Any, declared: Any) -> list[dict[str, Any]] | None:
    """Resolve and load the reference documents a contract declares.

    ``declared`` is a contract's ``references`` list (or None/empty). Reuses the
    existing path-token resolver (ctx.paths) and the approved text loader
    (rey_lib.files.read_text_file), so every LLM package-construction path attaches
    the same reference contents. Returns None when nothing is declared. A required
    reference that cannot be resolved or loaded raises before the LLM request; a
    non-required one is omitted with a warning.
    """
    from rey_lib.files import read_text_file
    from rey_lib.logs.app_logger import AppLogger

    if not declared:
        return None

    loaded: list[dict[str, Any]] = []
    for ref in declared:
        if not isinstance(ref, dict):
            continue
        name = str(ref.get("name") or "")
        role = str(ref.get("role") or "")
        raw_path = str(ref.get("path") or "")
        required = bool(ref.get("required", True))
        try:
            content = read_text_file(_resolve_reference_path(ctx, raw_path))
        except Exception as exc:
            if required:
                raise ConfigError(
                    f"Required contract reference '{name}' could not be loaded "
                    f"from '{raw_path}': {exc}"
                ) from exc
            AppLogger(__name__).warning(
                "Skipping optional contract reference '%s' (%s): %s", name, raw_path, exc
            )
            continue
        entry: dict[str, Any] = {"name": name}
        if role:
            entry["role"] = role
        entry["content"] = content
        loaded.append(entry)
    return loaded


def _build_analysis_package(
    ctx: Any,
    analysis_name: str,
    source_record_type: str,
    instructions: Any,
    source: dict[str, Any],
) -> dict[str, Any]:
    """Return the log-analysis LLM_PACKAGE: the situation an analysis reads.

    This is the legacy provider wire package for the log-analysis path, not the
    canonical LLM package (rey_lib/analysis/package.py). The same object is
    written as the durable LLM_PACKAGE record and serialized as the provider
    prompt. The canonical package is adopted separately in paths whose fields
    exist without reconstruction (rey_analyzer).

    **The contract is no longer embedded here.** It arrives as the instruction
    the task resolves, through the canonical AI settings object, so that every
    task answers the same way about what governs it. Carrying it here as well
    would send it twice and let this path disagree with the setting a reader can
    see.

    That supersedes SGC_Rey_Lib_Canonical_LLM_Package_And_Contract_Evidence
    reconciliation (c), which preserved this object unchanged as a legacy wire
    package. The invariant that replaces it: no consumer supplies or suppresses
    AI settings independently. Safe for the contracts themselves -- they describe
    their input by kind, never by the field it arrives in.

    The contract is still *parsed* to resolve the references it declares. What
    changed is where it is sent, not who reads it.
    """
    package: dict[str, Any] = {
        "analysis_name": analysis_name,
        "source_record_type": source_record_type,
    }
    declared = instructions.get("references") if isinstance(instructions, dict) else None
    references = load_contract_references(ctx, declared)
    if references:
        package["references"] = references
    package["source"] = source
    return package

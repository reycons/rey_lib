"""Build and append a configured LLM package to a completed run log, and run the
configured log analysis over that package."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from rey_lib.logs.evidence_projection import read_run_log_sections
from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "create_llm_package",
    "load_contract_references",
    "run_configured_log_analysis",
    "run_uncontracted_record_analysis",
]


#: The interpreter's output shape. Once configured per installation, it is the
#: stage's own: what it writes is what it is, not a choice an operator makes.
_INTERPRETATION_RECORD_TYPE = "LLM_INTERPRETATION"
_FAILURE_RECORD_TYPE = "LLM_ANALYSIS_FAILURE"
_RECORD_GROUP = "results"
_ARTIFACT_TYPE = "json"

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


def _execute_analysis_package(
    ctx: Any,
    ai: Any,
    execution_profile: str,
    artifact_type: str,
    package: dict[str, Any],
    max_input_characters: int = 0,
    payload_id: str | None = None,
    task: str = "",
) -> Any:
    """Send one package to an execution profile and return the parsed artifact.

    The single execution path for both configured analyses and No Contract runs:
    profile resolution, the envelope instruction, the provider call, envelope
    extraction, and parsing. Callers own record selection, failure recording, and
    output writing.

    Parameters
    ----------
    ctx : Any
        Resolved configuration, carrying ``llm_profiles``. Configuration only --
        the AI is handed in separately, because a context that resolves an
        installation's settings is not the same thing as that installation's
        running AI.
    ai : Any
        The runtime's one AI, built by bootstrap for the installation currently
        executing. ``None`` when this runtime configures none, which the caller's
        failure boundary records.
    execution_profile : str
        Name of the ``llm_profiles`` entry to run the package against. For a No
        Contract run this is the Workbench-selected profile. Empty when ``task``
        is given, because the runtime's settings then own the choice.
    task : str
        What the AI is being asked to do. Given instead of a profile, the
        runtime's task-aware settings decide which one answers, so this caller
        stops carrying execution policy it never owned.
    artifact_type : str
        Artifact envelope type. ``analysis.artifact_type`` for a configured
        analysis, ``""`` for a No Contract run.
    package : dict[str, Any]
        The complete, self-contained LLM input.
    max_input_characters : int
        Optional prompt size limit. ``0`` disables the check.
    structured : bool
        Whether the caller needs a parsed JSON payload rather than text. Asking
        tells the provider to answer in JSON and puts the runtime's output
        reader in the path, which is what normalizes a fenced or prose-wrapped
        answer. A caller that says nothing still receives text.
    """
    import json

    from rey_lib.config.ctx import find_in_ctx
    from rey_lib.config.env_reference import resolve_env_reference
    from rey_lib.artifacts import (
        build_envelope_instruction, extract_artifact_envelope, loads_llm_json,
    )
    from rey_lib.ai.errors import AIConfigurationError

    prompt = json.dumps(package) + build_envelope_instruction(artifact_type)
    if max_input_characters and len(prompt) > max_input_characters:
        from rey_lib.ai.errors import AIRequestError  # noqa: PLC0415 -- import cycle
        raise AIRequestError(
            f"Analysis input is {len(prompt)} characters, "
            f"over the configured limit of {max_input_characters}"
        )

    # A named profile is still checked here, because a caller that names one is
    # asserting it exists. A task names none: the settings answer, and the AI
    # refuses a selection it does not offer, so repeating the check would be a
    # second authority on the same question.
    if not task:
        profile = find_in_ctx(ctx, "llm_profiles", execution_profile)
        if profile is None:
            raise AIConfigurationError(
                f"llm_execution_profile not found: {execution_profile}"
            )
    raw = _ask(ai, prompt, execution_profile, payload_id=payload_id, task=task)
    content, _ = extract_artifact_envelope(raw, artifact_type)
    # The shared policy, not the standard library's: loads_llm_json accepts
    # literal control characters inside strings, protects invalid Markdown
    # escapes and completes a truncated document. Parsing extracted artifact
    # content with json.loads bypassed all three and failed on a control
    # character the shared loader is written to accept.
    return loads_llm_json(content)


def run_uncontracted_record_analysis(
    ctx: Any,
    record: dict[str, Any],
    execution_profile: str = "",
    max_input_characters: int = 0,
    task: str = "",
    structured: bool = False,
) -> dict[str, Any]:
    """Run one already-complete package through the LLM with NO contract added.

    No contract
    is resolved or inserted and no package is assembled: the supplied ``record`` is
    itself the complete package, serialized and sent raw through ``direct_ask``
    (no envelope instruction appended, raw response returned) using the
    Workbench-selected ``execution_profile``. Only existing rey_lib functions are
    composed; nothing is written to any log or file.

    Parameters
    ----------
    ctx : Any
        A resolved context carrying ``llm_profiles``.
    record : dict[str, Any]
        The already-complete package, passed through exactly as supplied.
    execution_profile : str
        Name of the ``llm_profiles`` entry to run against.
    max_input_characters : int
        Optional serialized-package size limit. ``0`` disables the check.

    Returns
    -------
    dict[str, Any]
        ``{"result": raw_response_text_or_None, "action": ..., "skipped": [...]}``.
    """
    import json

    from rey_lib.config.ctx import find_in_ctx
    from rey_lib.config.env_reference import resolve_env_reference
    from rey_lib.ai.errors import AIConfigurationError

    result: dict[str, Any] = {"result": None, "action": None, "skipped": []}

    if not isinstance(record, dict):
        from rey_lib.data.errors import DataStructureError  # noqa: PLC0415 -- import cycle
        raise DataStructureError("Record analysis requires a JSON object record")
    # A caller naming a profile is asserting it exists. A caller naming a task
    # names none: its settings answer, and the AI refuses a selection it does
    # not offer, so checking here would be a second authority on one question.
    if not task:
        profile = find_in_ctx(ctx, "llm_profiles", execution_profile)
        if profile is None:
            raise AIConfigurationError(
                f"llm_execution_profile not found: {execution_profile}"
            )

    # Raw send: the supplied package is serialized and sent exactly as-is. No
    # envelope instruction is appended and the raw response is returned unparsed
    # (direct_ask with no output_format sends the prompt exactly as supplied).
    prompt = json.dumps(record)
    if max_input_characters and len(prompt) > max_input_characters:
        from rey_lib.ai.errors import AIRequestError  # noqa: PLC0415 -- import cycle
        raise AIRequestError(
            f"Analysis input is {len(prompt)} characters, "
            f"over the configured limit of {max_input_characters}"
        )

    result["result"] = _ask(
        # A ctx-taking entry point, so the runtime's AI is read once here at the
        # boundary rather than looked up inside the execution path.
        getattr(ctx, "shared_ai", None), prompt, execution_profile,
        payload_id=str(record["payload_id"]) if record.get("payload_id") else None,
        task=task,
        structured=structured,
    )
    result["action"] = "analysed"
    return result


def run_configured_log_analysis(
    run_log: Any,
    *,
    ai: Any,
    package_record_type: str,
    task: str,
) -> dict[str, Any]:
    """Interpret the run's package record and append the interpretation.

    Sends the complete ``package_record_type`` record unchanged to the runtime's
    AI, extracts and validates the artifact from the standard rey_lib envelope,
    and appends the parsed result as LLM_INTERPRETATION.

    ``task`` is the only selection this stage makes -- what it is asking the AI
    to do. The AI resolves request override -> task override -> default from its
    own settings, so an operator changing that task's settings changes what this
    stage runs on, and no profile or instruction is named here.

    ``ai`` is the runtime's one AI, belonging to the installation executing the
    run, handed in rather than discovered. ``None`` means this runtime configures
    no AI, which the failure boundary records.

    The stage is unconditional and owns its own output shape. It was once a
    selectable ``log_analysis`` entry carrying a contract, a profile, an output
    record type and an enable switch; the contract and profile duplicated what
    AI settings own, the output shape is the stage's own, and the switch was
    retired rather than replaced. Nothing here resolves configuration.
    """
    import json

    from rey_lib.errors.error_utils import (
        build_error_record_payload,
        build_safe_error_payload,
    )
    from rey_lib.ai.errors import (
        AIConfigurationError, AIProviderError, AIUnavailableError,
    )
    from rey_lib.artifacts import ArtifactEnvelopeError

    result: dict[str, Any] = {"result": None, "action": None, "skipped": [], "failures": []}

    # No JSONL file (logging.run_store writes none) reads exactly as an empty log.
    json_path = run_log.path()
    records = (
        read_run_log_sections(Path(json_path).expanduser().resolve())["records"]
        if json_path is not None else []
    )

    # Newest existing package record -- the complete, self-contained LLM input.
    package = next((
        record for record in reversed(records)
        if str(record.get("record_type") or "").upper() == package_record_type.upper()
    ), None)
    if package is None:
        from rey_lib.data.errors import DataStructureError  # noqa: PLC0415 -- import cycle
        raise DataStructureError(
            f"Execution log does not contain package record: {package_record_type}"
        )

    # Idempotency. The stage has already run for this log if it produced either
    # outcome, so a second finalization appends nothing -- a repeated failure is
    # as duplicative as a repeated interpretation, and finalizing twice is a
    # thing that happens.
    if any(
        str(record.get("record_type") or "").upper()
        in (_INTERPRETATION_RECORD_TYPE, _FAILURE_RECORD_TYPE)
        for record in records
    ):
        result["action"] = "existing"
        return result

    try:
        parsed_result = _execute_analysis_package(
            # No ctx. Nothing on this path resolves configuration any more, and
            # the AI belongs to the runtime executing the run.
            None,
            ai,
            # No profile. This stage names a task and the AI answers with the
            # rest; naming one here would be a second answer to that question.
            "",
            _ARTIFACT_TYPE,
            package,
            task=task,
        )
    except (
        AIProviderError, ArtifactEnvelopeError, AIConfigurationError,
        # A runtime with no AI is one more way this stage cannot run, not a
        # reason to fail a run that already finished. It is a sibling of
        # AIConfigurationError rather than a subclass, so it once matched
        # nothing here and skipped the record below.
        AIUnavailableError, json.JSONDecodeError,
        AttributeError, KeyError, TypeError,
    ) as exc:
        # Canonical failure record. An exception is its own kind of record, so it
        # is never written under the type a successful interpretation uses.
        run_log.append(
            _FAILURE_RECORD_TYPE, record_group=_RECORD_GROUP,
            analysis_name=LOG_INTERPRETER_ANALYSIS,
            # Appended directly rather than through log_error, so the record is
            # built here -- once.
            **build_error_record_payload(**build_safe_error_payload(exc)),
        )
        result["failures"].append(str(exc))
        result["action"] = "failed"
        # Finalization runs after the run it describes has finished. A stage that
        # could not interpret the log must never be the thing that fails it.
        return result

    run_log.append(
        _INTERPRETATION_RECORD_TYPE, record_group=_RECORD_GROUP, **parsed_result,
    )
    result["action"] = "written_stdout"
    result["result"] = parsed_result
    return result


def _ask(
    ai: Any, prompt: str, execution_profile: str, *,
    payload_id: Any = None, task: str = "", structured: bool = False,
) -> Any:
    """Send one prompt through the runtime's AI and answer with its text.

    The AI is handed in, never discovered. It is the runtime's one AI, built by
    bootstrap, and it owns which provider and model answer, how a credential is
    resolved, how a failed call is retried and whether a replay is legal. This
    reads no provider configuration and resolves no credential.

    ``None`` is an explicit value meaning this runtime configures no AI. It is an
    ordinary capability state, and the caller's failure boundary records it.

    Evaluation evidence is recorded by ``rey_lib.logs`` through the run's
    ``RunLog``, which is why no log path appears here.
    """
    from rey_lib.ai import AIRequest  # noqa: PLC0415
    from rey_lib.ai.errors import AIUnavailableError  # noqa: PLC0415

    if ai is None:
        raise AIUnavailableError(
            "This runtime has no AI configured, so an analysis cannot run."
        )
    from rey_lib.ai.requests import AIOutputSpec  # noqa: PLC0415

    # A caller asking for JSON says so, and the runtime does two things with it:
    # the provider is told to answer in JSON, and OutputParser reads what comes
    # back -- including a model that wrapped its answer in prose or a fence,
    # which it already retries at the outermost object or array. Asking here is
    # what puts that boundary in the path; a caller that says nothing still gets
    # text, which is what the log-interpretation path returns.
    result = ai.execute(
        AIRequest.prompt(
            str(prompt),
            task=str(task or ""),
            profile_id=str(execution_profile or ""),
            **({"output": AIOutputSpec.json()} if structured else {}),
        ),
    )
    return result.value if structured else result.text

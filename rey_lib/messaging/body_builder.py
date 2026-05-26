"""Pluggable body builder dispatch for messaging."""

from __future__ import annotations

from typing import Any

__all__ = ["build_body", "SUPPORTED_BUILDER_TYPES"]

SUPPORTED_BUILDER_TYPES: frozenset[str] = frozenset(
    {"template", "llm_log_summary", "markdown_file", "static_text"}
)


def build_body(
    body_builder_config: Any,
    *,
    records: list[dict[str, Any]] | None = None,
    raw_text: str = "",
    data: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Dispatch to the correct body builder and return (template_data, body_markdown).

    Parameters
    ----------
    body_builder_config : Any
        Body builder config entry from a message definition.
        Must have a ``type`` field.
    records : list[dict] | None
        Parsed JSONL records, required for ``llm_log_summary``.
    raw_text : str
        Raw file content, used for ``markdown_file`` and ``static_text``.
    data : dict | None
        Caller-supplied context data merged into template substitution.

    Returns
    -------
    tuple[dict[str, Any], str]
        ``(template_data, body_markdown)`` where:
        - ``template_data`` is merged into ``request.data`` for ``$variable`` substitution.
        - ``body_markdown`` overrides the template body when non-empty.
    """
    builder_type = str(getattr(body_builder_config, "type", "") or "template").lower()
    if builder_type not in SUPPORTED_BUILDER_TYPES:
        raise ValueError(
            f"Unsupported body_builder type '{builder_type}'. "
            f"Supported: {sorted(SUPPORTED_BUILDER_TYPES)}"
        )

    base_data: dict[str, Any] = dict(data or {})

    if builder_type == "llm_log_summary":
        summary = _summarize_records(records or [], body_builder_config)
        return {**base_data, **summary}, ""

    if builder_type in {"markdown_file", "static_text"}:
        return base_data, raw_text

    # template — caller data drives $variable substitution; template owns the body shape
    return base_data, ""


def _summarize_records(
    records: list[dict[str, Any]],
    body_builder_config: Any,
) -> dict[str, Any]:
    """Return structured summary fields from parsed JSONL records.

    Filters and limits are read from ``body_builder_config.filters``.
    """
    filters = getattr(body_builder_config, "filters", None)
    record_limit = int(getattr(filters, "record_limit", 500) if filters else 500)
    configured_levels: list[str] = list(getattr(filters, "levels", []) if filters else [])
    filter_levels = (
        {lvl.upper() for lvl in configured_levels}
        if configured_levels
        else {"WARNING", "ERROR", "CRITICAL"}
    )

    warnings: list[str] = []
    errors: list[str] = []
    failed_steps: list[str] = []

    for record in records[:record_limit]:
        level = str(record.get("level") or record.get("levelname") or "").upper()
        status = str(record.get("status") or "")
        message = str(record.get("message") or "")

        if "WARNING" in filter_levels and (level == "WARNING" or status == "warning"):
            warnings.append(_compact_line(record, message))
        if filter_levels & {"ERROR", "CRITICAL"} and (
            level in {"ERROR", "CRITICAL"} or status == "failed"
        ):
            errors.append(_compact_line(record, message))
        if record.get("event_type") == "pipeline_step_completed" and status == "failed":
            step = str(record.get("pipeline_step_name") or record.get("app") or "unknown")
            failed_steps.append(step)

    return {
        "warning_count": str(len(warnings)),
        "error_count": str(len(errors)),
        "failed_step_count": str(len(failed_steps)),
        "failed_steps": ", ".join(dict.fromkeys(failed_steps)) or "None",
        "warning_summary": _bullet_list(warnings) or "- None",
        "error_summary": _bullet_list(errors) or "- None",
    }


def _compact_line(record: dict[str, Any], message: str) -> str:
    """Return one human-readable event line."""
    step = (
        record.get("pipeline_step_name")
        or record.get("app")
        or record.get("source")
        or ""
    )
    prefix = f"{step}: " if step else ""
    return f"{prefix}{message}".strip()


def _bullet_list(items: list[str], limit: int = 10) -> str:
    """Render summary lines as Markdown bullets."""
    selected = items[:limit]
    lines = [f"- {item}" for item in selected if item]
    if len(items) > limit:
        lines.append(f"- {len(items) - limit} additional events omitted")
    return "\n".join(lines)

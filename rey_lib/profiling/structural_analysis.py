"""Bounded deterministic structural analysis of inspected delimited files.

Reduces many inspected source files sharing one structural file type into a
single bounded analysis -- structural patterns with representatives, and a
ranked ordered-context section -- together with its canonical rendering.

This module is pure. It performs no file IO, takes no context, emits no
evidence, and returns the analysis and its rendered text rather than writing
anything. Writing the artifact, digesting it, registering it in a manifest, and
emitting lifecycle evidence stay with the application that owns those
vocabularies.

The reduction is cross-file by design. It is grouped by structural file type
and takes every inspected source for that type together; it is not a per-file
algorithm applied repeatedly.

Ten limits bound the output. Four are published in the artifact itself under
``analysis_limits``; the other six shape it without being advertised. All ten
are readable through :data:`ANALYSIS_LIMITS` so a caller never has to reach for
a private constant.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from rey_lib.encryption import sha256_text
from rey_lib.files.json import render_json
from rey_lib.profiling.csv_profile import normalized_header

__all__ = [
    "ANALYSIS_LIMITS",
    "AnalysisLimits",
    "StructuralAnalysis",
    "build_structural_analysis",
    "field_characteristic",
    "length_bucket",
    "structural_descriptor",
]

# ---------------------------------------------------------------------------
# Limits
#
# These values are part of the artifact contract. Every analysis produced since
# this reduction existed was bounded by them, and the digest of that artifact is
# recorded, so changing one changes recorded identity for every file type
# analysed afterwards.
# ---------------------------------------------------------------------------

_MAX_PATTERN_RECORDS = 1_000
_MAX_REPRESENTATIVES_PER_PATTERN = 3
_MAX_ORDERED_CONTEXT_RECORDS = 2_000
_MAX_ANALYSIS_BYTES = 2_000_000
_COMMON_PATTERN_RESERVE = 10
_CONTEXT_RADIUS = 2
_MAX_EVIDENCE_TEXT_BYTES = 1_024
_MAX_REPRESENTATIVE_TEXT_BYTES = 512
_MAX_REPRESENTATIVE_FIELDS = 20
_MAX_FIELD_TEXT_BYTES = 128


@dataclass(frozen=True)
class AnalysisLimits:
    """The ten bounds that shape one analysis.

    A readable view of the module constants, not an injection point. The
    reduction reads the constants directly, exactly as it always has; this
    exists so a caller can state what the bounds are without importing private
    names.
    """

    max_pattern_records: int
    max_representatives_per_pattern: int
    max_ordered_context_records: int
    max_analysis_bytes: int
    common_pattern_reserve: int
    context_radius: int
    max_evidence_text_bytes: int
    max_representative_text_bytes: int
    max_representative_fields: int
    max_field_text_bytes: int


ANALYSIS_LIMITS = AnalysisLimits(
    max_pattern_records=_MAX_PATTERN_RECORDS,
    max_representatives_per_pattern=_MAX_REPRESENTATIVES_PER_PATTERN,
    max_ordered_context_records=_MAX_ORDERED_CONTEXT_RECORDS,
    max_analysis_bytes=_MAX_ANALYSIS_BYTES,
    common_pattern_reserve=_COMMON_PATTERN_RESERVE,
    context_radius=_CONTEXT_RADIUS,
    max_evidence_text_bytes=_MAX_EVIDENCE_TEXT_BYTES,
    max_representative_text_bytes=_MAX_REPRESENTATIVE_TEXT_BYTES,
    max_representative_fields=_MAX_REPRESENTATIVE_FIELDS,
    max_field_text_bytes=_MAX_FIELD_TEXT_BYTES,
)


@dataclass(frozen=True)
class StructuralAnalysis:
    """One bounded analysis and the exact text that represents it.

    ``text`` is what a caller writes. It is produced here rather than left to
    the caller because the rendering is part of the analysis -- the artifact's
    bytes are hashed and that digest is recorded.
    """

    analysis: dict[str, Any]
    text: str


def build_structural_analysis(
    *,
    file_type: str,
    file_type_artifact: dict[str, Any],
    file_type_artifact_name: str,
    matching_files: list[str],
    inspection_artifacts: list[str],
    inspections_by_source: dict[str, dict[str, Any]],
) -> StructuralAnalysis:
    """Reduce every inspected source of one file type to one bounded analysis.

    Parameters
    ----------
    file_type : str
        The structural file-type identity the sources share.
    file_type_artifact : dict[str, Any]
        The decoded file-type artifact, read by the caller.
    file_type_artifact_name : str
        Its file name, recorded in the analysis.
    matching_files : list[str]
        Every source belonging to this file type.
    inspection_artifacts : list[str]
        The inspection artifact names, one per source.
    inspections_by_source : dict[str, dict[str, Any]]
        Inspection metadata and rows keyed by source file. Cross-file by
        design; this is what makes the reduction grouped rather than per-file.

    Returns
    -------
    StructuralAnalysis
        The analysis mapping and its rendered text. Nothing is written.
    """
    analysis, text = _build_bounded_analysis(
        file_type=file_type,
        file_type_artifact=file_type_artifact,
        file_type_artifact_name=file_type_artifact_name,
        matching_files=matching_files,
        inspection_artifacts=inspection_artifacts,
        inspections_by_source=inspections_by_source,
    )
    return StructuralAnalysis(analysis=analysis, text=text)


# ---------------------------------------------------------------------------
# Private — the reduction itself, unchanged. Every helper below is reached
# only through build_structural_analysis.
# ---------------------------------------------------------------------------


def _build_bounded_analysis(
    *,
    file_type: str,
    file_type_artifact: dict[str, Any],
    file_type_artifact_name: str,
    matching_files: list[str],
    inspection_artifacts: list[str],
    inspections_by_source: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Build the primary pattern summary and secondary ordered context."""

    delimiter = _analysis_delimiter(inspections_by_source)
    expected_headers = list(file_type_artifact.get("normalized_headers") or [])
    patterns, row_facts = _aggregate_structural_patterns(
        inspections_by_source,
        delimiter=delimiter,
        expected_headers=expected_headers,
    )
    ranked_context = _rank_ordered_context(row_facts)

    pattern_limit = min(len(patterns), _MAX_PATTERN_RECORDS)
    context_limit = min(len(ranked_context), _MAX_ORDERED_CONTEXT_RECORDS)
    representative_limit = _MAX_REPRESENTATIVES_PER_PATTERN

    while True:
        selected_patterns = _select_patterns(patterns, pattern_limit)
        selected_signatures = {
            pattern["structural_signature"] for pattern in selected_patterns
        }
        omitted_signatures = sorted(set(patterns) - selected_signatures)
        pattern_records = [
            _render_pattern(pattern, representative_limit)
            for pattern in sorted(
                selected_patterns,
                key=lambda item: item["structural_signature"],
            )
        ]
        context_records = [
            _render_context_record(item)
            for item in sorted(
                ranked_context[:context_limit],
                key=lambda item: (
                    item["source_file"],
                    int(item["row"].get("physical_line_number") or 0),
                ),
            )
        ]
        omitted_context_count = len(row_facts) - len(context_records)
        truncated = bool(omitted_signatures or omitted_context_count)
        analysis = {
            "schema_version": 2,
            "file_type": file_type,
            "file_type_artifact": file_type_artifact_name,
            "signature": file_type_artifact.get("signature"),
            "layout": file_type_artifact.get("layout"),
            "ordered_headers": list(file_type_artifact.get("ordered_headers") or []),
            "normalized_headers": expected_headers,
            "matching_files": matching_files,
            "profile_artifacts": list(
                file_type_artifact.get("profile_artifacts") or []
            ),
            "inspection_artifacts": inspection_artifacts,
            "analysis_limits": {
                "max_pattern_records": _MAX_PATTERN_RECORDS,
                "max_representatives_per_pattern": (_MAX_REPRESENTATIVES_PER_PATTERN),
                "max_ordered_context_records": _MAX_ORDERED_CONTEXT_RECORDS,
                "max_serialized_bytes": _MAX_ANALYSIS_BYTES,
            },
            "completeness": {
                "bounded": True,
                "truncated": truncated,
                "patterns": {
                    "observed": len(patterns),
                    "included": len(pattern_records),
                    "omitted": len(omitted_signatures),
                },
                "ordered_context": {
                    "observed": len(row_facts),
                    "included": len(context_records),
                    "omitted": omitted_context_count,
                },
            },
            "structural_patterns": {
                "primary_payload": True,
                "signature_version": 1,
                "observed_pattern_count": len(patterns),
                "included_pattern_count": len(pattern_records),
                "omitted_pattern_count": len(omitted_signatures),
                "omitted_signatures_sha256": _signature_set_sha256(omitted_signatures),
                "records": pattern_records,
            },
            "ordered_context": {
                "secondary_evidence": True,
                "selection": ("structural_events_with_context_then_evenly_spaced"),
                "observed_row_count": len(row_facts),
                "included_row_count": len(context_records),
                "records": context_records,
            },
        }
        analysis_text = (
            json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if len(analysis_text.encode("utf-8")) <= _MAX_ANALYSIS_BYTES:
            return analysis, analysis_text

        if context_limit:
            context_limit = max(0, context_limit - max(1, context_limit // 8))
            continue
        if representative_limit > 1:
            representative_limit -= 1
            continue
        if pattern_limit > 1:
            pattern_limit = max(1, pattern_limit - max(1, pattern_limit // 8))
            continue
        raise InputError(
            f"File-type analysis for '{file_type}' cannot fit within "
            f"{_MAX_ANALYSIS_BYTES} serialized bytes."
        )


def _aggregate_structural_patterns(
    inspections_by_source: dict[str, dict[str, Any]],
    *,
    delimiter: str,
    expected_headers: list[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Group every inspected row by one stable structural signature."""

    patterns: dict[str, dict[str, Any]] = {}
    row_facts: list[dict[str, Any]] = []
    expected_width = len(expected_headers)

    for source_file in sorted(inspections_by_source):
        source = inspections_by_source[source_file]
        rows = sorted(
            source["_rows"],
            key=lambda row: (
                int(row.get("physical_line_number") or 0),
                int(row.get("logical_row_number") or 0),
            ),
        )
        previous_region: str | None = None
        previous_shape: tuple[Any, ...] | None = None
        for row in rows:
            descriptor = structural_descriptor(
                row,
                delimiter=delimiter,
                expected_headers=expected_headers,
                expected_width=expected_width,
            )
            signature = _canonical_sha256(descriptor)
            occurrence = {
                "source_file": source_file,
                "physical_line_number": int(row.get("physical_line_number") or 0),
                "logical_row_number": int(row.get("logical_row_number") or 0),
            }
            pattern = patterns.setdefault(
                signature,
                {
                    "structural_signature": signature,
                    "descriptor": descriptor,
                    "occurrence_count": 0,
                    "first_occurrence": occurrence,
                    "last_occurrence": occurrence,
                    "source_occurrence_counts": {},
                    "representatives": [],
                    "important": _important_descriptor(descriptor),
                },
            )
            pattern["occurrence_count"] += 1
            pattern["last_occurrence"] = occurrence
            source_counts = pattern["source_occurrence_counts"]
            source_counts[source_file] = source_counts.get(source_file, 0) + 1
            if len(pattern["representatives"]) < _MAX_REPRESENTATIVES_PER_PATTERN:
                pattern["representatives"].append(
                    _representative_record(row, source_file)
                )

            shape = (
                descriptor["field_count"],
                descriptor["blank"],
                descriptor["parse_status"],
                descriptor["header_candidate"],
                descriptor["repeated_header_shape"],
                descriptor["expected_width_relation"],
            )
            reasons = _structural_event_reasons(
                descriptor,
                is_first=previous_shape is None,
                region_changed=(
                    previous_region is not None
                    and descriptor["region"] != previous_region
                ),
                shape_changed=(previous_shape is not None and shape != previous_shape),
            )
            row_facts.append(
                {
                    "source_file": source_file,
                    "row": row,
                    "structural_signature": signature,
                    "descriptor": descriptor,
                    "event_reasons": reasons,
                }
            )
            previous_region = descriptor["region"]
            previous_shape = shape

        if rows:
            row_facts[-1]["event_reasons"] = sorted(
                set(row_facts[-1]["event_reasons"]) | {"source_end"}
            )

    return patterns, row_facts


def structural_descriptor(
    row: dict[str, Any],
    *,
    delimiter: str,
    expected_headers: list[str],
    expected_width: int,
) -> dict[str, Any]:
    text = str(row.get("text") or "")
    parsed_fields = [str(field) for field in list(row.get("parsed_fields") or [])]
    field_count = int(row.get("field_count") or len(parsed_fields))
    is_blank = bool(row.get("is_blank"))
    parse_status = str(row.get("parse_status") or "parsed")
    header_candidate = bool(row.get("is_header_candidate"))
    normalized_fields = normalized_header(parsed_fields)
    header_identity = str(row.get("header_candidate_identity_sha256") or "") or (
        _canonical_sha256(normalized_fields) if header_candidate else None
    )
    repeated_header_shape = bool(
        expected_headers
        and header_identity == _canonical_sha256(expected_headers)
        and not bool(row.get("is_detected_header"))
    )
    if is_blank:
        width_relation = "blank"
    elif field_count < expected_width:
        width_relation = "narrower"
    elif field_count > expected_width:
        width_relation = "wider"
    else:
        width_relation = "equal"

    return {
        "field_count": field_count,
        "blank": is_blank,
        "line_length_bucket": length_bucket(int(row.get("line_length") or len(text))),
        "delimiter_count": int(
            row.get("delimiter_count")
            if row.get("delimiter_count") is not None
            else text.count(delimiter)
        ),
        "parse_status": parse_status,
        "normalized_field_characteristics": _run_length_encode(
            [field_characteristic(field) for field in parsed_fields]
        ),
        "header_candidate": header_candidate,
        "header_identity_sha256": header_identity,
        "detected_header": bool(row.get("is_detected_header")),
        "repeated_header_shape": repeated_header_shape,
        "expected_width_relation": width_relation,
        "region": str(row.get("region") or "unknown"),
    }


def field_characteristic(value: str) -> str:
    """Return one value's canonical datatype with its length bucket.

    The datatype comes from the one canonical detector; this adds only the
    length bucket that structural comparison needs. It owns no datatype
    definition of its own, so a date here is the same date everywhere.
    """
    from rey_lib.profiling.file_profiler import detect_datatype

    token = value.strip()
    return f"{detect_datatype(token)}:{length_bucket(len(token))}"


def _run_length_encode(values: list[str]) -> list[dict[str, Any]]:
    encoded: list[dict[str, Any]] = []
    for value in values:
        if encoded and encoded[-1]["characteristic"] == value:
            encoded[-1]["count"] += 1
        else:
            encoded.append({"characteristic": value, "count": 1})
    return encoded


def length_bucket(length: int) -> str:
    if length == 0:
        return "0"
    for maximum, label in (
        (16, "1-16"),
        (32, "17-32"),
        (64, "33-64"),
        (128, "65-128"),
        (256, "129-256"),
        (512, "257-512"),
        (1_024, "513-1024"),
    ):
        if length <= maximum:
            return label
    return "1025+"


def _important_descriptor(descriptor: dict[str, Any]) -> bool:
    return bool(
        descriptor["blank"]
        or descriptor["parse_status"] != "parsed"
        or descriptor["header_candidate"]
        or descriptor["detected_header"]
        or descriptor["repeated_header_shape"]
        or descriptor["expected_width_relation"] not in {"equal", "blank"}
        or descriptor["region"] != "data"
    )


def _structural_event_reasons(
    descriptor: dict[str, Any],
    *,
    is_first: bool,
    region_changed: bool,
    shape_changed: bool,
) -> list[str]:
    reasons: list[str] = []
    if is_first:
        reasons.append("source_start")
    if descriptor["detected_header"]:
        reasons.append("detected_header")
    elif descriptor["header_candidate"]:
        reasons.append("header_candidate")
    if descriptor["repeated_header_shape"]:
        reasons.append("repeated_header_shape")
    if descriptor["parse_status"] != "parsed":
        reasons.append("parse_anomaly")
    if descriptor["expected_width_relation"] not in {"equal", "blank"}:
        reasons.append("field_count_anomaly")
    if region_changed:
        reasons.append("region_transition")
    if shape_changed:
        reasons.append("structural_transition")
    return reasons


def _select_patterns(
    patterns: dict[str, dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Prioritize rare/important shapes while reserving common baselines."""

    all_patterns = list(patterns.values())
    if len(all_patterns) <= limit:
        return all_patterns

    priority = sorted(
        all_patterns,
        key=lambda item: (
            not item["important"],
            item["occurrence_count"],
            item["structural_signature"],
        ),
    )
    common = sorted(
        patterns.values(),
        key=lambda item: (
            -item["occurrence_count"],
            item["structural_signature"],
        ),
    )
    common_reserve = min(_COMMON_PATTERN_RESERVE, limit // 10)
    rare_slots = limit - common_reserve
    selected = priority[:rare_slots]
    selected_signatures = {item["structural_signature"] for item in selected}
    for item in common:
        if len(selected) >= limit:
            break
        if item["structural_signature"] not in selected_signatures:
            selected.append(item)
            selected_signatures.add(item["structural_signature"])
    if len(selected) < limit:
        for item in priority[rare_slots:]:
            if len(selected) >= limit:
                break
            if item["structural_signature"] not in selected_signatures:
                selected.append(item)
                selected_signatures.add(item["structural_signature"])
    return selected


def _rank_ordered_context(
    row_facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank context windows around structural events, then sequence samples."""

    by_source: dict[str, list[dict[str, Any]]] = {}
    for fact in row_facts:
        by_source.setdefault(fact["source_file"], []).append(fact)

    selected: dict[tuple[str, int], dict[str, Any]] = {}
    for source_file in sorted(by_source):
        facts = by_source[source_file]
        for index, fact in enumerate(facts):
            reasons = fact["event_reasons"]
            if not reasons:
                continue
            anchor_priority = _event_priority(reasons)
            for context_index in range(
                max(0, index - _CONTEXT_RADIUS),
                min(len(facts), index + _CONTEXT_RADIUS + 1),
            ):
                context = facts[context_index]
                distance = abs(context_index - index)
                key = (
                    source_file,
                    int(context["row"].get("physical_line_number") or 0),
                )
                reason_values = (
                    reasons
                    if distance == 0
                    else [f"context_for:{reason}" for reason in reasons]
                )
                candidate = {
                    **context,
                    "context_reasons": sorted(set(reason_values)),
                    "selection_priority": (
                        anchor_priority,
                        distance,
                        source_file,
                        key[1],
                    ),
                }
                existing = selected.get(key)
                if existing is None:
                    selected[key] = candidate
                else:
                    existing["context_reasons"] = sorted(
                        set(existing["context_reasons"])
                        | set(candidate["context_reasons"])
                    )
                    existing["selection_priority"] = min(
                        existing["selection_priority"],
                        candidate["selection_priority"],
                    )

    remaining_capacity = max(
        0,
        _MAX_ORDERED_CONTEXT_RECORDS - len(selected),
    )
    if remaining_capacity:
        unsampled = [
            fact
            for fact in row_facts
            if (
                fact["source_file"],
                int(fact["row"].get("physical_line_number") or 0),
            )
            not in selected
        ]
        for fact in _bounded_rows(unsampled, remaining_capacity):
            line_number = int(fact["row"].get("physical_line_number") or 0)
            key = (fact["source_file"], line_number)
            selected[key] = {
                **fact,
                "context_reasons": ["deterministic_sequence_sample"],
                "selection_priority": (
                    9,
                    0,
                    fact["source_file"],
                    line_number,
                ),
            }
    return sorted(
        selected.values(),
        key=lambda item: item["selection_priority"],
    )


def _event_priority(reasons: list[str]) -> int:
    if set(reasons) & {
        "detected_header",
        "repeated_header_shape",
        "parse_anomaly",
    }:
        return 0
    if set(reasons) & {
        "header_candidate",
        "field_count_anomaly",
        "region_transition",
    }:
        return 1
    return 2


def _representative_record(
    row: dict[str, Any],
    source_file: str,
) -> dict[str, Any]:
    text, text_truncated = _truncate_utf8(
        str(row.get("text") or ""),
        _MAX_REPRESENTATIVE_TEXT_BYTES,
    )
    parsed_fields = [
        _truncate_utf8(str(field), _MAX_FIELD_TEXT_BYTES)[0]
        for field in list(row.get("parsed_fields") or [])[:_MAX_REPRESENTATIVE_FIELDS]
    ]
    field_count = int(row.get("field_count") or len(parsed_fields))
    return {
        "source_file": source_file,
        "physical_line_number": int(row.get("physical_line_number") or 0),
        "logical_row_number": int(row.get("logical_row_number") or 0),
        "text": text,
        "text_truncated": text_truncated,
        "parsed_fields": parsed_fields,
        "parsed_fields_omitted_count": max(0, field_count - len(parsed_fields)),
    }


def _render_pattern(
    pattern: dict[str, Any],
    representative_limit: int,
) -> dict[str, Any]:
    representatives = pattern["representatives"][:representative_limit]
    return {
        "structural_signature": pattern["structural_signature"],
        "descriptor": pattern["descriptor"],
        "occurrence_count": pattern["occurrence_count"],
        "first_occurrence": pattern["first_occurrence"],
        "last_occurrence": pattern["last_occurrence"],
        "representative_line_numbers": [
            {
                "source_file": item["source_file"],
                "physical_line_number": item["physical_line_number"],
                "logical_row_number": item["logical_row_number"],
            }
            for item in representatives
        ],
        "source_occurrence_counts": [
            {
                "source_file": source_file,
                "count": pattern["source_occurrence_counts"][source_file],
            }
            for source_file in sorted(pattern["source_occurrence_counts"])
        ],
        "representatives": representatives,
    }


def _render_context_record(item: dict[str, Any]) -> dict[str, Any]:
    row = item["row"]
    text, text_truncated = _truncate_utf8(
        str(row.get("text") or ""),
        _MAX_EVIDENCE_TEXT_BYTES,
    )
    return {
        "source_file": item["source_file"],
        "physical_line_number": int(row.get("physical_line_number") or 0),
        "logical_row_number": int(row.get("logical_row_number") or 0),
        "structural_signature": item["structural_signature"],
        "context_reasons": item["context_reasons"],
        "text": text,
        "text_truncated": text_truncated,
        "field_count": int(row.get("field_count") or 0),
        "parse_status": str(row.get("parse_status") or "parsed"),
        "is_blank": bool(row.get("is_blank")),
        "is_detected_header": bool(row.get("is_detected_header")),
        "is_header_candidate": bool(row.get("is_header_candidate")),
        "region": str(row.get("region") or "unknown"),
    }


def _analysis_delimiter(
    inspections_by_source: dict[str, dict[str, Any]],
) -> str:
    delimiters = {
        str(source.get("delimiter") or "") for source in inspections_by_source.values()
    }
    if len(delimiters) != 1 or not next(iter(delimiters)):
        raise InputError(
            "Inspection artifacts for one file type must share one delimiter."
        )
    return next(iter(delimiters))


def _truncate_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value, False
    truncated = encoded[:maximum_bytes]
    while True:
        try:
            return truncated.decode("utf-8"), True
        except UnicodeDecodeError:
            truncated = truncated[:-1]


def _canonical_sha256(value: Any) -> str:
    # Byte-for-byte the representation the canonical renderer is defined as,
    # so every identity hash already recorded stays valid.
    return sha256_text(render_json(value, mode="canonical"))


def _signature_set_sha256(signatures: list[str]) -> str:
    return _canonical_sha256(signatures)


def _bounded_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Return deterministic, ordered coverage of a possibly large inspection."""

    if len(rows) <= limit:
        return rows
    if limit < 2:
        return rows[:limit]
    last = len(rows) - 1
    indices = {(offset * last) // (limit - 1) for offset in range(limit)}
    return [rows[index] for index in sorted(indices)]

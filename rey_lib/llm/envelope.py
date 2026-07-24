"""Universal JSON-envelope handling for LLM artifact generation.

Provider/model independent. Any LLM generation request that targets a typed
artifact (sql, yaml, python, ...) asks the model to return one standard JSON
envelope:

    {"artifact_type": "<type>", "content": "<artifact>", "notes": []}

The shared layer then strips accidental outer Markdown fencing, parses the
JSON, extracts the ``content`` field, validates it for the artifact type, and
returns only the clean content for writing to the final artifact file.

This module never branches on a specific provider or model. Every provider is
asked for the same envelope, and any provider/model may return it cleanly or
wrapped in fencing — both are normalised here.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from rey_lib.llm.exceptions import ParseFailure

__all__ = [
    "ARTIFACT_TYPE_FIELD",
    "CONTENT_FIELD",
    "NOTES_FIELD",
    "REJECTION_PREFIX",
    "build_envelope_instruction",
    "extract_artifact",
    "extract_artifact_envelope",
    "loads_llm_json",
    "rejection_reason_from_notes",
]

ARTIFACT_TYPE_FIELD = "artifact_type"
CONTENT_FIELD = "content"
NOTES_FIELD = "notes"

# A contract may decline an object by returning the content unchanged and adding
# a notes entry that starts with this prefix. The reason follows the prefix.
REJECTION_PREFIX = "REJECTED"

# Artifact types whose extracted content must be plain code/data — no Markdown
# fencing and no leftover JSON-envelope wrapper. Other types (markdown, text,
# html) pass through unvalidated.
_STRICT_ARTIFACT_TYPES = frozenset(
    {"sql", "ddl_commented_sql", "python", "shell", "yaml", "rey_loader_yaml",
     "json", "xml", "csv"}
)


def loads_llm_json(text: str) -> Any:
    """Parse model-produced JSON with one shared, provider-neutral policy.

    Literal control characters inside strings are accepted. If a model emits a
    non-JSON escape commonly found in Markdown (for example ``\\_``), only that
    invalid escape marker is protected and parsing is retried. The decoded
    value retains the backslash so Markdown semantics are unchanged.
    """
    try:
        return json.loads(text, strict=False)
    except (json.JSONDecodeError, ValueError) as first_error:
        repaired = _protect_invalid_string_escapes(text)
        try:
            return json.loads(repaired, strict=False)
        except (json.JSONDecodeError, ValueError):
            completed = _complete_truncated_json_document(repaired)
            if completed == repaired:
                raise first_error
            return json.loads(completed, strict=False)


def _protect_invalid_string_escapes(text: str) -> str:
    """Make invalid backslash escapes legal only while inside JSON strings."""
    valid_escapes = frozenset('"\\/bfnrtu')
    output: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        character = text[index]
        if character == '"' and not in_string:
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == '"' and in_string:
            in_string = False
            output.append(character)
            index += 1
            continue
        if character == "\\" and in_string and index + 1 < len(text):
            escaped = text[index + 1]
            if escaped in valid_escapes:
                output.extend((character, escaped))
                index += 2
                continue
            output.extend(("\\", "\\"))
            index += 1
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _complete_truncated_json_document(text: str) -> str:
    """Close only a final unterminated string and its open JSON containers."""
    stack: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        character = text[index]
        if in_string and character == "\\" and index + 1 < len(text):
            index += 2
            continue
        if character == '"':
            in_string = not in_string
        elif not in_string and character in "{[":
            stack.append(character)
        elif not in_string and character in "}]":
            expected = "{" if character == "}" else "["
            if not stack or stack[-1] != expected:
                return text
            stack.pop()
        index += 1
    if not in_string:
        return text
    suffix = ['"']
    suffix.extend("}" if opener == "{" else "]" for opener in reversed(stack))
    return text + "".join(suffix)


def build_envelope_instruction(artifact_type: str) -> str:
    """Return the standard JSON-envelope output instruction for a request.

    Provider/model independent — every provider receives the same instruction
    so the shared layer can extract the artifact uniformly.

    Parameters
    ----------
    artifact_type : str
        The requested artifact type (e.g. ``"sql"``). Falls back to ``"text"``.

    Returns
    -------
    str
        Instruction text appended to the user message.
    """
    at = (artifact_type or "text").lower()
    return (
        "\n\nReturn your response as valid JSON only. Use this exact structure:\n"
        "{\n"
        f'  "{ARTIFACT_TYPE_FIELD}": "{at}",\n'
        f'  "{CONTENT_FIELD}": "<{at.upper()} ONLY>",\n'
        f'  "{NOTES_FIELD}": []\n'
        "}\n"
        f"The {CONTENT_FIELD} field must contain {at} only. "
        "Do not include Markdown fences inside the content field. "
        "Do not include explanations inside the content field. "
        f"Do not include JSON inside the content field unless {ARTIFACT_TYPE_FIELD} "
        "is json."
    )


def extract_artifact(
    raw_response: str | dict[str, Any],
    artifact_type: str,
) -> str:
    """Extract clean artifact content from a JSON-envelope response.

    Thin wrapper over :func:`extract_artifact_envelope` returning the content
    only — preserves the historical signature for existing callers.

    Parameters
    ----------
    raw_response : str
        The model response text (possibly fenced).
    artifact_type : str
        The expected artifact type (e.g. ``"sql"``).

    Returns
    -------
    str
        The clean artifact content, ready to write to the final file.

    Raises
    ------
    ParseFailure
        See :func:`extract_artifact_envelope`.
    """
    content, _notes = extract_artifact_envelope(raw_response, artifact_type)
    return content


def extract_artifact_envelope(
    raw_response: str | dict[str, Any],
    artifact_type: str,
) -> tuple[str, list[Any]]:
    """Extract ``(content, notes)`` from a JSON-envelope response.

    Strips an accidental outer Markdown fence, parses the JSON envelope,
    extracts and validates the ``content`` field, and returns the ``notes`` list
    alongside it. ``notes`` lets a contract carry an out-of-band signal (e.g. a
    rejection reason) without putting it inside the artifact content.

    Parameters
    ----------
    raw_response : str
        The model response text (possibly fenced).
    artifact_type : str
        The expected artifact type (e.g. ``"sql"``).

    Returns
    -------
    tuple[str, list[Any]]
        The clean artifact content and the envelope ``notes`` (empty when
        absent).

    Raises
    ------
    ParseFailure
        If the response cannot be parsed as JSON after fence stripping, the
        ``content`` field is missing, or the extracted content fails artifact
        validation. The original raw response is left untouched for review.
    """
    if isinstance(raw_response, dict):
        envelope = raw_response
    else:
        text = _strip_outer_fence((raw_response or "").strip())
        try:
            envelope = loads_llm_json(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ParseFailure(
                "LLM response extraction failed. Expected a JSON envelope with a "
                f"'{CONTENT_FIELD}' field. Artifact type: {artifact_type or 'text'}. "
                f"The raw response was preserved for review. JSON parse error: {exc}"
            ) from exc

    if not isinstance(envelope, dict) or CONTENT_FIELD not in envelope:
        raise ParseFailure(
            "LLM response extraction failed. The JSON response did not contain "
            f"the required field: '{CONTENT_FIELD}'. No final artifact was written."
        )

    content = envelope[CONTENT_FIELD]
    if (artifact_type or "").lower() == "json":
        # For json artifacts the content may be a JSON-serialisable value.
        content = content if isinstance(content, str) else json.dumps(content, indent=2)
    elif not isinstance(content, str):
        content = str(content)
    content = content.strip()

    _validate_content(content, (artifact_type or "").lower())

    notes = envelope.get(NOTES_FIELD)
    return content, (notes if isinstance(notes, list) else [])


def rejection_reason_from_notes(notes: list[Any]) -> Optional[str]:
    """Return a rejection reason when ``notes`` signals a declined object.

    A contract may decline an object by returning the content unchanged and
    adding a notes entry beginning with ``REJECTION_PREFIX`` (e.g.
    ``"REJECTED: object cannot be safely documented"``). The text after the
    prefix (and an optional ``:``) is returned as the reason; ``None`` when no
    note signals rejection.

    Parameters
    ----------
    notes : list[Any]
        The envelope ``notes`` list.

    Returns
    -------
    Optional[str]
        The rejection reason, or None.
    """
    for note in notes or []:
        if isinstance(note, str) and note.strip().upper().startswith(REJECTION_PREFIX):
            reason = note.strip()[len(REJECTION_PREFIX):].lstrip(": ").strip()
            return reason or "object declined by contract"
    return None


def _validate_content(content: str, artifact_type: str) -> None:
    """Validate extracted content for strict artifact types.

    Strict types must not contain Markdown fencing or a leftover JSON-envelope
    wrapper. Non-strict types (markdown, text, html) pass through unvalidated.

    Parameters
    ----------
    content : str
        The extracted content.
    artifact_type : str
        The (lower-cased) artifact type.

    Raises
    ------
    ParseFailure
        If strict content still contains fencing or a JSON wrapper.
    """
    if artifact_type not in _STRICT_ARTIFACT_TYPES:
        return

    if "```" in content:
        raise ParseFailure(
            f"LLM artifact validation failed. Artifact type: {artifact_type}. "
            "Reason: extracted content still contains Markdown fencing. "
            "No final artifact was written."
        )

    head = content.lstrip()[:200]
    if (
        artifact_type != "json"
        and head.startswith("{")
        and f'"{ARTIFACT_TYPE_FIELD}"' in head
    ):
        raise ParseFailure(
            f"LLM artifact validation failed. Artifact type: {artifact_type}. "
            "Reason: extracted content still contains a JSON envelope wrapper. "
            "No final artifact was written."
        )


def _strip_outer_fence(text: str) -> str:
    """Remove a single outer Markdown code fence if present.

    Parameters
    ----------
    text : str
        Response text that may be wrapped in a ``` fence.

    Returns
    -------
    str
        Text with one outer fence removed; unchanged if no leading fence.
    """
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:]  # drop the opening ``` or ```json line
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()

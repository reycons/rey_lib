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

from rey_lib.llm.exceptions import ParseFailure

__all__ = [
    "ARTIFACT_TYPE_FIELD",
    "CONTENT_FIELD",
    "NOTES_FIELD",
    "build_envelope_instruction",
    "extract_artifact",
]

ARTIFACT_TYPE_FIELD = "artifact_type"
CONTENT_FIELD = "content"
NOTES_FIELD = "notes"

# Artifact types whose extracted content must be plain code/data — no Markdown
# fencing and no leftover JSON-envelope wrapper. Other types (markdown, text,
# html) pass through unvalidated.
_STRICT_ARTIFACT_TYPES = frozenset(
    {"sql", "python", "shell", "yaml", "json", "xml", "csv"}
)


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


def extract_artifact(raw_response: str, artifact_type: str) -> str:
    """Extract clean artifact content from a JSON-envelope response.

    Strips an accidental outer Markdown fence, parses the JSON envelope,
    extracts the ``content`` field, and validates it for the artifact type.

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
        If the response cannot be parsed as JSON after fence stripping, the
        ``content`` field is missing, or the extracted content fails artifact
        validation. The original raw response is left untouched for review.
    """
    text = _strip_outer_fence((raw_response or "").strip())

    try:
        envelope = json.loads(text)
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
    return content


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

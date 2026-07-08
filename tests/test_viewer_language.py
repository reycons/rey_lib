"""Tests for viewer-oriented text language classification."""

from __future__ import annotations

from rey_lib.viewers import APPROVED_TEXT_LANGUAGES, classify_text_language


def test_classify_text_language_uses_approved_registry_only() -> None:
    """Every returned language is part of the shared viewer registry."""
    samples = [
        "query.sql",
        "config.yaml",
        "config.yml",
        "payload.json",
        "run.jsonl",
        "readme.md",
        "script.py",
        "rows.csv",
        "error.err",
        "plain.txt",
        "unknown.bin",
    ]

    for sample in samples:
        assert classify_text_language(sample) in APPROVED_TEXT_LANGUAGES


def test_classify_text_language_maps_supported_suffixes() -> None:
    """Known content types map to abstract viewer language values."""
    assert classify_text_language("query.sql") == "sql"
    assert classify_text_language("config.yaml") == "yaml"
    assert classify_text_language("config.yml") == "yaml"
    assert classify_text_language("payload.json") == "json"
    assert classify_text_language("run.jsonl") == "jsonl"
    assert classify_text_language("readme.md") == "markdown"
    assert classify_text_language("script.py") == "python"
    assert classify_text_language("rows.csv") == "csv"
    assert classify_text_language("app.log") == "log"
    assert classify_text_language("plain.txt") == "text"


def test_classify_text_language_can_use_mime_type() -> None:
    """MIME classification remains abstract and rendering-engine independent."""
    assert classify_text_language("download", "application/json; charset=utf-8") == "json"
    assert classify_text_language("download", "application/x-ndjson") == "jsonl"
    assert classify_text_language("download", "text/csv") == "csv"
    assert classify_text_language("download", "text/plain") == "text"


def test_classify_text_language_unknown_is_safe() -> None:
    """Unsupported inputs return unknown rather than guessing."""
    assert classify_text_language("") == "unknown"
    assert classify_text_language("README") == "unknown"
    assert classify_text_language("archive.bin") == "unknown"


def test_classify_text_language_can_use_display_name() -> None:
    """Backend callers can classify by display name when path is opaque."""
    assert classify_text_language("/opaque/artifact", display_name="query.sql") == "sql"
    assert classify_text_language("/opaque/artifact", display_name="config.yaml") == "yaml"


def test_classify_text_language_can_use_preview_content() -> None:
    """Backend preview content provides a final abstract content-type fallback."""
    assert classify_text_language("/opaque/artifact", content="SELECT *\nFROM trades;\n") == "sql"
    assert classify_text_language("/opaque/artifact", content='{"a": 1}\n{"b": 2}\n') == "jsonl"
    assert classify_text_language("/opaque/artifact", content='{"a": 1}\n') == "json"

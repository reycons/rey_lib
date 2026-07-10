"""Tests for the public redact_text secret-masking helper
(SGC_Pipeline_Coordinator_Step_Subprocess_Output_Streaming — shared sanitization)."""

from __future__ import annotations

from rey_lib.errors.error_utils import redact_text


def test_masks_key_value_secret_shapes() -> None:
    """password / token / api_key style key=value pairs are masked."""
    assert "[REDACTED]" in redact_text("password=hunter2")
    assert "hunter2" not in redact_text("password=hunter2")
    assert "[REDACTED]" in redact_text("api_key: AKIA-XYZ")
    assert "[REDACTED]" in redact_text("connection_string=postgres://u:p@h/db")


def test_masks_bearer_tokens() -> None:
    """Bearer tokens are masked."""
    out = redact_text("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "[REDACTED]" in out


def test_non_secret_text_is_unchanged() -> None:
    """Ordinary output text passes through untouched."""
    line = "applying sql file fidelity_transactions_v01.staging_table.sql"
    assert redact_text(line) == line


def test_handles_non_string_and_none() -> None:
    """Non-string / None inputs are coerced to text without error."""
    assert redact_text(None) == ""
    assert redact_text(42) == "42"

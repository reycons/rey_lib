"""Tests for file_operator.feed_config."""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.files.error_utils import RedactorError
from rey_lib.files.feed_config import FEED_CONFIG_FILENAME, load_feed_config


def _write_yaml(inbox: Path, content: str) -> Path:
    """Write a redact.yaml into ``inbox`` and return its path."""
    p = inbox / FEED_CONFIG_FILENAME
    p.write_text(content, encoding="utf-8")
    return p


class TestLoadFeedConfigAbsent:
    """No redact.yaml in inbox — returns None."""

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert load_feed_config(tmp_path) is None


class TestLoadFeedConfigValid:
    """Valid redact.yaml — parses correctly."""

    def test_basic_delimited(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, """
layout: delimited
delimiter: ","
encoding: utf-8-sig
file_pattern: "tran_*.csv"
redact_columns:
  - ACCOUNT NUMBER
  - MASTER ACCOUNT
""")
        cfg = load_feed_config(tmp_path)
        assert cfg is not None
        assert cfg.layout         == "delimited"
        assert cfg.delimiter      == ","
        assert cfg.encoding       == "utf-8-sig"
        assert cfg.file_pattern   == "tran_*.csv"
        assert cfg.redact_columns == ["ACCOUNT NUMBER", "MASTER ACCOUNT"]
        assert cfg.redact_ranges  == []

    def test_fixed_width_ranges(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, """
layout: fixed_width
file_pattern: "pos_*.txt"
redact_ranges:
  - 2-13
  - 42-53
""")
        cfg = load_feed_config(tmp_path)
        assert cfg is not None
        assert cfg.layout        == "fixed_width"
        assert cfg.redact_ranges == ["2-13", "42-53"]
        assert cfg.redact_columns == []

    def test_max_sample_rows_parsed(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "layout: delimited\nmax_sample_rows: 250\n")
        cfg = load_feed_config(tmp_path)
        assert cfg is not None
        assert cfg.max_sample_rows == 250

    def test_legacy_sample_size_is_a_compatibility_alias(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "layout: delimited\nsample_size: 125\n")
        cfg = load_feed_config(tmp_path)
        assert cfg is not None
        assert cfg.max_sample_rows == 125
        assert cfg.sample_size == 125

    def test_both_sample_limit_names_are_rejected(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path,
            "layout: delimited\nmax_sample_rows: 250\nsample_size: 125\n",
        )
        with pytest.raises(RedactorError, match="cannot both be configured"):
            load_feed_config(tmp_path)

    def test_defaults_when_keys_absent(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "layout: auto\n")
        cfg = load_feed_config(tmp_path)
        assert cfg is not None
        assert cfg.file_pattern == "*"
        assert cfg.delimiter    == ","
        assert cfg.encoding     == "utf-8-sig"
        assert cfg.max_sample_rows is None

    def test_file_pattern_list(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, """
layout: delimited
file_pattern:
  - "*.ps1"
  - "*.tr1"
""")
        cfg = load_feed_config(tmp_path)
        assert cfg is not None
        assert cfg.file_pattern == ["*.ps1", "*.tr1"]

    def test_filename_classification_regex_parsed(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, r"""
layout: delimited
classification:
  filename_regex: '^(?P<owner>.+?)_(?P<period>[A-Za-z]{3}\d{2})\.csv$'
""")
        cfg = load_feed_config(tmp_path)
        assert cfg is not None
        assert cfg.classification_filename_regex == (
            r"^(?P<owner>.+?)_(?P<period>[A-Za-z]{3}\d{2})\.csv$"
        )

    def test_filename_classification_absent_by_default(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "layout: delimited\n")
        cfg = load_feed_config(tmp_path)
        assert cfg is not None
        assert cfg.classification_filename_regex is None


class TestLoadFeedConfigInvalid:
    """Malformed redact.yaml — raises RedactorError."""

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "layout: [\nbad yaml")
        with pytest.raises(RedactorError, match="Cannot parse"):
            load_feed_config(tmp_path)

    def test_non_mapping_raises(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "- just\n- a list\n")
        with pytest.raises(RedactorError, match="must be a YAML mapping"):
            load_feed_config(tmp_path)

    def test_filename_classification_regex_must_compile(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, """
classification:
  filename_regex: '(?P<owner>'
""")
        with pytest.raises(RedactorError, match="not a valid regular expression"):
            load_feed_config(tmp_path)

    def test_filename_classification_regex_requires_named_group(
        self,
        tmp_path: Path,
    ) -> None:
        _write_yaml(tmp_path, """
classification:
  filename_regex: '^.+[.]csv$'
""")
        with pytest.raises(RedactorError, match="at least one named capture group"):
            load_feed_config(tmp_path)

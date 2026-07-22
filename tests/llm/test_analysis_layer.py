"""
Tests for the analysis domain layer.

Covers:
- AnalysisContractSpec parsing from extended frontmatter YAML
- DataProfile and PreparedInput from prepare()
- Preparation pipeline: column filtering, row filtering, sampling, redaction
- TextDataSource passthrough
- CSVDataSource extraction
- Analyzer.analyze() end-to-end with mock provider
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from rey_lib.llm.analysis import (
    AnalysisContract,
    AnalysisContractSpec,
    AnalysisResult,
    Analyzer,
    load_analysis_contract,
)
from rey_lib.llm.datasource import CSVDataSource, SourceData, TextDataSource
from rey_lib.llm.preparation import prepare
from rey_lib.llm.records import STATUS_SUCCESS
from rey_lib.llm.runner import _ProviderConfig  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_analysis_contract(
    tmp_path:   Path,
    extra_yaml: str = "",
    body:       str = "Analyse the data.",
) -> Path:
    """Write a minimal analysis contract with optional extra frontmatter."""
    contract = tmp_path / "analysis.md"
    parts = [
        "---\n",
        "name: test-analysis\n",
        "version: 1.0\n",
        "effective_date: 2025-01-01\n",
    ]
    if extra_yaml:
        parts.append(extra_yaml if extra_yaml.endswith("\n") else extra_yaml + "\n")
    parts.append(f"---\n\n{body}\n")
    contract.write_text("".join(parts), encoding="utf-8")
    return contract


def _make_source_data(
    rows:     list[dict[str, Any]] = None,
    raw_text: str = "",
    columns:  list[str] = None,
    ref:      str = "test",
) -> SourceData:
    """Build a SourceData for testing."""
    rows    = rows    or []
    columns = columns or (list(rows[0].keys()) if rows else [])
    return SourceData(
        rows        = rows,
        raw_text    = raw_text,
        columns     = columns,
        row_count   = len(rows),
        source_ref  = ref,
        truncated   = False,
        source_hash = "abc123",
    )


def _make_mock_provider(content: str = '{"result": "ok"}') -> MagicMock:
    """Return a mock provider whose run() returns the given JSON string."""
    from rey_lib.llm.providers.base import ProviderCapabilities, ProviderResponse

    caps = ProviderCapabilities(
        supports_tools           = False,
        supports_images          = False,
        supports_json_mode       = True,
        supports_streaming       = False,
        supports_system_messages = True,
    )
    response = ProviderResponse(
        content    = content,
        tokens_in  = 10,
        tokens_out = 10,
        model      = "mock-model",
        raw        = {},
    )
    provider = MagicMock()
    provider.capabilities = caps
    provider.run.return_value = response
    return provider


# ---------------------------------------------------------------------------
# load_analysis_contract — contract spec parsing
# ---------------------------------------------------------------------------

class TestLoadAnalysisContract:
    """Tests for load_analysis_contract()."""

    def test_minimal_contract_uses_defaults(self, tmp_path: Path) -> None:
        """A contract with no domain fields gets permissive defaults."""
        path     = _write_analysis_contract(tmp_path)
        contract = load_analysis_contract(path)

        assert contract.name    == "test-analysis"
        assert contract.version == "1.0"
        assert contract.spec.source_type      == "any"
        assert contract.spec.allowed_columns  == []
        assert contract.spec.required_filters == []
        assert contract.spec.max_rows         == 200
        assert contract.spec.sampling_method  == "head"
        assert contract.spec.sampling_seed    is None
        assert contract.spec.redaction        == []
        assert contract.spec.output_schema    is None

    def test_allowed_columns_parsed(self, tmp_path: Path) -> None:
        """allowed_columns list is parsed from YAML."""
        path = _write_analysis_contract(tmp_path, extra_yaml=(
            "allowed_columns:\n  - revenue\n  - region"
        ))
        contract = load_analysis_contract(path)
        assert contract.spec.allowed_columns == ["revenue", "region"]

    def test_required_filters_parsed(self, tmp_path: Path) -> None:
        """required_filters list is parsed from YAML."""
        path = _write_analysis_contract(tmp_path, extra_yaml=(
            "required_filters:\n"
            "  - column: status\n"
            "    operator: \"==\"\n"
            "    value: active\n"
        ))
        contract = load_analysis_contract(path)
        assert len(contract.spec.required_filters) == 1
        assert contract.spec.required_filters[0]["column"]   == "status"
        assert contract.spec.required_filters[0]["operator"] == "=="
        assert contract.spec.required_filters[0]["value"]    == "active"

    def test_sampling_parsed(self, tmp_path: Path) -> None:
        """sampling.method and sampling.seed are parsed."""
        path = _write_analysis_contract(tmp_path, extra_yaml=(
            "sampling:\n  method: random\n  seed: 42\n"
        ))
        contract = load_analysis_contract(path)
        assert contract.spec.sampling_method == "random"
        assert contract.spec.sampling_seed   == 42

    def test_redaction_parsed(self, tmp_path: Path) -> None:
        """redaction rules are parsed from YAML."""
        path = _write_analysis_contract(tmp_path, extra_yaml=(
            "redaction:\n  - column: ssn\n    mask: \"[SSN]\"\n"
        ))
        contract = load_analysis_contract(path)
        assert contract.spec.redaction == [{"column": "ssn", "mask": "[SSN]"}]

    def test_output_schema_parsed(self, tmp_path: Path) -> None:
        """Inline output_schema dict is parsed from YAML."""
        path = _write_analysis_contract(tmp_path, extra_yaml=(
            "output_schema:\n  type: object\n  properties:\n    total: {type: number}\n"
        ))
        contract = load_analysis_contract(path)
        assert contract.spec.output_schema is not None
        assert contract.spec.output_schema["type"] == "object"

    def test_nested_output_config_parsed(self, tmp_path: Path) -> None:
        """Canonical output.format settings drive raw artifact execution."""
        path = _write_analysis_contract(tmp_path, extra_yaml=(
            "output:\n"
            "  format: raw\n"
            "  artifact_type: rey_loader_yaml\n"
        ))
        contract = load_analysis_contract(path)
        assert contract.spec.output_format == "raw"
        assert contract.spec.artifact_type == "rey_loader_yaml"

    def test_invalid_source_type_raises(self, tmp_path: Path) -> None:
        """An unrecognised source_type raises ConfigurationFailure."""
        from rey_lib.llm.exceptions import ConfigurationFailure

        path = _write_analysis_contract(tmp_path, extra_yaml="source_type: ftp\n")
        with pytest.raises(ConfigurationFailure, match="source_type"):
            load_analysis_contract(path)

    def test_invalid_sampling_method_raises(self, tmp_path: Path) -> None:
        """An unrecognised sampling.method raises ConfigurationFailure."""
        from rey_lib.llm.exceptions import ConfigurationFailure

        path = _write_analysis_contract(tmp_path, extra_yaml="sampling:\n  method: zigzag\n")
        with pytest.raises(ConfigurationFailure, match="sampling.method"):
            load_analysis_contract(path)

    def test_contract_hash_and_path_set(self, tmp_path: Path) -> None:
        """hash and path are accessible as properties on AnalysisContract."""
        path     = _write_analysis_contract(tmp_path)
        contract = load_analysis_contract(path)
        assert contract.hash != ""
        assert contract.path == path.resolve()


# ---------------------------------------------------------------------------
# prepare() — column filtering
# ---------------------------------------------------------------------------

class TestColumnFiltering:
    """Tests for allowed_columns enforcement in prepare()."""

    def _rows(self) -> list[dict]:
        return [
            {"name": "Alice", "salary": 100, "ssn": "111-22-3333"},
            {"name": "Bob",   "salary": 200, "ssn": "444-55-6666"},
        ]

    def test_allowed_columns_restricts_output(self) -> None:
        """Only allowed columns appear in the rendered output."""
        sd     = _make_source_data(rows=self._rows())
        result = prepare(sd, allowed_columns=["name", "salary"])
        assert "ssn" not in result.rendered_text
        assert "name" in result.rendered_text
        assert "salary" in result.rendered_text

    def test_empty_allowed_columns_keeps_all(self) -> None:
        """Empty allowed_columns permits all columns through."""
        sd     = _make_source_data(rows=self._rows())
        result = prepare(sd, allowed_columns=[])
        assert "ssn" in result.rendered_text

    def test_profile_reflects_columns_used(self) -> None:
        """DataProfile.columns matches the filtered column set."""
        sd     = _make_source_data(rows=self._rows())
        result = prepare(sd, allowed_columns=["name"])
        assert result.profile.columns == ["name"]


# ---------------------------------------------------------------------------
# prepare() — row filtering
# ---------------------------------------------------------------------------

class TestRowFiltering:
    """Tests for required_filters enforcement in prepare()."""

    def _rows(self) -> list[dict]:
        return [
            {"status": "active",   "amount": 100},
            {"status": "inactive", "amount": 200},
            {"status": "active",   "amount": 300},
        ]

    def test_equality_filter(self) -> None:
        """== operator keeps only matching rows."""
        sd = _make_source_data(rows=self._rows())
        result = prepare(
            sd,
            required_filters=[{"column": "status", "operator": "==", "value": "active"}],
        )
        assert result.profile.rows_after_filter == 2

    def test_inequality_filter(self) -> None:
        """!= operator excludes matching rows."""
        sd = _make_source_data(rows=self._rows())
        result = prepare(
            sd,
            required_filters=[{"column": "status", "operator": "!=", "value": "active"}],
        )
        assert result.profile.rows_after_filter == 1

    def test_greater_than_filter(self) -> None:
        """> operator keeps rows where column value > threshold."""
        sd = _make_source_data(rows=self._rows())
        result = prepare(
            sd,
            required_filters=[{"column": "amount", "operator": ">", "value": 100}],
        )
        assert result.profile.rows_after_filter == 2

    def test_in_filter(self) -> None:
        """in operator keeps rows whose value is in the list."""
        sd = _make_source_data(rows=self._rows())
        result = prepare(
            sd,
            required_filters=[
                {"column": "status", "operator": "in", "value": ["active", "pending"]}
            ],
        )
        assert result.profile.rows_after_filter == 2

    def test_multiple_filters_are_ANDed(self) -> None:
        """All filters must pass — they are AND-combined."""
        sd = _make_source_data(rows=self._rows())
        result = prepare(
            sd,
            required_filters=[
                {"column": "status", "operator": "==",  "value": "active"},
                {"column": "amount", "operator": ">=",  "value": 300},
            ],
        )
        assert result.profile.rows_after_filter == 1


# ---------------------------------------------------------------------------
# prepare() — sampling
# ---------------------------------------------------------------------------

class TestSampling:
    """Tests for sampling strategies in prepare()."""

    def _rows(self, n: int = 20) -> list[dict]:
        return [{"id": i, "val": i * 10} for i in range(n)]

    def test_head_sampling(self) -> None:
        """head keeps the first N rows."""
        sd     = _make_source_data(rows=self._rows(20))
        result = prepare(sd, max_rows=5, sampling_method="head")
        assert result.profile.rows_sampled == 5
        assert "id" in result.rendered_text

    def test_tail_sampling(self) -> None:
        """tail keeps the last N rows."""
        sd     = _make_source_data(rows=self._rows(20))
        result = prepare(sd, max_rows=5, sampling_method="tail")
        assert result.profile.rows_sampled == 5
        # Last 5 rows have ids 15-19.
        assert "15" in result.rendered_text

    def test_random_sampling_with_seed_is_deterministic(self) -> None:
        """Same seed produces the same rows on repeated calls."""
        rows   = self._rows(100)
        sd     = _make_source_data(rows=rows)
        r1     = prepare(sd, max_rows=10, sampling_method="random", sampling_seed=7)
        r2     = prepare(sd, max_rows=10, sampling_method="random", sampling_seed=7)
        assert r1.rendered_text == r2.rendered_text

    def test_no_sampling_when_within_limit(self) -> None:
        """All rows are kept when count <= max_rows."""
        sd     = _make_source_data(rows=self._rows(5))
        result = prepare(sd, max_rows=10)
        assert result.profile.rows_sampled == 5

    def test_profile_records_sampling_method(self) -> None:
        """DataProfile.sampling_method records the strategy used."""
        sd     = _make_source_data(rows=self._rows(20))
        result = prepare(sd, max_rows=5, sampling_method="tail")
        assert result.profile.sampling_method == "tail"


# ---------------------------------------------------------------------------
# prepare() — redaction
# ---------------------------------------------------------------------------

class TestColumnRedaction:
    """Tests for column-level redaction in prepare()."""

    def _rows(self) -> list[dict]:
        return [
            {"name": "Alice", "ssn": "111-22-3333"},
            {"name": "Bob",   "ssn": "444-55-6666"},
        ]

    def test_redacted_column_value_replaced(self) -> None:
        """Column values matching a redaction rule are masked."""
        sd     = _make_source_data(rows=self._rows())
        result = prepare(
            sd,
            redaction_rules=[{"column": "ssn", "mask": "[SSN]"}],
        )
        assert "111-22-3333" not in result.rendered_text
        assert "[SSN]" in result.rendered_text

    def test_non_redacted_column_unchanged(self) -> None:
        """Columns not in redaction_rules are not modified."""
        sd     = _make_source_data(rows=self._rows())
        result = prepare(
            sd,
            redaction_rules=[{"column": "ssn", "mask": "[SSN]"}],
        )
        assert "Alice" in result.rendered_text
        assert "Bob"   in result.rendered_text

    def test_profile_records_redacted_columns(self) -> None:
        """DataProfile.columns_redacted lists masked column names."""
        sd     = _make_source_data(rows=self._rows())
        result = prepare(
            sd,
            redaction_rules=[{"column": "ssn", "mask": "[SSN]"}],
        )
        assert "ssn" in result.profile.columns_redacted


# ---------------------------------------------------------------------------
# prepare() — text source passthrough
# ---------------------------------------------------------------------------

class TestTextSourcePreparation:
    """Tests for TextDataSource handling in prepare()."""

    def test_text_passed_through_unchanged(self) -> None:
        """Text sources skip tabular stages and return text as-is."""
        sd     = _make_source_data(raw_text="Quarterly revenue was strong.")
        result = prepare(sd)
        assert result.rendered_text == "Quarterly revenue was strong."

    def test_text_source_profile_is_minimal(self) -> None:
        """DataProfile for text sources reflects zero tabular rows."""
        sd     = _make_source_data(raw_text="Some text.")
        result = prepare(sd)
        assert result.profile.rows_sampled == 0
        assert result.profile.columns      == []

    def test_text_source_input_hash_set(self) -> None:
        """PreparedInput.input_hash is populated for text sources."""
        sd     = _make_source_data(raw_text="Some text.")
        result = prepare(sd)
        assert len(result.input_hash) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# CSVDataSource
# ---------------------------------------------------------------------------

class TestCSVDataSource:
    """Tests for CSVDataSource.extract()."""

    def _write_csv(self, tmp_path: Path, rows: int) -> Path:
        p     = tmp_path / "data.csv"
        lines = ["col_a,col_b,col_c"] + [f"val_{i},{i},{i*2}" for i in range(rows)]
        p.write_text("\n".join(lines), encoding="utf-8")
        return p

    def test_all_rows_extracted_within_limit(self, tmp_path: Path) -> None:
        """All rows are returned when count <= max_extract."""
        path   = self._write_csv(tmp_path, 5)
        source = CSVDataSource(path)
        data   = source.extract(max_extract=100)
        assert data.row_count == 5
        assert not data.truncated

    def test_truncated_at_max_extract(self, tmp_path: Path) -> None:
        """Extraction stops at max_extract and sets truncated=True."""
        path   = self._write_csv(tmp_path, 50)
        source = CSVDataSource(path)
        data   = source.extract(max_extract=10)
        assert data.row_count == 10
        assert data.truncated

    def test_columns_populated(self, tmp_path: Path) -> None:
        """columns list matches the CSV header."""
        path   = self._write_csv(tmp_path, 3)
        source = CSVDataSource(path)
        data   = source.extract()
        assert data.columns == ["col_a", "col_b", "col_c"]

    def test_source_hash_deterministic(self, tmp_path: Path) -> None:
        """Same file produces the same source_hash on each extraction."""
        path   = self._write_csv(tmp_path, 5)
        source = CSVDataSource(path)
        h1     = source.extract().source_hash
        h2     = source.extract().source_hash
        assert h1 == h2


# ---------------------------------------------------------------------------
# TextDataSource
# ---------------------------------------------------------------------------

class TestTextDataSource:
    """Tests for TextDataSource.extract()."""

    def test_raw_text_populated(self) -> None:
        """raw_text contains the supplied string."""
        source = TextDataSource("hello world", ref="greeting")
        data   = source.extract()
        assert data.raw_text == "hello world"

    def test_rows_empty(self) -> None:
        """rows is always empty for a text source."""
        source = TextDataSource("text")
        data   = source.extract()
        assert data.rows == []

    def test_source_ref_set(self) -> None:
        """source_ref matches the ref parameter."""
        source = TextDataSource("text", ref="my-doc")
        data   = source.extract()
        assert data.source_ref == "my-doc"

    def test_not_truncated(self) -> None:
        """Text sources are never marked truncated."""
        source = TextDataSource("x" * 100_000)
        data   = source.extract(max_extract=5)
        assert not data.truncated


# ---------------------------------------------------------------------------
# Analyzer end-to-end
# ---------------------------------------------------------------------------

class TestAnalyzerEndToEnd:
    """End-to-end tests for Analyzer.analyze()."""

    def test_analyze_returns_analysis_result(self, tmp_path: Path) -> None:
        """analyze() returns an AnalysisResult with populated fields."""
        contract_path = _write_analysis_contract(
            tmp_path,
            extra_yaml=(
                "output_schema:\n  type: object\n  properties:\n    result: {type: string}\n"
            ),
        )
        provider  = _make_mock_provider('{"result": "ok"}')
        analyzer  = Analyzer(contract_path=contract_path, provider="mock", model="m")
        source    = TextDataSource("Sales data here.")

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(name="mock", model="m", provider=provider),
        ):
            result = analyzer.analyze(source, analysis_id="run-001")

        assert isinstance(result, AnalysisResult)
        assert result.status == STATUS_SUCCESS
        assert result.data   == {"result": "ok"}

    def test_nested_raw_output_retains_generated_artifact_text(self, tmp_path: Path) -> None:
        """output.format raw carries extracted model content in raw_text."""
        contract_path = _write_analysis_contract(
            tmp_path,
            extra_yaml=(
                "output:\n"
                "  format: raw\n"
                "  artifact_type: rey_loader_yaml\n"
            ),
        )
        generated = "data_sources:\n  - name: example\n    enabled: true"
        provider = _make_mock_provider(
            '{"artifact_type":"rey_loader_yaml","content":'
            '"data_sources:\\n  - name: example\\n    enabled: true\\n","notes":[]}'
        )
        analyzer = Analyzer(contract_path=contract_path, provider="mock", model="m")

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(name="mock", model="m", provider=provider),
        ):
            result = analyzer.analyze(TextDataSource("profile"), analysis_id="raw-001")

        assert result.status == STATUS_SUCCESS
        assert result.data is None
        assert result.raw_text == generated

    def test_prepared_metadata_in_result(self, tmp_path: Path) -> None:
        """AnalysisResult.prepared contains DataProfile from preparation."""
        contract_path = _write_analysis_contract(tmp_path)
        provider      = _make_mock_provider('{"result": "ok"}')
        analyzer      = Analyzer(contract_path=contract_path, provider="mock", model="m")
        source        = TextDataSource("Some text.")

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(name="mock", model="m", provider=provider),
        ):
            result = analyzer.analyze(source, analysis_id="run-002")

        assert result.prepared is not None
        assert result.prepared.source_ref == "text"

    def test_contract_spec_drives_column_filtering(self, tmp_path: Path) -> None:
        """allowed_columns from the contract spec restricts what the LLM sees."""
        contract_path = _write_analysis_contract(
            tmp_path,
            extra_yaml="allowed_columns:\n  - revenue\n",
        )
        provider = _make_mock_provider('{"result": "ok"}')
        analyzer = Analyzer(contract_path=contract_path, provider="mock", model="m")

        rows = [
            {"revenue": 1000, "customer_id": "C001"},
            {"revenue": 2000, "customer_id": "C002"},
        ]

        class _RowSource:
            def extract(self, max_extract: int = 10_000):  # noqa: ANN001, ANN201
                return _make_source_data(rows=rows, ref="test-rows")

        with patch(
            "rey_lib.llm.runner._resolve_provider_config",
            return_value=_ProviderConfig(name="mock", model="m", provider=provider),
        ):
            result = analyzer.analyze(_RowSource(), analysis_id="run-003")

        # The provider call must not have seen customer_id.
        call_args   = provider.run.call_args
        messages    = call_args[1]["messages"] if call_args[1] else call_args[0][0]
        all_content = " ".join(m.content for m in messages)
        assert "customer_id" not in all_content
        assert "C001"        not in all_content

    def test_analyzer_exposes_contract(self, tmp_path: Path) -> None:
        """Analyzer.contract returns the loaded AnalysisContract."""
        contract_path = _write_analysis_contract(tmp_path)
        analyzer      = Analyzer(contract_path=contract_path, provider="mock", model="m")
        assert isinstance(analyzer.contract, AnalysisContract)
        assert analyzer.contract.name == "test-analysis"

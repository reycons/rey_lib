"""Analysis contracts are read on demand from control.analysis_contract (backlog 903).

One runtime path: a consumer names a contract, the runtime's Control reads its
stored text and format through the get_analysis_contract binding, and
rey_lib.analysis parses it. No file is read and nothing falls back to one.

No database is opened: routine calls are recorded, not made.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.ai.errors import AIConfigurationError
from rey_lib.analysis import analysis_contract, parse_analysis_contract, parse_contract
from rey_lib.analysis.api import RunRequest
from rey_lib.analysis.execution import run
from rey_lib.control.control import Control
from rey_lib.encryption import sha256_text
from rey_lib.errors.error_utils import ConfigError

_MARKDOWN = (
    "---\n"
    "name: stored\n"
    "version: 1.0.0\n"
    "effective_date: 2026-10-12\n"
    "max_rows: 5\n"
    "---\n"
    "\n"
    "Analyse it.\n"
)
_YAML = (
    "name: stored_yaml\n"
    "version: 2\n"
    "effective_date: 2026-10-12\n"
    "output:\n"
    "  format: raw\n"
)


class _Control:
    """The runtime's Control, answering get_analysis_contract from a mapping."""

    installation_id = 7

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.asked: list[str] = []

    def analysis_contract(self, name: str) -> dict[str, Any] | None:
        self.asked.append(name)
        return self.rows.get(name)


def _row(name: str, text: str, fmt: str) -> dict[str, Any]:
    return {"name": name, "format": fmt, "contract_text": text}


# ---------------------------------------------------------------------------
# The one parser, from text
# ---------------------------------------------------------------------------

def test_a_markdown_contract_is_frontmatter_then_body() -> None:
    contract = parse_contract(_MARKDOWN, "markdown")

    assert (contract.name, contract.version) == ("stored", "1.0.0")
    assert contract.body == "Analyse it."
    assert contract.text == _MARKDOWN
    assert contract.hash == sha256_text(_MARKDOWN)
    assert contract.path is None


def test_a_yaml_contract_is_the_whole_document() -> None:
    contract = parse_contract(_YAML, "yaml")

    assert (contract.name, contract.version) == ("stored_yaml", "2")
    assert contract.body == _YAML
    assert contract.raw_frontmatter["output"] == {"format": "raw"}


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(ConfigError, match="format 'json'"):
        parse_contract(_YAML, "json")


def test_a_missing_required_field_is_refused() -> None:
    with pytest.raises(ConfigError, match="missing required field 'effective_date'"):
        parse_contract("name: x\nversion: 1\n", "yaml")


def test_the_analysis_spec_comes_from_the_text() -> None:
    contract = parse_analysis_contract(_MARKDOWN, "markdown")

    assert contract.spec.max_rows == 5
    assert contract.base.text == _MARKDOWN


# ---------------------------------------------------------------------------
# Control.analysis_contract: one binding, by name, the installation not sent
# ---------------------------------------------------------------------------

def _answering(rows: list[dict[str, Any]]) -> tuple[Control, list[tuple[str, dict[str, Any], bool]]]:
    calls: list[tuple[str, dict[str, Any], bool]] = []
    control = Control.__new__(Control)

    def _call_rows(action: str, variables: dict[str, Any],
                   required: bool = False) -> list[dict[str, Any]]:
        calls.append((action, dict(variables), required))
        return [dict(row) for row in rows]

    control._call_rows = _call_rows  # type: ignore[method-assign]
    return control, calls


def test_control_reads_one_contract_by_name_and_requires_the_binding() -> None:
    control, calls = _answering([_row("stored", _MARKDOWN, "markdown")])

    row = control.analysis_contract("stored")

    assert row == _row("stored", _MARKDOWN, "markdown")
    # The installation is not sent -- the binding reads it off Control -- and
    # the lookup is required: a missing binding is a configuration fault.
    assert calls == [("get_analysis_contract", {"name": "stored"}, True)]


def test_control_answers_none_when_no_contract_is_stored() -> None:
    control, _ = _answering([])

    assert control.analysis_contract("absent") is None


# ---------------------------------------------------------------------------
# The seam: rey_lib.analysis.analysis_contract(control, name)
# ---------------------------------------------------------------------------

def test_the_seam_parses_the_stored_text() -> None:
    control = _Control({"stored": _row("stored", _MARKDOWN, "markdown")})

    contract = analysis_contract(control, "stored")

    assert contract.name == "stored"
    assert contract.spec.max_rows == 5
    assert control.asked == ["stored"]


def test_the_seam_refuses_a_runtime_without_control() -> None:
    with pytest.raises(ConfigError, match="has none"):
        analysis_contract(None, "stored")


def test_the_seam_refuses_an_unknown_name() -> None:
    with pytest.raises(ConfigError, match="no analysis contract named 'absent'"):
        analysis_contract(_Control({}), "absent")


def test_the_seam_refuses_a_contract_selected_by_no_name() -> None:
    with pytest.raises(ConfigError, match="by its name"):
        analysis_contract(_Control({}), "  ")


def test_the_seam_fails_closed_when_the_text_names_another_contract() -> None:
    control = _Control({"stored": _row("stored", _YAML, "yaml")})

    with pytest.raises(ConfigError, match="declares the name 'stored_yaml'"):
        analysis_contract(control, "stored")


# ---------------------------------------------------------------------------
# Execution carries the parsed contract; there is no file to fall back to
# ---------------------------------------------------------------------------

def test_a_stage_with_no_contract_and_no_text_is_refused() -> None:
    request = RunRequest(pipeline_id="p", stage_id="s", input_data="x")

    with pytest.raises(AIConfigurationError, match="carries no contract"):
        run(request, ai=object())

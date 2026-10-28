"""A registration's canonical-object capabilities, as they are read (#1196 D1).

Only a well-formed declaration can become governed or be invoked, so each one is
checked once, when the registration is read: a dotted canonical key published
once, target kinds, a description, an input contract that is JSON Schema 2020-12
(no sample), and access read | write.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config.applications import ApplicationCapability, build_applications
from rey_lib.errors.error_utils import ConfigError

_CONTRACT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"max_bytes": {"type": "integer", "minimum": 1}},
    "required": ["max_bytes"],
    "additionalProperties": False,
}


def _capability(**overrides: Any) -> dict[str, Any]:
    return {"key": "file.text", "description": "Bounded display-safe text.",
            "target_kinds": ["configured_file"], "input_contract": _CONTRACT,
            "access": "read", **overrides}


def _built(*capabilities: dict[str, Any]) -> Any:
    registrations = {"data_engine": {"name": "data_engine",
                                       "capabilities": list(capabilities)}}
    ctx = SimpleNamespace(apps=[{"name": "data_engine", "app_path": "/apps/data_engine"}])
    return build_applications(ctx, registrations=registrations)[0]


def test_capabilities_are_empty_until_published() -> None:
    assert _built().capabilities == ()


def test_a_published_capability_is_carried_as_declared() -> None:
    application = _built(_capability())

    assert application.capabilities == (ApplicationCapability(
        key="file.text", description="Bounded display-safe text.",
        target_kinds=("configured_file",), input_contract=_CONTRACT, access="read",
    ),)
    # Beside the workflow operations, never inside them.
    assert application.workflow_operations == ()


@pytest.mark.parametrize("overrides,refusal", [
    ({"key": "text"}, "dotted canonical name"),
    ({"key": "File.Text"}, "dotted canonical name"),
    ({"target_kinds": []}, "no target_kinds"),
    ({"target_kinds": "configured_file"}, "no target_kinds"),
    ({"description": "  "}, "no description"),
    ({"input_contract": None}, "no contract_definition object"),
    ({"input_contract": {"type": "nonsense"}}, "not valid JSON Schema"),
    ({"access": "execute"}, "access is read or write"),
    ({"access": ""}, "access is read or write"),
])
def test_a_malformed_declaration_is_refused_when_the_registration_is_read(
    overrides: dict[str, Any], refusal: str,
) -> None:
    with pytest.raises(ConfigError, match=refusal):
        _built(_capability(**overrides))


def test_a_key_published_twice_is_refused() -> None:
    with pytest.raises(ConfigError, match="published twice"):
        _built(_capability(), _capability(description="Another."))

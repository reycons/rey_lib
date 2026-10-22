"""A workflow operation's published settings contract (backlog 939).

Each application publishes, for every operation a workflow may invoke, two JSON
objects in its registration:

``contract_definition``
    A JSON Schema (2020-12) stating the settings the operation reads: structure,
    nesting, arrays, required fields, types, defaults, allowed values, keyed
    maps, variants and descriptions. It is authoritative for constructing and
    validating the operation's workflow settings, by the front end and by an
    LLM, and it holds no workflow values.

``contract_sample``
    The starter settings a new settings instance copies, conforming to the
    definition.

This module is the one place that knows the contract is JSON Schema. A
registration is checked when it is read -- a malformed definition or a sample
that does not satisfy it is refused at load -- and the workflow coordinator asks
it what a step's effective settings violate before anything is dispatched.

Public API
----------
check_contract       Refuse a definition that is not valid JSON Schema 2020-12,
                     or a sample that does not satisfy it.
check_definition     The definition half alone, for a contract with no sample.
contract_violations  What settings violate a definition, one message per error.
contract_review      The same, split into unsupported keys and violations.

Unsupported settings -- keys a contract does not define -- are not part of the
canonical contract and are never generated from it. Existing configuration may
still carry them, so a caller validating live settings can tolerate them
(contract_review) while refusing every other violation (user, 2026-10-13,
backlog 939).
"""

from __future__ import annotations

from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from rey_lib.errors.error_utils import ConfigError

__all__ = ["check_contract", "check_definition", "contract_review", "contract_violations"]

#: The dialect every contract is written in.
_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def check_contract(definition: Any, sample: Any, label: str) -> None:
    """Refuse a contract whose definition or sample is unusable.

    Args:
        definition: The published contract_definition.
        sample: The published contract_sample.
        label: Names the operation in a refusal, e.g. ``rey_loader.sql_operation``.

    Raises:
        ConfigError: The definition or sample is not a JSON object, the
            definition names another dialect or is not valid JSON Schema
            2020-12, or the sample does not satisfy the definition.
    """
    check_definition(definition, label)
    if not isinstance(sample, Mapping):
        raise ConfigError(f"Operation '{label}' publishes no contract_sample object.")
    violations = contract_violations(definition, sample)
    if violations:
        raise ConfigError(
            f"Operation '{label}' contract_sample does not satisfy its definition: "
            + "; ".join(violations)
        )


def check_definition(definition: Any, label: str) -> None:
    """Refuse a definition that is not a JSON Schema 2020-12 object.

    The half of :func:`check_contract` that needs no sample: a canonical-object
    capability's input contract publishes a definition alone (#1196 D1).

    Args:
        definition: The published definition.
        label: Names what publishes it in a refusal, e.g. ``file_operator.file.text``.

    Raises:
        ConfigError: The definition is not a JSON object, names another
            dialect, or is not valid JSON Schema 2020-12.
    """
    if not isinstance(definition, Mapping):
        raise ConfigError(f"Operation '{label}' publishes no contract_definition object.")
    dialect = definition.get("$schema", _DIALECT)
    if dialect != _DIALECT:
        raise ConfigError(
            f"Operation '{label}' contract_definition declares $schema {dialect!r}; "
            f"contracts are JSON Schema 2020-12 ({_DIALECT})."
        )
    try:
        Draft202012Validator.check_schema(definition)
    except SchemaError as exc:
        raise ConfigError(
            f"Operation '{label}' contract_definition is not valid JSON Schema: {exc.message}"
        ) from exc


def contract_violations(definition: Mapping[str, Any], settings: Any) -> list[str]:
    """Every way the settings violate the definition, in path order.

    Args:
        definition: A contract_definition already accepted by :func:`check_contract`.
        settings: The settings to validate, as plain JSON-compatible data.

    Returns:
        One message per violation, each prefixed with the path of the value
        it concerns (``(settings)`` for the root). Empty when the settings
        satisfy the definition.
    """
    errors = sorted(
        Draft202012Validator(definition).iter_errors(settings),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    return [f"{_path(error.absolute_path)}: {error.message}" for error in errors]


def contract_review(
    definition: Mapping[str, Any],
    settings: Any,
) -> tuple[list[str], list[str]]:
    """Split what settings break into unsupported keys and real violations.

    An unsupported key is one the definition does not allow at that position
    (a JSON Schema additionalProperties error): the setting is not part of the
    contract, and nothing reads it. Everything else -- a missing required
    setting, a wrong type, a value outside the allowed set, settings matching
    no variant -- is a violation.

    Args:
        definition: A contract_definition already accepted by :func:`check_contract`.
        settings: The settings to review, as plain JSON-compatible data.

    Returns:
        ``(unsupported, violations)``, each a list of messages prefixed with the
        path of the value it concerns, in path order.
    """
    unsupported: list[str] = []
    violations: list[str] = []
    errors = sorted(
        Draft202012Validator(definition).iter_errors(settings),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    for error in errors:
        message = f"{_path(error.absolute_path)}: {error.message}"
        (unsupported if error.validator == "additionalProperties" else violations).append(message)
    return unsupported, violations


def _path(parts: Any) -> str:
    """A violation's location, written the way configuration is read."""
    rendered = ""
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else (f".{part}" if rendered else str(part))
    return rendered or "(settings)"

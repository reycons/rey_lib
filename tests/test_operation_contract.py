"""The operation contract check and review (backlog 939).

A workflow operation publishes a JSON Schema 2020-12 contract_definition and a
contract_sample conforming to it. The check refuses an unusable contract when a
registration loads; the review splits what live settings break into unsupported
keys (tolerated) and violations (refused).
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.config.applications import ApplicationOperation, _operations
from rey_lib.config.operation_contract import check_contract, contract_review, contract_violations
from rey_lib.errors.error_utils import ConfigError

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _definition(**extra: Any) -> dict[str, Any]:
    return {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {"source": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}},
        "required": ["source"],
        "additionalProperties": False,
        **extra,
    }


class TestCheckContract:
    def test_a_valid_contract_is_accepted(self) -> None:
        check_contract(_definition(), {"source": ""}, "app.op")

    @pytest.mark.parametrize(("definition", "sample", "message"), [
        (None, {}, "no contract_definition object"),
        (_definition(), None, "no contract_sample object"),
        ({**_definition(), "$schema": "http://json-schema.org/draft-07/schema#"}, {"source": ""}, "JSON Schema 2020-12"),
        ({**_definition(), "type": "objekt"}, {"source": ""}, "not valid JSON Schema"),
        (_definition(), {}, "contract_sample does not satisfy"),
        (_definition(), {"source": "", "typo": 1}, "contract_sample does not satisfy"),
    ], ids=["no_definition", "no_sample", "other_dialect", "invalid_schema", "sample_missing_required", "sample_unsupported_key"])
    def test_an_unusable_contract_is_refused(self, definition: Any, sample: Any, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            check_contract(definition, sample, "app.op")


class TestReview:
    def test_settings_that_satisfy_the_contract_have_nothing_to_say(self) -> None:
        assert contract_review(_definition(), {"source": "x", "limit": 2}) == ([], [])

    def test_unsupported_keys_and_violations_are_told_apart(self) -> None:
        unsupported, violations = contract_review(_definition(), {"limit": 0, "typo": 1})
        assert unsupported == ["(settings): Additional properties are not allowed ('typo' was unexpected)"]
        assert violations == [
            "(settings): 'source' is a required property",
            "limit: 0 is less than the minimum of 1",
        ]

    def test_a_violation_is_named_by_its_path(self) -> None:
        definition = {
            "$schema": _SCHEMA, "type": "object",
            "properties": {"sources": {"type": "array", "items": {
                "type": "object", "properties": {"name": {"type": "string"}}}}},
        }
        assert contract_violations(definition, {"sources": [{"name": 1}]}) == [
            "sources[0].name: 1 is not of type 'string'",
        ]


class TestTheRegistrationIsReadWithItsContract:
    def test_an_operation_carries_its_contract(self) -> None:
        (built,) = _operations({"workflow_operations": [{
            "name": "op", "description": "d",
            "contract_definition": _definition(), "contract_sample": {"source": ""},
        }]}, "app")
        assert built == ApplicationOperation(
            name="op", description="d",
            contract_definition=_definition(), contract_sample={"source": ""},
        )

    def test_the_retired_parameters_list_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="publishes a parameters list"):
            _operations({"workflow_operations": [{
                "name": "op", "parameters": [],
                "contract_definition": _definition(), "contract_sample": {"source": ""},
            }]}, "app")

    def test_an_operation_without_a_contract_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="no contract_definition object"):
            _operations({"workflow_operations": [{"name": "op"}]}, "app")

"""HostedCapabilities: invoke against the object the application already holds (#1196 D1).

A key is invocable only where the registration, the governed declaration and the
owner's catalogue all have it and the first two agree on every declared field;
each disagreement disables that key alone. invoke then refuses, in order: a key
not invocable, inputs violating the contract, a hosted kind outside the target
kinds, access other than read -- and otherwise calls the catalogue.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any

import pytest

from rey_lib.config.applications import Application, ApplicationCapability
from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.run.capabilities import HostedCapabilities

_CONTRACT = {
    "type": "object",
    "properties": {"max_bytes": {"type": "integer", "minimum": 1}},
    "required": ["max_bytes"],
    "additionalProperties": False,
}
_NO_INPUTS = {"type": "object", "additionalProperties": False}


def _declared(key: str, contract: dict[str, Any], access: str = "read") -> ApplicationCapability:
    return ApplicationCapability(key=key, description=f"The {key} capability.",
                                 target_kinds=("configured_file", "file_manifest"),
                                 input_contract=contract, access=access)


def _row(capability: ApplicationCapability, **overrides: Any) -> dict[str, Any]:
    return {"capability_key": capability.key, "description": capability.description,
            "target_kinds": list(capability.target_kinds),
            "input_contract": dict(capability.input_contract),
            "access": capability.access, **overrides}


class _Control:
    """The governed read boundary, answering rows or failing."""

    def __init__(self, rows: list[dict[str, Any]] | None) -> None:
        self.rows = rows
        self.asked: list[str] = []

    def get_application_capabilities(self, application: str) -> list[dict[str, Any]]:
        self.asked.append(application)
        if self.rows is None:
            raise ConfigError("no binding for get_application_capabilities")
        return self.rows


_TEXT = _declared("file.text", _CONTRACT)
_FACTS = _declared("file.facts", _NO_INPUTS)


def _hosted(published: tuple[ApplicationCapability, ...] = (_TEXT, _FACTS),
            rows: list[dict[str, Any]] | None = None,
            catalogue: dict[str, Any] | None = None,
            kind: str = "configured_file") -> tuple[HostedCapabilities, _Control]:
    control = _Control([_row(one) for one in published] if rows is None else rows)
    application = Application(name="file_operator", capabilities=published)
    held = {"text": "hello"}
    if catalogue is None:
        catalogue = {
            "file.text": lambda inputs: held["text"][: inputs["max_bytes"]],
            "file.facts": lambda inputs: {"file_type": "text"},
        }
    return HostedCapabilities(application, control, catalogue, kind), control


def test_an_agreed_capability_calls_the_catalogue_over_the_held_object() -> None:
    hosted, control = _hosted()

    assert hosted.invoke("file.text", {"max_bytes": 4}) == "hell"
    assert hosted.invoke("file.facts", {}) == {"file_type": "text"}
    # The governed read asks by the application's own name, once.
    assert control.asked == ["file_operator"]


@pytest.mark.parametrize("field,value", [
    ("description", "Delete the file."),
    ("target_kinds", ["run_log_file"]),
    ("input_contract", _NO_INPUTS),
    ("access", "write"),
])
def test_a_disagreement_disables_that_key_alone(field: str, value: Any) -> None:
    hosted, _ = _hosted(rows=[_row(_TEXT, **{field: value}), _row(_FACTS)])

    with pytest.raises(AppError, match=f"disagree on {field}"):
        hosted.invoke("file.text", {"max_bytes": 4})
    assert hosted.invoke("file.facts", {}) == {"file_type": "text"}


def test_target_kinds_agree_whatever_order_they_are_listed_in() -> None:
    reordered = _row(_TEXT, target_kinds=["file_manifest", "configured_file"])
    hosted, _ = _hosted(rows=[reordered, _row(_FACTS)])

    assert hosted.invoke("file.text", {"max_bytes": 2}) == "he"


def test_a_key_missing_from_any_source_is_not_invocable() -> None:
    ungoverned, _ = _hosted(rows=[_row(_FACTS)])
    unimplemented, _ = _hosted(catalogue={"file.facts": lambda inputs: {}})
    unpublished, _ = _hosted(published=(_FACTS,), rows=[_row(_TEXT), _row(_FACTS)])
    catalogue_only, _ = _hosted(published=(_FACTS,))

    with pytest.raises(AppError, match="not governed for this application"):
        ungoverned.invoke("file.text", {"max_bytes": 1})
    with pytest.raises(AppError, match="catalogue does not implement it"):
        unimplemented.invoke("file.text", {"max_bytes": 1})
    with pytest.raises(AppError, match="registration does not publish it"):
        unpublished.invoke("file.text", {"max_bytes": 1})
    # A catalogue entry alone is not authority.
    with pytest.raises(AppError, match="registration does not publish it"):
        catalogue_only.invoke("file.text", {"max_bytes": 1})


def test_an_unknown_key_is_refused_by_name() -> None:
    hosted, _ = _hosted()

    with pytest.raises(AppError, match="'file.delete' is not invocable"):
        hosted.invoke("file.delete", {})


def test_a_failed_governed_read_leaves_no_key_invocable() -> None:
    application = Application(name="file_operator", capabilities=(_TEXT, _FACTS))
    catalogue = {"file.facts": lambda inputs: {}}
    # Construction does not fail: the application still hosts its object.
    hosted = HostedCapabilities(application, _Control(None), catalogue, "configured_file")

    with pytest.raises(AppError, match="could not be read"):
        hosted.invoke("file.facts", {})


def test_inputs_violating_the_contract_are_refused_before_the_catalogue() -> None:
    called: list[Any] = []
    hosted, _ = _hosted(catalogue={"file.text": called.append, "file.facts": called.append})

    with pytest.raises(AppError, match="refuses its inputs"):
        hosted.invoke("file.text", {"max_bytes": 0})
    with pytest.raises(AppError, match="refuses its inputs"):
        hosted.invoke("file.facts", {"path": "/etc/passwd"})
    assert called == []


def test_a_hosted_kind_outside_the_target_kinds_is_refused() -> None:
    hosted, _ = _hosted(kind="database_object")

    with pytest.raises(AppError, match="does not apply to the hosted kind 'database_object'"):
        hosted.invoke("file.facts", {})


def test_a_write_capability_is_refused_in_d1() -> None:
    write = _declared("file.write_text", _NO_INPUTS, access="write")
    called: list[Any] = []
    hosted, _ = _hosted(published=(write,), catalogue={"file.write_text": called.append})

    with pytest.raises(AppError, match="only read capabilities are answered"):
        hosted.invoke("file.write_text", {})
    assert called == []


def test_its_interface_carries_no_transport_identity_or_caller() -> None:
    """The host's own check is the one addition (#1141 S3): a callable, not an identity."""
    construction = set(inspect.signature(HostedCapabilities.__init__).parameters)
    invocation = set(inspect.signature(HostedCapabilities.invoke).parameters)

    assert construction == {"self", "application", "control", "catalogue", "hosted_kind",
                            "identified_by"}
    assert invocation == {"self", "capability", "inputs"}


# -- a delivered capability request (#1141 S3) ------------------------------------


def _answering(identified_by: Any, called: list[str] | None = None) -> HostedCapabilities:
    """file_operator's capabilities with a catalogue that records each call."""
    calls = called if called is not None else []
    control = _Control([_row(one) for one in (_TEXT, _FACTS)])
    application = Application(name="file_operator", capabilities=(_TEXT, _FACTS))
    catalogue = {
        "file.text": lambda inputs: calls.append("file.text") or "hello"[: inputs["max_bytes"]],
        "file.facts": lambda inputs: calls.append("file.facts") or {"file_type": "text"},
    }
    return HostedCapabilities(application, control, catalogue, "configured_file", identified_by)


def test_applicable_asks_invokes_authorization_and_calls_nothing() -> None:
    called: list[str] = []
    capabilities = _answering(lambda subject: True, called)

    assert capabilities.applicable(
        ["file.facts", "file.unknown", "file.text", "file.facts", "database.query"]
    ) == ["file.facts", "file.text"]
    assert called == []


def test_applicable_refuses_what_invoke_would_refuse_before_reading_inputs() -> None:
    write = _declared("file.write", _NO_INPUTS, access="write")
    foreign = replace(_declared("file.other", _NO_INPUTS), target_kinds=("workflow",))
    capabilities, _ = _hosted(published=(_TEXT, write, foreign), catalogue={
        "file.text": lambda inputs: "x", "file.write": lambda inputs: "x",
        "file.other": lambda inputs: "x"})

    assert capabilities.applicable(["file.text", "file.write", "file.other"]) == ["file.text"]


def test_the_subject_goes_unread_to_the_hosts_check_and_nowhere_else() -> None:
    seen: list[Any] = []
    reference = {"file_manifest_id": 42, "anything": ["the host's vocabulary"]}
    capabilities = _answering(lambda subject: seen.append(subject) or True)

    assert capabilities.answer({"subject": reference, "capability": "file.facts",
                                "inputs": {}}) == {"file_type": "text"}
    assert seen == [reference] and seen[0] is reference


def test_a_subject_the_host_does_not_hold_is_refused_before_anything_runs() -> None:
    called: list[str] = []
    capabilities = _answering(lambda subject: False, called)

    for request in ({"subject": {"x": 1}, "capability": "file.facts", "inputs": {}},
                    {"subject": {"x": 1}, "applicable": ["file.facts"]}):
        with pytest.raises(AppError, match="does not hold the subject"):
            capabilities.answer(request)
    assert called == []


def test_a_host_with_no_check_answers_no_subject_request() -> None:
    capabilities, _ = _hosted()

    with pytest.raises(AppError, match="does not hold the subject"):
        capabilities.answer({"subject": {}, "applicable": ["file.text"]})


def test_an_answer_is_applicable_or_invoke_and_invoke_still_checks_its_inputs() -> None:
    capabilities = _answering(lambda subject: True)

    assert capabilities.answer({"subject": {}, "applicable": ["file.text", "x"]}) == ["file.text"]
    assert capabilities.answer({"subject": {}, "capability": "file.text",
                                "inputs": {"max_bytes": 3}}) == "hel"
    with pytest.raises(AppError, match="refuses its inputs"):
        capabilities.answer({"subject": {}, "capability": "file.text", "inputs": {}})
    with pytest.raises(AppError, match="names a capability or asks which apply"):
        capabilities.answer({"subject": {}})

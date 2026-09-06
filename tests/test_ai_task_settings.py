"""Task-aware AI settings, exercised.

Settings answer one question per purpose: which profile, which instruction, what
temperature, which representation. A task states overrides; the defaults apply
where it states none. Written against the public surface, so a later
decomposition that keeps the contract does not rewrite these.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.ai import (
    AI,
    AIInstruction,
    AIProfile,
    AIRegistry,
    AIRequest,
    AISettings,
    EchoProvider,
)
from rey_lib.ai.construction import settings_from_ctx
from rey_lib.ai.errors import AIConfigurationError, AISelectionError
from rey_lib.ai.instructions import AIInstructionKind
from rey_lib.ai.settings import AISettingsTask

PROFILES = (
    AIProfile(id="dflt", provider="echo", model="m1",
              profile_access={"allowed": ["redacted", "unredacted"],
                              "default": "redacted"}),
    AIProfile(id="other", provider="echo", model="m2",
              profile_access={"allowed": ["redacted"], "default": "redacted"}),
)
INSTRUCTIONS = (
    AIInstruction(id="one", kind=AIInstructionKind.RAW, text="a"),
    AIInstruction(id="two", kind=AIInstructionKind.RAW, text="b"),
)


def runtime(settings: AISettings) -> AI:
    """One runtime over two profiles and two instructions."""
    registry = AIRegistry(
        profiles=PROFILES,
        instructions=INSTRUCTIONS,
        providers=(EchoProvider(name="echo"),),
    )
    return AI(registry=registry, settings=settings)


def declared(**ai_settings: Any) -> SimpleNamespace:
    """A context declaring an ``ai_settings`` block and nothing else."""
    return SimpleNamespace(ai_settings=SimpleNamespace(**ai_settings))


def read(ctx: Any) -> AISettings:
    return settings_from_ctx(ctx, profiles=PROFILES, instructions=INSTRUCTIONS)


# -- construction -----------------------------------------------------------

def test_absent_configuration_is_the_current_behaviour_not_a_failure() -> None:
    """A runtime configuring no settings gets the defaults, as it always did."""
    assert read(SimpleNamespace()) == AISettings()


def test_configured_defaults_and_tasks_are_read() -> None:
    settings = read(declared(
        default={"profile": "dflt", "instruction": "one",
                 "temperature": 0, "representation": "redacted"},
        tasks=[{"name": "t", "profile": "other"}],
    ))

    assert settings.profile_id == "dflt"
    assert settings.instruction_id == "one"
    assert settings.temperature == 0
    assert settings.representation == "redacted"
    assert settings.task("t").profile_id == "other"


def test_a_duplicate_task_name_is_refused() -> None:
    """Which of two entries applied would otherwise depend on order."""
    with pytest.raises(AIConfigurationError, match="twice"):
        read(declared(default={}, tasks=[{"name": "t"}, {"name": "t"}]))


def test_a_task_named_default_is_refused() -> None:
    """The word addresses the defaults beside it, so it cannot name a task."""
    with pytest.raises(AIConfigurationError, match="addresses the defaults"):
        read(declared(default={}, tasks=[{"name": "default"}]))


def test_an_unnamed_task_is_refused() -> None:
    with pytest.raises(AIConfigurationError):
        read(declared(default={}, tasks=[{"profile": "dflt"}]))


@pytest.mark.parametrize("block,message", [
    ({"default": {"profile": "ghost"}, "tasks": []}, "profile"),
    ({"default": {"instruction": "ghost"}, "tasks": []}, "instruction"),
    ({"default": {}, "tasks": [{"name": "t", "profile": "ghost"}]}, "profile"),
    ({"default": {}, "tasks": [{"name": "t", "instruction": "ghost"}]}, "instruction"),
])
def test_a_selection_this_runtime_does_not_offer_fails_construction(
    block: dict[str, Any], message: str,
) -> None:
    """Both levels. Dropping a bad entry would hide a configuration defect."""
    with pytest.raises(AIConfigurationError, match=message):
        read(declared(**block))


# -- resolution -------------------------------------------------------------

def test_a_request_naming_no_task_resolves_the_defaults() -> None:
    """Every existing caller keeps working, unedited."""
    ai = runtime(AISettings(profile_id="dflt", instruction_id="one", temperature=0.2))
    resolved = ai.resolve(AIRequest.prompt("x"))

    assert resolved.profile.id == "dflt"
    assert resolved.instruction.id == "one"
    assert resolved.options.temperature == 0.2


def test_an_unknown_task_inherits_the_defaults() -> None:
    """tasks[] is an override collection, not the set of valid purposes."""
    ai = runtime(AISettings(profile_id="dflt", instruction_id="one"))
    resolved = ai.resolve(AIRequest.prompt("x", task="never_configured"))

    assert resolved.profile.id == "dflt"
    assert resolved.instruction.id == "one"


def test_a_task_overriding_one_field_inherits_the_rest() -> None:
    ai = runtime(AISettings(
        profile_id="dflt", instruction_id="one", temperature=0.1,
        tasks=(AISettingsTask("t", profile_id="other"),),
    ))
    resolved = ai.resolve(AIRequest.prompt("x", task="t"))

    assert resolved.profile.id == "other"
    assert resolved.instruction.id == "one"
    assert resolved.options.temperature == 0.1


def test_a_task_overriding_only_the_instruction_keeps_the_default_profile() -> None:
    ai = runtime(AISettings(
        profile_id="dflt", instruction_id="one",
        tasks=(AISettingsTask("t", instruction_id="two"),),
    ))
    resolved = ai.resolve(AIRequest.prompt("x", task="t"))

    assert resolved.profile.id == "dflt"
    assert resolved.instruction.id == "two"


def test_an_explicit_request_override_beats_the_task() -> None:
    """A governed operation never silently depends on an operator's selection."""
    ai = runtime(AISettings(
        profile_id="dflt", tasks=(AISettingsTask("t", profile_id="other"),),
    ))
    resolved = ai.resolve(AIRequest.prompt("x", task="t", profile_id="dflt"))

    assert resolved.profile.id == "dflt"


def test_a_configured_zero_temperature_reaches_the_provider_as_zero() -> None:
    """Zero is the value this estate configures; losing it as "unset" changes
    what a model does."""
    ai = runtime(AISettings(
        profile_id="dflt", temperature=0.9,
        tasks=(AISettingsTask("t", temperature=0.0),),
    ))

    assert ai.resolve(AIRequest.prompt("x", task="t")).options.temperature == 0.0


# -- what changes, and what does not ----------------------------------------

def test_changing_a_default_changes_what_an_inheriting_task_resolves_to() -> None:
    ai = runtime(AISettings(profile_id="dflt", tasks=(AISettingsTask("t"),)))
    ai.select_profile("other")

    assert ai.resolve(AIRequest.prompt("x", task="t")).profile.id == "other"


def test_an_explicit_task_override_survives_a_default_change() -> None:
    ai = runtime(AISettings(
        profile_id="dflt", tasks=(AISettingsTask("t", profile_id="other"),),
    ))
    ai.select_profile("dflt")

    assert ai.resolve(AIRequest.prompt("x", task="t")).profile.id == "other"


def test_a_resolved_request_does_not_change_when_settings_change_afterwards() -> None:
    """The snapshot is what executes. A later change reaches the next run."""
    ai = runtime(AISettings(profile_id="dflt", tasks=(AISettingsTask("t"),)))
    resolved = ai.resolve(AIRequest.prompt("x", task="t"))

    ai.update_settings(ai.settings.with_task(AISettingsTask("t", profile_id="other")))

    assert resolved.profile.id == "dflt"


def test_a_task_override_naming_something_absent_is_refused() -> None:
    """Accepting it would configure a task into a state that fails only when
    that task next runs."""
    ai = runtime(AISettings(profile_id="dflt"))

    with pytest.raises(AISelectionError, match="for task 't'"):
        ai.update_settings(ai.settings.with_task(AISettingsTask("t", profile_id="ghost")))


# -- representation, which is a request and never an authorization ----------

def test_a_task_may_choose_which_representation_is_requested() -> None:
    ai = runtime(AISettings(
        profile_id="dflt", representation="unredacted",
        tasks=(AISettingsTask("t", representation="redacted"),),
    ))

    assert ai.permitted_access(task="t") == "redacted"
    assert ai.permitted_access() == "unredacted"


def test_settings_cannot_widen_what_a_profile_is_authorised_to_receive() -> None:
    """The envelope is the profile's, and a setting only asks within it."""
    ai = runtime(AISettings(
        profile_id="dflt", representation="unredacted",
        tasks=(AISettingsTask("narrow", profile_id="other"),),
    ))

    with pytest.raises(AISelectionError, match="does not allow"):
        ai.permitted_access(task="narrow")


# -- declared instruction contracts -----------------------------------------
#
# Configuration is the authority over what may be selected. Each entry names a
# file; what a reader sees is the contract's own name and version; and the
# declared id is what a setting resolves through, so it is config's identity
# rather than the filename's.

from rey_lib.config.bootstrap import _ai_instructions  # noqa: E402
from rey_lib.errors.error_utils import ConfigError  # noqa: E402


class _AIControl:
    """The Control an AI reads its configuration through, in memory.

    The estate reads the control database through Control, so a test of what the
    runtime offers supplies one rather than a configuration section. What it
    answers is the shape ``control.f_ai_instruction_get`` returns.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def ai_instructions(self, required: bool = True) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


def contract_row(key: str, name: Any, version: Any, body: str = "rules: []") -> dict:
    """One contract instruction, as the routine returns it."""
    return {"instruction_key": key, "kind": "contract",
            "contract_key": name, "contract_version": version, "body": body}


RESERVED = [
    {"instruction_key": "__none__", "kind": "none",
     "contract_key": None, "contract_version": None, "body": None},
    {"instruction_key": "__ad_hoc__", "kind": "raw",
     "contract_key": None, "contract_version": None, "body": None},
]


def instructions(*rows: dict, reserved: bool = True) -> SimpleNamespace:
    """A context whose Control answers with these instructions."""
    return SimpleNamespace(
        shared_control=_AIControl((RESERVED if reserved else []) + list(rows)))


def offered(ctx: Any) -> dict[str, str]:
    """Every CONTRACT instruction, as id -> what a reader sees."""
    return {
        i.id: i.name for i in _ai_instructions(ctx)
        if i.kind is AIInstructionKind.CONTRACT
    }


def test_a_runtime_declaring_none_offers_the_two_canonical_choices() -> None:
    """No contracts is an ordinary state, not a failure."""
    built = _ai_instructions(instructions())

    assert [i.kind for i in built] == [AIInstructionKind.NONE, AIInstructionKind.RAW]


def test_a_declared_entry_is_shown_by_the_contract_s_own_name_and_version() -> None:
    """Not the entry's key: what the prompt calls itself."""
    ctx = instructions(contract_row("anything", "rey_log_interpreter", "1.3.1"))

    assert offered(ctx) == {"anything": "rey_log_interpreter 1.3.1"}


def test_contracts_sharing_a_name_stay_distinct_by_version() -> None:
    """Four rows declare rey_log_interpreter in this estate; a name alone does
    not identify one."""
    ctx = instructions(
        contract_row("current", "rey_log_interpreter", "1.3.1"),
        contract_row("previous", "rey_log_interpreter", "1.2.1"),
    )

    assert offered(ctx) == {
        "current": "rey_log_interpreter 1.3.1",
        "previous": "rey_log_interpreter 1.2.1",
    }


def test_the_key_is_stable_so_the_contract_behind_it_may_change() -> None:
    """The property the identity distinction exists to give: repointing an
    instruction at another contract leaves every setting referencing it
    working."""
    before = instructions(contract_row("log_interpreter", "rey", "1.0.0"))
    after = instructions(contract_row("log_interpreter", "rey", "2.0.0"))

    assert set(offered(before)) == set(offered(after)) == {"log_interpreter"}
    assert offered(after)["log_interpreter"] == "rey 2.0.0"


def test_two_instructions_shown_identically_are_refused() -> None:
    """Distinct keys, but a reader sees one label twice and cannot choose."""
    ctx = instructions(
        contract_row("one", "rey", "1.0.0"),
        contract_row("two", "rey", "1.0.0"),
    )

    with pytest.raises(ConfigError, match="tell them apart"):
        _ai_instructions(ctx)


def test_a_contract_carrying_no_body_is_refused_rather_than_dropped() -> None:
    """It was declared, so nothing to send is a defect rather than an omission.

    A repeated key, a missing contract and a contract with no name or version
    are refused by the schema -- instruction_key is unique, the kind check
    requires a contract, and the columns are NOT NULL -- so they cannot reach
    this reader. What can is a row whose contract has no body.
    """
    ctx = instructions(contract_row("x", "rey", "1.0.0", body=""))

    with pytest.raises(ConfigError, match="carries no"):
        _ai_instructions(ctx)


def test_a_runtime_with_no_control_cannot_read_its_configuration() -> None:
    """Configured and unreadable is a refusal, not an absent capability."""
    with pytest.raises(ConfigError, match="no shared Control"):
        _ai_instructions(SimpleNamespace())


# -- the invariant: one path, no consumer supplying or suppressing -----------
#
# No consumer supplies or suppresses AI settings independently. Every task
# resolves Engine, Instruction, Creativity and Data Profile through the
# canonical AI settings object.

def test_a_contract_instruction_can_actually_execute(tmp_path: Any) -> None:
    """The Instruction setting is only real if a contract's body can be sent.

    Every instruction this runtime offers carries a reference and no text, so
    without a loader at the construction boundary none could run -- and the
    settings panel was offering them regardless.
    """
    from rey_lib.ai.construction import _contract_loader
    from rey_lib.ai.contracts import ContractResolver
    from rey_lib.ai.instructions import AIInstruction

    path = tmp_path / "c.yaml"
    path.write_text("contract:\n  name: x\n  version: '1'\nrules: []\n", encoding="utf-8")
    instruction = AIInstruction(
        id="x", kind=AIInstructionKind.CONTRACT, name="x 1", reference=str(path),
    )

    body = ContractResolver(loader=_contract_loader).body_of(instruction)

    assert "name: x" in body


def test_the_log_analysis_package_no_longer_carries_a_contract() -> None:
    """It arrives as the task's instruction instead, so sending it here too
    would be the same thing twice and could disagree with what a reader sees."""
    from rey_lib.logs.llm_package import _build_analysis_package

    package = _build_analysis_package(
        ctx=None,
        analysis_name="log_interpreter",
        source_record_type="RESULTS_SUMMARY",
        instructions={"name": "rey_log_interpreter", "rules": []},
        source={"record_type": "RESULTS_SUMMARY"},
    )

    assert "instructions" not in package
    assert set(package) == {"analysis_name", "source_record_type", "source"}


def test_no_consumer_names_an_instruction_of_its_own() -> None:
    """The invariant, asserted over the call sites rather than described.

    A consumer that passed AIRequest(instruction=...) would be supplying an AI
    setting independently, and one that passed an explicit NONE would be
    suppressing one. Both are what this batch exists to remove.
    """
    from pathlib import Path

    sources = [
        Path("rey_lib/logs/llm_package.py").read_text(encoding="utf-8"),
        Path("rey_lib/logs/summary.py").read_text(encoding="utf-8"),
    ]

    for source in sources:
        assert "instruction=AIInstruction" not in source
        assert "AIInstructionKind.NONE" not in source


# -- reading what a provider actually said -----------------------------------
#
# A model often wraps its answer in a fence. The runtime's own output reader
# already retries at the outermost object, so a caller that says it wants JSON
# gets a parsed payload; one that says nothing still gets text.

class _Fenced(EchoProvider):
    """A provider that wraps its JSON the way a real one often does."""

    def invoke(self, call: Any, *, cancelled: Any = None, on_text: Any = None) -> Any:  # noqa: ANN401, ARG002
        from rey_lib.ai.providers.base import ProviderReply

        return ProviderReply(text='```json\n{"items": [{"label": "from"}]}\n```')


def fenced_runtime(structured: bool) -> Any:
    """One runtime over a provider that fences, and a profile that permits JSON."""
    from rey_lib.ai.capabilities import AICapability, AICapabilitySet

    provider = _Fenced(name="echo")
    provider.capability_for = lambda model: AICapabilitySet.of(  # type: ignore[method-assign]
        AICapability.TEXT, AICapability.STRUCTURED_OUTPUT,
    )
    registry = AIRegistry(
        profiles=(AIProfile(id="dflt", provider="echo", model="m"),),
        providers=(provider,),
    )
    return AI(registry=registry, settings=AISettings(profile_id="dflt"))


def test_a_fenced_answer_is_parsed_when_the_caller_asked_for_json() -> None:
    """Normalized at the runtime's output boundary, not at each caller.

    OutputParser already retries at the outermost object when a model wraps its
    answer in prose or a fence. Asking for JSON is what puts that boundary in
    the path.
    """
    from rey_lib.ai.requests import AIOutputSpec

    result = fenced_runtime(True).execute(
        AIRequest.prompt("x", output=AIOutputSpec.json()),
    )

    assert result.value == {"items": [{"label": "from"}]}


def test_a_caller_that_asks_for_nothing_still_receives_text() -> None:
    """The log-interpretation path returns Markdown inside an envelope, so
    parsing every answer as JSON would break it. Each caller states its own need."""
    result = fenced_runtime(False).execute(AIRequest.prompt("x"))

    assert result.value is None
    assert result.text.startswith("```json")


def test_the_ollama_adapter_declares_the_structured_output_it_implements() -> None:
    """The adapter is the sole authority on what it can do, and it sets Ollama's
    format: json from call.json_output. Declaring less than it implements made
    the capability check refuse requests it could serve."""
    from rey_lib.ai.capabilities import AICapability
    from rey_lib.ai.providers.configuration import ConfiguredProvider
    from rey_lib.ai.providers.ollama_provider import OllamaProvider

    adapter = OllamaProvider(ConfiguredProvider(id="local", provider="ollama", model="m"))

    assert AICapability.STRUCTURED_OUTPUT in adapter.capability_for("m")
    # Still absent, because neither is implemented here.
    assert AICapability.TOOLS not in adapter.capability_for("m")


# -- the cutover: configuration comes from the database ----------------------

def test_the_runtime_reads_no_task_or_contract_configuration_from_yaml() -> None:
    """The property the cutover exists to give, asserted structurally.

    `ctx.ai_settings` and `ctx.ai_instructions` were the YAML sections the
    runtime built itself from. Nothing in the production build may read them
    now, and no contract file may be opened for a task's instruction -- the
    body arrives on the instruction.

    `ctx.llm` is deliberately not included: it is provider connection
    configuration, not task or contract configuration, and it stays in YAML.
    """
    import ast
    import inspect

    from rey_lib.config import bootstrap

    source = inspect.getsource(bootstrap)
    tree = ast.parse(source)
    built_by = {"_ai_instructions", "_ai_settings", "_ai_control", "_ai_installation"}

    reads: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in built_by:
            continue
        for inner in ast.walk(node):
            # getattr(ctx, "ai_settings") / getattr(ctx, "ai_instructions")
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "getattr"
                    and len(inner.args) >= 2
                    and isinstance(inner.args[1], ast.Constant)
                    and inner.args[1].value in ("ai_settings", "ai_instructions")):
                reads.append(f"{node.name} reads ctx.{inner.args[1].value}")
            # ctx.ai_settings / ctx.ai_instructions -- on ctx itself, which is
            # what distinguishes it from Control.ai_instructions(), the read
            # that replaced it.
            if (isinstance(inner, ast.Attribute)
                    and inner.attr in ("ai_settings", "ai_instructions")
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "ctx"):
                reads.append(f"{node.name} reads ctx.{inner.attr}")

    assert reads == [], "; ".join(reads)


def test_a_contract_body_arrives_on_the_instruction() -> None:
    """No file is opened for it, so the resolver needs no loader.

    ContractResolver answers with `text` before it looks at `reference`, which
    is what let the body move to the database without touching it.
    """
    from rey_lib.ai.contracts import ContractResolver

    ctx = instructions(contract_row("x", "rey", "1.0.0", body="rules: [one]"))
    instruction = next(
        i for i in _ai_instructions(ctx) if i.kind is AIInstructionKind.CONTRACT
    )

    assert instruction.text == "rules: [one]"
    assert not instruction.reference
    # No loader supplied, and it still resolves.
    assert ContractResolver().body_of(instruction) == "rules: [one]"

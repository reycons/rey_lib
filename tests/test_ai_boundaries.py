"""The boundaries the AI subsystem is built to hold, asserted over its source.

Behavioural tests prove it works. These prove it is still the thing that was
designed: a boundary is not kept by intending to keep it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

AI = Path(__file__).resolve().parents[1] / "rey_lib" / "ai"
REPO = Path(__file__).resolve().parents[2]

#: Comments and docstrings, removed before a rule reads a file.
#:
#: Every rule below is about what the code *does*. One that read prose would
#: fail the moment someone wrote down the rule it enforces.
_PROSE = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|#[^\n]*')


def code_of(path: Path) -> str:
    return _PROSE.sub(" ", path.read_text(encoding="utf-8"))


def sources() -> list[Path]:
    return sorted(AI.rglob("*.py"))


def test_the_subsystem_does_not_depend_on_the_old_one() -> None:
    """`rey_lib.llm` is learned evidence, not a dependency.

    The whole point of building this rather than objectifying that one.
    """
    for path in sources():
        assert "rey_lib.llm" not in code_of(path), f"{path.name} imports the old subsystem"


def test_nothing_here_is_presentation_aware() -> None:
    """No viewer, panel, region or browser concept exists in the domain."""
    forbidden = (
        "viewer", "panel", "region", "workspace", "presentation", "console",
        "browser", "window", "render", "widget", "tab",
    )
    for path in sources():
        code = code_of(path).lower()
        for word in forbidden:
            assert not re.search(rf"\b{word}\b", code), f"{path.name} names {word}"


def test_no_application_semantics_leaked_in() -> None:
    """Analyzer, autocomplete and Workbench semantics live in their own owners."""
    for word in ("autocomplete", "supersession", "caret", "debounce", "workbench", "analyzer"):
        for path in sources():
            assert word not in code_of(path).lower(), f"{path.name} names {word}"


def test_the_root_contains_no_provider_switch() -> None:
    """Provider vocabulary never reaches the aggregate root."""
    root = code_of(AI / "ai.py")

    for provider in ("openai", "anthropic", "ollama", "gemini", "echo"):
        assert provider not in root.lower(), f"the root names provider {provider}"
    assert "provider ==" not in root


def test_the_root_delegates_rather_than_executes() -> None:
    """`AI` resolves and hands over; it holds no execution machinery."""
    root = code_of(AI / "ai.py")

    assert "self._executor.execute" in root
    assert "self._executor.stream" in root
    for mechanism in ("jsonschema", "json.loads", "retry_on", "max_attempts", "invoke("):
        assert mechanism not in root, f"the root implements {mechanism}"


def test_there_is_no_module_level_mutable_state() -> None:
    """No process-global registry, cache or singleton.

    The old subsystem kept providers in a module-level dict, so two
    installations in one process shared it. That is the state this does not
    have, and this is what proves it.
    """
    # Unindented only: a local inside a method is that call's own, and is the
    # opposite of shared state.
    binding = re.compile(r"^_?[a-z][a-z0-9_]*\s*(?::[^=]+)?=\s*(?:\{\}|\[\]|set\(\)|dict\(\)|list\(\))\s*$")
    for path in sources():
        for line in code_of(path).splitlines():
            if line[:1].isspace() or not line.strip():
                continue
            if binding.match(line.strip()):
                pytest.fail(f"{path.name} holds module-level mutable state: {line.strip()}")


def test_a_provider_reply_never_escapes_as_itself() -> None:
    """Provider types stop at the boundary.

    `ProviderReply` and `ProviderCall` are named by the executor and the
    adapters, and by nothing an application would import.
    """
    application_facing = ("ai.py", "results.py", "requests.py", "settings.py", "sessions.py")
    for name in application_facing:
        code = code_of(AI / name)
        assert "ProviderReply" not in code, f"{name} exposes a provider reply"
        assert "ProviderCall" not in code, f"{name} exposes a provider call"


def test_no_production_consumer_imports_the_new_subsystem() -> None:
    """Build is not cutover: nothing is attached yet."""
    found: list[str] = []
    for path in (REPO).rglob("*.py"):
        parts = set(path.parts)
        if ".venv" in parts or "__pycache__" in parts:
            continue
        if AI in path.parents or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"\bfrom rey_lib\.ai\b|\bimport rey_lib\.ai\b", text):
            found.append(str(path.relative_to(REPO)))

    assert not found, f"the new subsystem is already attached to {found}"


def test_every_error_the_subsystem_raises_is_its_own() -> None:
    """An application never has to catch a provider's exception."""
    from rey_lib.ai import errors

    hierarchy = [
        name for name in dir(errors)
        if name.startswith("AI") and isinstance(getattr(errors, name), type)
    ]
    for name in hierarchy:
        assert issubclass(getattr(errors, name), errors.AIError), name


def test_ctx_is_named_only_at_the_construction_boundary() -> None:
    """ctx is construction/discovery input, never retained runtime state.

    The guard targets the ``ctx`` **identifier** -- as a parameter, an
    attribute or a name that is read -- and deliberately not the English word
    "context". ``AIRequest.context`` and ``ResolvedAIRequest.context`` are
    unrelated caller metadata, and a crude substring guard would fail on them
    while catching nothing that matters.
    """
    offenders: list[str] = []
    for path in sorted(AI.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "construction.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "ctx":
                offenders.append(f"{path.name}:{node.lineno} reads ctx")
            elif isinstance(node, ast.Attribute) and node.attr == "ctx":
                offenders.append(f"{path.name}:{node.lineno} holds .ctx")
            elif isinstance(node, ast.arg) and node.arg == "ctx":
                offenders.append(f"{path.name}:{node.lineno} takes ctx")

    assert not offenders, (
        "ctx must not cross the construction boundary: " + "; ".join(offenders)
    )


def test_the_caller_metadata_context_fields_are_untouched_by_that_guard() -> None:
    """The exemption is real: these exist and are not ctx."""
    from rey_lib.ai import AIRequest

    assert AIRequest.prompt("x", context={"caller": "console"}).context == {
        "caller": "console",
    }


def test_no_provider_sdk_is_imported_above_the_adapter_boundary() -> None:
    """Provider vocabulary stops at the adapter, and no external agent model enters."""
    banned = ("openai", "anthropic", "litellm", "pydantic_ai", "agents")
    offenders: list[str] = []
    for path in sorted(AI.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in banned:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")

    assert not offenders, "external model leaked inward: " + "; ".join(offenders)

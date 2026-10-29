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


def test_the_root_holds_no_retry_or_validation_machinery() -> None:
    """`AI` runs a task through its mechanism; validation is OutputParser's."""
    root = code_of(AI / "ai.py")

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


def test_only_ai_app_constructs_an_ai() -> None:
    """ai_app is the only application that constructs and retains an AI (#1025).

    Every other application reaches ai_app's live AI over the transports. One
    that built its own would hold a second AI beside the live one.
    """
    allowed = {REPO / "ai_app" / "main.py"}
    offenders: list[str] = []
    for path in REPO.rglob("*.py"):
        parts = set(path.parts)
        if ".venv" in parts or "__pycache__" in parts or "node_modules" in parts:
            continue
        if AI in path.parents or path.name.startswith("test_") or "tests" in parts:
            continue
        if path in allowed:
            continue
        text = code_of(path)
        if re.search(r"(?<![\w.])AI\s*\(", text):
            offenders.append(str(path.relative_to(REPO)))

    assert not offenders, "these construct an AI of their own: " + ", ".join(offenders)


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
    "context", which a crude substring guard would trip over while catching
    nothing that matters.
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


def test_no_provider_sdk_is_imported_above_the_adapter_boundary() -> None:
    """Provider vocabulary stops at the adapter, and no external agent model enters.

    Two bans, and they are not the same ban. A provider SDK is legitimate
    *inside* ``providers/`` -- that is what an adapter is for -- and forbidden
    everywhere above it. An external agent framework is forbidden everywhere,
    including in an adapter, because adopting one there would still put its
    execution model under the boundary.

    Pydantic AI is the exception by decision (backlog 1019, ai.redesign): it is
    AI's approved implementation dependency, so it is permitted inside this
    subsystem -- and only here, which the next test holds.
    """
    sdks = ("openai", "anthropic", "google")
    frameworks = ("litellm", "agents")
    offenders: list[str] = []
    for path in sorted(AI.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        adapter = path.parent.name == "providers"
        banned = frameworks if adapter else sdks + frameworks
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


def test_pydantic_ai_is_imported_only_inside_the_subsystem() -> None:
    """Pydantic AI is AI's implementation dependency, never a competing architecture.

    Anything outside ``rey_lib/ai`` that imported it would be a second model or
    tool layer beside AI (backlog 1019, ai.redesign).
    """
    importing = re.compile(r"^\s*(?:from|import)\s+pydantic_ai\b", re.M)
    offenders: list[str] = []
    for path in REPO.rglob("*.py"):
        parts = set(path.parts)
        if ".venv" in parts or "__pycache__" in parts or "node_modules" in parts:
            continue
        if AI in path.parents or path.name.startswith("test_"):
            continue
        if importing.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(REPO)))

    assert not offenders, "pydantic_ai imported outside rey_lib/ai: " + ", ".join(offenders)


class TestAConnectionIsAName:
    """The AI carries which connection an ask queries through, and no more.

    Whether a connection may be used by a language model is the connection's own
    declaration, and connections belong to the database layer. What crosses into
    this one is the answer rather than the objects: a set of permitted names.
    """

    @staticmethod
    def _ai(connections=()):
        from rey_lib.ai.ai import AI

        return AI(connections=connections)

    @staticmethod
    def _control(group_connection: str = "", task_connection: str = ""):
        """The governed reads, with one group row and one task row."""
        class _Control:
            def ai_configuration(self, installation):
                return [
                    {"row_type": "group", "connection_name": group_connection},
                    {"row_type": "task", "task_key": "search", "connection_name": task_connection},
                ]

            def ai_engine_profiles(self):
                return []

            def ai_instructions(self):
                return []

        return _Control()

    def test_it_holds_names_and_opens_nothing(self) -> None:
        offered = self._ai(("code_index", "warehouse")).connections()

        assert offered == ("code_index", "warehouse")
        assert all(isinstance(name, str) for name in offered)

    def test_a_connection_nothing_offered_is_refused(self) -> None:
        """Not "no such connection" -- one may well exist. It has not said a
        model may use it, which is the refusal a reader needs."""
        from rey_lib.ai.errors import AISelectionError

        with pytest.raises(AISelectionError, match="offered to AI"):
            self._ai(("code_index",)).projection(self._control("rey_apps"), "local")

    def test_a_task_naming_an_unoffered_connection_is_refused(self) -> None:
        """Every level, as for a profile. A task configured into a state that
        only fails when that task next runs is a state nobody sees."""
        from rey_lib.ai.errors import AISelectionError

        with pytest.raises(AISelectionError, match="for task 'search'"):
            self._ai(("code_index",)).projection(self._control("", "rey_apps"), "local")

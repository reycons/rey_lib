"""RunLog owns run logging, and nothing else can quietly take it back.

Three previous attempts at this migration each shipped green and each preserved
the architecture it was meant to remove: a ``log_run_record`` shim, a class that
still read ``ctx`` per record, then ``ctx.run_log`` acting as the locator for its
own replacement. Every one passed the behaviour tests, because behaviour was
never what was wrong.

So these are structural. They read the source rather than run it, and they fail
on the *shape* that let the earlier attempts look finished:

- a run log that can see a context can start reading one again
- a locator, cache or ambient lookup lets one execution get two owners of state
  that must be single
- a retired state module that is still importable is still an owner
- more than one construction point is more than one owner

Scoped to rey_lib for the module rules, because this policy governs this
repository. The launch-boundary rules walk the sibling application repositories,
so they only run where those are checked out together — the layout the migration
was performed against.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APPS_ROOT = REPO.parent
PACKAGE = REPO / "rey_lib"
LOGS = PACKAGE / "logs"

RUN_LOG = LOGS / "run_log.py"

# The applications that enter through the shared process boundary.
ENTRY_POINTS = (
    "file_operator/main.py",
    "rey_loader/main.py",
    "rey_analyzer/main.py",
    "rey_messaging/rey_messaging/cli.py",
    "rey_db_admin/main.py",
    "ftp_sync/main.py",
    "pipeline_coordinator/pipeline_coordinator/cli.py",
    "rey_console/rey_console/cli.py",
    "console_next/console_next/cli.py",
)

# Retired: their state moved into RunLog and the modules were deleted rather
# than left forwarding, so a surviving import is a real regression.
RETIRED_MODULES = ("record_parenting", "nest_level", "run_state")


def _production_files() -> list[Path]:
    """Every production module in the package (tests are not the subject)."""
    return [
        path for path in PACKAGE.rglob("*.py")
        if not any(part.startswith(".") or part in {"venv", "tests"}
                   for part in path.parts)
    ]


def _run_log_class() -> ast.ClassDef:
    """The RunLog class node."""
    tree = ast.parse(RUN_LOG.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RunLog":
            return node
    raise AssertionError("RunLog is not defined in run_log.py")


def _method(name: str) -> ast.FunctionDef:
    """One method of RunLog."""
    for node in _run_log_class().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"RunLog has no method {name!r}")


# ---------------------------------------------------------------------------
# The owner holds no context
# ---------------------------------------------------------------------------

class TestRunLogDoesNotSeeAContext:
    """It cannot read what it was never given."""

    @pytest.mark.parametrize("method", ["__init__", "append"])
    def test_the_method_takes_no_ctx(self, method: str) -> None:
        node = _method(method)
        params = [a.arg for a in node.args.args + node.args.kwonlyargs]
        assert "ctx" not in params, (
            f"RunLog.{method} takes a ctx. The owner is constructed with the "
            "state it needs and reads nothing afterwards."
        )

    def test_it_stores_no_ctx(self) -> None:
        for node in ast.walk(_run_log_class()):
            if isinstance(node, ast.Attribute) and node.attr.endswith("ctx"):
                raise AssertionError(
                    f"RunLog assigns or reads {node.attr!r}. Holding a context "
                    "is how the previous attempt kept reading one per record."
                )

    def test_the_module_names_no_ctx_attribute_read(self) -> None:
        """No ``getattr(ctx, ...)`` anywhere in the owner's module."""
        source = RUN_LOG.read_text(encoding="utf-8")
        offenders = re.findall(r"getattr\(\s*ctx\b", source)
        assert offenders == [], (
            "run_log.py reads attributes off a ctx. Every value the owner needs "
            "arrives at construction or through a bind_* transition."
        )


# ---------------------------------------------------------------------------
# No second way to reach an owner
# ---------------------------------------------------------------------------

class TestThereIsNoLocator:
    """One execution must never be able to get two owners of one log."""

    _FORBIDDEN = (
        (r"\brun_log_for\s*\(", "run_log_for(ctx) is a locator"),
        (r"\bctx\.run_log\b", "ctx.run_log makes the context the owner's locator"),
        (r"\bestablish_run_log\s*\(", "establish_run_log is locate-or-create"),
        (r"ContextVar\(\s*[\"']run_log", "a ContextVar run log is an ambient lookup"),
    )

    # file_routing/sanitization pass a frozen operation context whose parameter
    # is spelled ``ctx`` and which carries an explicit ``run_log`` field. That is
    # the owner being handed over, not a context being asked for one. Named here
    # rather than matched loosely, so the rule stays sharp everywhere else.
    _EXPLICIT_OPERATION_CONTEXTS = {"rey_lib/files/file_routing.py"}

    @pytest.mark.parametrize("pattern,why", _FORBIDDEN)
    def test_the_pattern_is_absent(self, pattern: str, why: str) -> None:
        offenders = [
            str(path.relative_to(REPO))
            for path in _production_files()
            if re.search(pattern, path.read_text(encoding="utf-8", errors="ignore"))
            and str(path.relative_to(REPO)) not in self._EXPLICIT_OPERATION_CONTEXTS
        ]
        assert offenders == [], f"{why}: {', '.join(offenders)}"


class TestRetiredOwnersAreGone:
    """A module that still imports is still an owner."""

    @pytest.mark.parametrize("module", RETIRED_MODULES)
    def test_the_module_does_not_exist(self, module: str) -> None:
        assert not (LOGS / f"{module}.py").exists(), (
            f"logs/{module}.py is back. Its state belongs to RunLog; leaving the "
            "module forwarding preserves it as a public architectural concept."
        )

    @pytest.mark.parametrize("module", RETIRED_MODULES)
    def test_nothing_imports_it(self, module: str) -> None:
        pattern = re.compile(rf"^\s*(from|import)\s+.*\b{module}\b", re.MULTILINE)
        offenders = [
            str(path.relative_to(REPO))
            for path in _production_files()
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
        ]
        assert offenders == [], (
            f"{module} is still imported by: {', '.join(offenders)}"
        )


# ---------------------------------------------------------------------------
# The runtime owns the lifecycle
# ---------------------------------------------------------------------------

class TestTheRuntimeOwnsTheRunLog:
    """Built once at the composition root, collected at the process end."""

    def test_bootstrap_is_the_only_construction_point(self) -> None:
        """Only the composition root and the owner's own module build one."""
        allowed = {
            "rey_lib/config/bootstrap.py",   # the composition root
            "rey_lib/logs/run_log.py",       # the class itself
            # Post-run finalization opens a run log on a *finished* log it did
            # not write, which is a different run, not a second owner of this one.
            "rey_lib/files/log_run_rollback.py",
        }
        offenders = [
            str(path.relative_to(REPO))
            for path in _production_files()
            if re.search(r"\bRunLog\s*\(", path.read_text(encoding="utf-8", errors="ignore"))
            and str(path.relative_to(REPO)) not in allowed
        ]
        assert offenders == [], (
            "RunLog is constructed outside the composition root by: "
            f"{', '.join(offenders)}. A second construction is a second owner."
        )

    def test_the_runtime_registers_it_for_collection(self) -> None:
        source = (PACKAGE / "config" / "bootstrap.py").read_text(encoding="utf-8")
        assert "register_runtime_object(ctx, run_log)" in source, (
            "app_runtime does not register the run log for collection, so "
            "nothing closes it at the end of the process."
        )

    def test_control_is_built_by_the_runtime_not_the_run_log(self) -> None:
        """RunLog references Control; it does not decide when Control exists."""
        for node in ast.walk(_run_log_class()):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "Control"):
                raise AssertionError(
                    "RunLog constructs Control. It holds a reference; the "
                    "runtime owns the lifetime."
                )

    @pytest.mark.parametrize("entry_point", ENTRY_POINTS)
    def test_every_entry_point_takes_the_run_log_from_the_boundary(
            self, entry_point: str) -> None:
        path = APPS_ROOT / entry_point
        if not path.exists():
            pytest.skip(f"{entry_point} is not checked out beside rey_lib")
        source = path.read_text(encoding="utf-8")
        assert re.search(r"with app_runtime\(.*\) as \(ctx, run_log\)", source), (
            f"{entry_point} does not take the run log from app_runtime. An entry "
            "point that builds its own has a second owner for one process."
        )


# ---------------------------------------------------------------------------
# Writers take the owner
# ---------------------------------------------------------------------------

class TestWritersTakeTheOwner:
    """A writer's first argument is what it writes through."""

    def test_no_log_writer_takes_ctx_as_its_logging_owner(self) -> None:
        """``log_*`` in the logs package writes, so it takes the run log.

        ``ctx`` may still be present for a genuinely non-logging concern — a
        path resolver, a rollback target — which is why this looks at the first
        parameter rather than banning the name.
        """
        offenders = []
        for path in LOGS.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                if not node.name.startswith("log_"):
                    continue
                params = [a.arg for a in node.args.args]
                if params and params[0] == "ctx":
                    offenders.append(f"{path.name}:{node.name}")
        # Named, with the reason each is not a run-log writer:
        #   log_run_record  — the delegation itself; it takes the run log and is
        #                     only listed because its name starts with log_.
        #   log_enter/exit  — indentation on the Python logger, not a record.
        #   log_file_manifest_record — writes the installation file manifest via
        #                     ctx.paths; a different artifact from the run log.
        expected = {
            "record_enrichment.py:log_run_record",
            "logging_setup.py:log_enter",
            "logging_setup.py:log_exit",
            "file_manifest.py:log_file_manifest_record",
        }
        assert set(offenders) - expected == set(), (
            "these log_* writers take ctx as their logging owner: "
            f"{', '.join(sorted(set(offenders) - expected))}"
        )

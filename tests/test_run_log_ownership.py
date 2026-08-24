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
import json
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
        assert re.search(r"with app_runtime\(.*\) as \(ctx, run_log\)", source,
                             re.DOTALL), (
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


# ---------------------------------------------------------------------------
# The ambient binding
# ---------------------------------------------------------------------------

class TestTheAmbientBindingIsScoped:
    """The one sanctioned ambient adapter, and the rules that keep it one."""

    def test_the_runtime_clears_the_binding_at_teardown(self) -> None:
        source = (PACKAGE / "config" / "bootstrap.py").read_text(encoding="utf-8")
        assert "reset_run_binding()" in source, (
            "app_runtime does not reset the ambient binding, so a collected run "
            "log stays bound for whatever runs next in this process."
        )

    def test_bind_and_clear_are_paired_in_production(self) -> None:
        """Every production bind_run has a clear_run in the same module.

        The binding is a stack now, so an unmatched bind leaks a frame and an
        unmatched clear pops a scope that is still executing.
        """
        offenders = []
        for path in _production_files():
            source = path.read_text(encoding="utf-8", errors="ignore")
            binds = len(re.findall(r"(?<!def )\bbind_run\s*\(", source))
            clears = len(re.findall(r"(?<!def )\b(?:clear_run|reset_run_binding)\s*\(", source))
            if binds and not clears:
                offenders.append(str(path.relative_to(REPO)))
        assert offenders == [], (
            f"bind_run with no matching clear in: {', '.join(offenders)}"
        )

    def test_only_file_operation_recording_reads_the_binding(self) -> None:
        """The ambient path is an adapter for one caller, not a general locator.

        Ordinary writers take the run log explicitly. If a second subsystem
        starts resolving its owner ambiently, bind_run has become the locator
        this migration removed.
        """
        allowed = {
            "rey_lib/logs/record_enrichment.py",  # defines it
            "rey_lib/logs/file_records.py",       # the one sanctioned consumer
            "rey_lib/files/file_utils.py",        # reads it to report the bound run
        }
        offenders = [
            str(path.relative_to(REPO))
            for path in _production_files()
            if re.search(r"\bcurrent_run\s*\(|_CURRENT_RUN\b",
                         path.read_text(encoding="utf-8", errors="ignore"))
            and str(path.relative_to(REPO)) not in allowed
        ]
        assert offenders == [], (
            f"the ambient binding is read outside file-operation recording by: "
            f"{', '.join(offenders)}"
        )


class TestOneRunIsOneLog:
    """The run log's path is its identity."""

    def test_rebinding_after_records_are_written_is_refused(self, tmp_path) -> None:
        from rey_lib.errors.error_utils import StateError
        from tests.conftest import make_run_log

        run_log = make_run_log(tmp_path, path=str(tmp_path / "a.jsonl"))
        run_log.append("ROW_COUNT", count_name="c", count=1)
        with pytest.raises(StateError, match="One run is one log"):
            run_log.bind_path(tmp_path / "b.jsonl")

    def test_rebinding_before_any_record_is_allowed(self, tmp_path) -> None:
        """A pipeline learns its log directory after launch, so it may still name it."""
        from tests.conftest import make_run_log

        run_log = make_run_log(tmp_path, path=str(tmp_path / "a.jsonl"))
        run_log.bind_path(tmp_path / "b.jsonl")
        assert Path(run_log.path()).name == "b.jsonl"

    def test_concurrent_scopes_for_one_run_are_supported(self, tmp_path) -> None:
        """Threads sharing one run is the intended case and must stay silent.

        pipeline_coordinator runs a parallel step group sharing one run, which
        is why the bound value is process-global rather than thread-local.
        """
        import threading

        from rey_lib.logs.record_enrichment import (
            bind_run, clear_run, current_run, reset_run_binding,
        )
        from tests.conftest import make_run_log

        reset_run_binding()
        run_log = make_run_log(tmp_path, path=str(tmp_path / "a.jsonl"), run_id="A")
        bind_run(run_log)
        worker = threading.Thread(target=lambda: (bind_run(run_log), clear_run()))
        worker.start()
        worker.join()

        assert current_run()["run_id"] == "A", (
            "a thread sharing the bound run must leave it bound on exit"
        )
        clear_run()
        assert current_run() is None
        reset_run_binding()

    def test_overlapping_scopes_for_different_runs_are_reported(
            self, tmp_path, caplog) -> None:
        """The invariant is enforced, not assumed.

        Every concurrent unit of execution in the estate today is a subprocess,
        so distinct ambient scopes never overlap in one interpreter. Nothing in
        the language guarantees that, so a bind that breaks it says so instead
        of silently recording file operations against the wrong run.
        """
        import logging
        import threading

        from rey_lib.logs.record_enrichment import (
            bind_run, clear_run, reset_run_binding,
        )
        from tests.conftest import make_run_log

        reset_run_binding()
        first = make_run_log(tmp_path, path=str(tmp_path / "a.jsonl"), run_id="A")
        second = make_run_log(tmp_path, path=str(tmp_path / "b.jsonl"), run_id="B")

        with caplog.at_level(logging.WARNING):
            bind_run(first)
            worker = threading.Thread(target=lambda: (bind_run(second), clear_run()))
            worker.start()
            worker.join()
            clear_run()

        assert any("must not overlap" in record.message for record in caplog.records), (
            "binding a different run while another thread holds a scope was not "
            "reported; the invariant is unenforced again"
        )
        reset_run_binding()

    def test_pairing_frames_are_per_thread(self) -> None:
        """Nesting is a per-thread property, so the frames must be too.

        A single global frame list is correct only for properly nested pushes
        and pops. Threads do not nest -- A.bind, B.bind, A.clear, B.clear is
        reachable -- so a global list would restore the wrong owner.
        """
        source = (LOGS / "record_enrichment.py").read_text(encoding="utf-8")
        assert '"frames"' in source and "threading.get_ident()" in source, (
            "the ambient frames are no longer keyed by thread"
        )


# ---------------------------------------------------------------------------
# Adoption is a move
# ---------------------------------------------------------------------------

class TestAdoptionIsAMove:
    """A field the run log took over does not stay readable on the context.

    Leaving it behind is how this migration would quietly reintroduce what it
    removed: a caller reads ctx.parent_run_id to stamp a record, the run log
    stamps its own, and the two disagree the moment either moves.
    """

    def test_the_adopted_fields_are_removed_from_the_context(self, tmp_path) -> None:
        from rey_lib.config.bootstrap import _ADOPTED_FIELDS, open_run_log
        from rey_lib.config.config_utils import Namespace

        ctx = Namespace({
            "run_id": "R1", "run_timestamp": "ts", "app_name": "rey_loader",
            "owner_app_name": "rey_loader", "run_log_dir": str(tmp_path),
            "log_file": str(tmp_path / "a.jsonl"),
            "parent_run_id": "R0", "subject_type": "app", "subject_id": "s",
            "subject_name": "S", "pipeline_run_id": "P1", "workflow_run_id": "W1",
            "pipeline_id": "PI", "workflow_id": "WI",
        })
        run_log = open_run_log(ctx)

        left = [f for f in _ADOPTED_FIELDS if getattr(ctx, f, None) is not None]
        assert left == [], f"the run log adopted these but the context kept them: {left}"
        # And the run log actually took them, rather than both losing the value.
        assert run_log._lineage["parent_run_id"] == "R0"
        assert run_log._lineage["pipeline_run_id"] == "P1"

    def test_the_inherited_runtime_snapshot_survives(self, tmp_path) -> None:
        """ctx.runtime is the enclosing pipeline's snapshot, not this run's fields.

        It is how a step subprocess receives the run above it, so removing it
        would cut the lineage the records are supposed to carry.
        """
        from rey_lib.config.bootstrap import open_run_log
        from rey_lib.config.config_utils import Namespace

        ctx = Namespace({
            "run_id": "R1", "run_timestamp": "ts", "app_name": "rey_loader",
            "log_file": str(tmp_path / "a.jsonl"),
            "runtime": Namespace({"pipeline_run_id": "P1"}),
        })
        run_log = open_run_log(ctx)

        assert getattr(ctx.runtime, "pipeline_run_id", None) == "P1"
        assert run_log._lineage.get("pipeline_run_id") == "P1"

    def test_no_caller_restamps_lineage_the_run_log_already_adds(self) -> None:
        """A payload that sets a lineage field by hand is a second stamper."""
        from rey_lib.logs.run_log import DOMAIN_FIELDS, LINEAGE_FIELDS

        offenders = []
        for path in APPS_ROOT.rglob("*.py"):
            if any(p.startswith(".") or p in {"venv", "tests", "build", "dist"}
                   for p in path.parts):
                continue
            source = path.read_text(encoding="utf-8", errors="ignore")
            for field in (*LINEAGE_FIELDS, *DOMAIN_FIELDS):
                if re.search(rf'"{field}":\s*getattr\(ctx,', source):
                    offenders.append(f"{path.name}:{field}")
        assert offenders == [], (
            "these stamp a lineage field the run log already stamps: "
            f"{', '.join(sorted(set(offenders)))}"
        )


# ---------------------------------------------------------------------------
# The database destination
# ---------------------------------------------------------------------------

class TestControlIsSubordinateToRunLog:
    """Control is the run log's DB mechanism, not a second logging owner."""

    def test_only_the_run_log_drives_the_control_lifecycle(self) -> None:
        """No caller opens a batch, a step or an event except through RunLog."""
        import ast

        methods = {"log_event", "start_batch", "end_batch", "start_step", "end_step"}
        allowed = {"rey_lib/logs/run_log.py", "rey_lib/control/control.py"}
        offenders = []
        for path in _production_files():
            name = str(path.relative_to(REPO))
            if name in allowed:
                continue
            # Parsed, not matched: a usage example in a package docstring is
            # prose, and a rule that cannot tell prose from a call is a rule
            # people learn to work around.
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in methods):
                    offenders.append(f"{name}:{node.lineno}")
        assert offenders == [], (
            "these reach the control database for run records without going "
            f"through the run log: {', '.join(offenders)}"
        )

    def test_the_run_store_module_holds_no_persistence_authority(self) -> None:
        """It reads configuration at launch and owns nothing afterwards."""
        import ast

        source = (LOGS / "run_store.py").read_text(encoding="utf-8")
        defined = {n.name for n in ast.parse(source).body
                   if isinstance(n, ast.FunctionDef)}
        retired = {"writes_jsonl", "writes_db", "new_batch_intent",
                   "require_structural_record", "persist_run_start",
                   "persist_step_start", "persist_step_end",
                   "persist_run_complete", "persist_record", "_severity_of"}
        assert defined & retired == set(), (
            "run_store has taken persistence decisions back from the run log: "
            f"{', '.join(sorted(defined & retired))}"
        )

    def test_control_is_closed_by_the_runtime(self) -> None:
        """Registered for collection, so it must answer close() itself.

        Without an explicit one it resolves through __getattr__ to the context,
        which has none, and every successful run fails at teardown.
        """
        from rey_lib.control.control import Control

        assert "close" in vars(Control), (
            "Control has no close() of its own; runtime collection would send "
            "it to the context and fail the teardown of every DB-logging run"
        )

    def test_a_record_raised_by_persistence_is_not_written(self, tmp_path) -> None:
        """The DB sink runs SQL, and SQL execution is a logged event.

        Without this the run log would record its own writes as run evidence,
        and persisting those would call the database to record the call that
        was recording something.
        """
        from tests.conftest import make_run_log
        from rey_lib.logs.sql_records import log_sql_execution

        class _Control:
            batch_id = 1
            batch_step_id = None
            owns_batch = False
            run_log = None
            events: list = []

            def write_run_log_record(self, **kwargs) -> None:
                type(self).events.append(kwargs["record_type"])
                # What every real control routine does: run SQL.
                log_sql_execution(self.run_log, operation="write_run_log_record",
                                  status="success")

        control = _Control()
        _Control.events = []
        run_log = make_run_log(tmp_path, path=str(tmp_path / "a.jsonl"))
        run_log.destination = "both"
        run_log.control = control
        control.run_log = run_log

        run_log.append("ROW_COUNT", count_name="c", count=1)

        assert _Control.events == ["ROW_COUNT"], (
            f"persistence recursed or recorded itself: {_Control.events}"
        )
        written = [json.loads(line) for line
                   in (tmp_path / "a.jsonl").read_text().splitlines() if line.strip()]
        assert [r["record_type"] for r in written] == ["ROW_COUNT"], (
            "the run log recorded its own persistence as run evidence"
        )

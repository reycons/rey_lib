"""rey_lib's architecture boundaries, asserted through the central authority.

Contract: rey_lib_architecture_policy.sgc.yaml

This repository declares its policy as data in repository_map.rules.yaml. The
generator produces the structural facts and rey_lib.repository_map.boundaries
evaluates the policy against them. This test invokes that one authority and
asserts the result; it encodes no rule, and it reads no source tree.

rey_lib owns both the map implementation and its own repository policy, so this
makes rey_lib a consumer of its own package. That self-consumption is
intentional and recorded in the contract. The alternative — a private checker
inside the repository that owns the shared one — is the second enforcement path
this whole architecture exists to avoid.

Only what the evidence model can prove is asserted here. Statements this
repository is genuinely bound by but the facts cannot demonstrate, such as "no
hard-coded paths", are listed in the contract as not claimed rather than
approximated with a rule that would look like enforcement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map import (
    LANGUAGE_EXTRACTORS,
    extract_executable_references,
    inventory_files,
    load_scan_rules,
)
from rey_lib.repository_map.boundaries import check_architecture_boundaries

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "repository_map.rules.yaml"


@pytest.fixture(scope="module")
def violations():
    """Return every architecture-boundary violation in this repository."""
    rules = load_scan_rules(RULES_PATH)
    files = inventory_files(REPO_ROOT, rules)
    references = []
    for record in files:
        if record.language not in LANGUAGE_EXTRACTORS:
            continue
        if not rules.extracts_facts_from(record):
            continue
        references.extend(
            extract_executable_references(REPO_ROOT / record.path, record.language, record.path)
        )
    return check_architecture_boundaries(rules, references=references, files=files)


def test_this_repository_declares_policy() -> None:
    """A framework nine repositories depend on should not be unjudged."""
    rules = load_scan_rules(RULES_PATH)

    assert rules.rules_for("architecture_rules"), "rey_lib declares no architecture policy."
    assert rules.declares_any_policy is True


def test_the_shared_framework_imports_no_application(violations) -> None:
    """rey_lib is depended upon by nine repositories and depends on none.

    Proves the import relationship only. It is a proxy for one dimension of
    "owns no application business logic" and is not that statement.
    """
    offenders = [
        f"{v.source_path}:{v.source_line} -> {v.callee}"
        for v in violations
        if v.rule_id == "shared_framework_imports_no_application"
    ]

    assert offenders == []


def test_logging_routes_through_the_logging_owner(violations) -> None:
    """Framework code obtains loggers through the rey_lib logging API.

    Five sites constructed a logger directly when this policy was declared and
    were corrected: control_utils, db/procedure_map, and three logs modules.
    record_enrichment uses a lazy in-function import because logging_setup
    imports from it, which is the idiom this package already uses to reach a
    higher layer.

    The rule forbids the direct logging API only. print() is not a logging
    call: folder_maker._print_result writes a CLI summary to stdout and is
    untouched. That is a narrowed rule, not a widened exemption list.
    """
    offenders = [
        f"{v.source_path}:{v.source_line} -> {v.callee}"
        for v in violations
        if v.rule_id == "logging_routes_through_the_logging_owner"
    ]

    assert offenders == []


def test_run_identity_has_one_owner(violations) -> None:
    """The logging layer consumes a run identity and never mints one.

    One execution, one run_id, minted once in rey_lib.run.identity. A second
    minting site is how a process handle and a durable record end up describing
    one execution under two names, leaving a reader to correlate them by name
    and start time -- which is what the Console did before this.

    record_enrichment minted the run id when this policy was declared and was
    corrected: it now requires an identity the launch boundary already
    established, and no longer imports uuid at all.

    Scoped to the logging layer rather than to rey_lib, because uuid4
    legitimately produces error_id, message_id, operation_id, profile_id and
    approval_id elsewhere. A rule wide enough to catch those would need seven
    exemptions, which is the grandfather list this policy refuses to be.

    execution_records is exempt because it mints failure_record_id -- the
    identity of an error record, not of a run. A different owner, not an
    uncorrected site.
    """
    offenders = [
        f"{v.source_path}:{v.source_line} -> {v.callee}"
        for v in violations
        if v.rule_id == "run_identity_has_one_owner"
    ]

    assert offenders == []


def test_logging_does_not_reach_up_into_run(violations) -> None:
    """The layer chain runs downward, and the logging layer is below run.

    Runner -> rey_lib.run -> rey_lib.logs -> control_utils -> procedure map ->
    DB adapter -> database. rey_lib.run sits above rey_lib.logs, so logs
    importing run inverts the chain.

    One site did exactly that when this policy was declared and was corrected.
    Moving the minting out of record_enrichment left behind a resolve function
    that reached back up into rey_lib.run through a lazy import -- the smallest
    change that moved the owner, and an upward call all the same. It is now
    require_run_id: identity is established at the launch boundary and arrives
    on the context, rather than being fetched from below at write time.

    The inversion is not only a rule: importing rey_lib.run from a logs module
    raises ImportError, because rey_lib.run.state imports rey_lib.logs. The
    guard names the direction that the cycle would otherwise report as an
    accident.
    """
    offenders = [
        f"{v.source_path}:{v.source_line} -> {v.callee}"
        for v in violations
        if v.rule_id == "logging_does_not_reach_up_into_run"
    ]

    assert offenders == []


def test_control_is_reached_only_through_logs(violations) -> None:
    """Control is the control database, and holding it is how control is reached.

    logs owns persistence orchestration; control owns the database operations
    behind it. The hop between them is the architecture, not overhead: it is
    what keeps a change of run store localized, and what stops an application
    recording a run straight into the control database.

    The boundary moved to construction when the procedural surface became one
    object. The earlier version of this rule named the five run/batch/step/event
    functions; as methods they can no longer be matched that way, because a
    call on a local variable is matched by that variable's name rather than by
    the owner. Taking the object is the single point every use passes through.

    That widens it, and the widening is real rather than incidental: Control
    also carries artifacts, contracts and config snapshots, which this boundary
    has no opinion about. None has a production caller today, so the rule costs
    nothing now and will fire on the first one -- the review this policy asks
    for, not a failure.

    Scoped to rey_lib because this policy governs this repository. The same
    prohibition on applications, the Runner, workflow and pipeline code is not
    enforceable from here and belongs in each repository's own rules.
    """
    offenders = [
        f"{v.source_path}:{v.source_line} -> {v.callee}"
        for v in violations
        if v.rule_id == "control_is_reached_only_through_logs"
    ]

    assert offenders == []


def test_connections_are_opened_only_by_connection(violations) -> None:
    """A database handle is created in one place.

    One Connection per configured connection, shared by every consumer that
    names it. Opening directly is how that is undone: the caller gets a second
    handle to the same database and neither holder knows about the other, which
    is the state this objectification removed.

    DBAdapter.get_connection is deliberately kept. Connection is built on it,
    so the rule names its single legitimate caller instead of deleting the
    mechanism -- a boundary, not a removal.

    Eleven production sites called it directly when this policy was declared
    and were migrated: file_loader, control, rey_loader, rey_db_admin,
    rey_console and console_next. The four outside this repository are guarded
    by their own tests, since this policy only reaches rey_lib.

    One exemption, and it is a deferred migration rather than an owner.
    file_loader's rejection writer resolves its connection from sql_configs
    rather than from any connection registry, so repointing it would change
    which record it uses -- a behaviour change, not a migration. It is the
    single remaining direct caller, it is expected to be removed rather than
    joined, and it is not evidence of the target architecture.
    """
    offenders = [
        f"{v.source_path}:{v.source_line} -> {v.callee}"
        for v in violations
        if v.rule_id == "connections_are_opened_only_by_connection"
    ]

    assert offenders == []


def test_the_exemption_list_names_only_the_implementation_layer() -> None:
    """One exemption, and it is the module that cannot route through itself.

    Guards the failure mode this policy is most likely to suffer: an exemption
    list quietly growing until the rule holds by construction.
    """
    rules = load_scan_rules(RULES_PATH)
    logging_rule = next(
        rule
        for rule in rules.rules_for("architecture_rules")
        if rule.rule_id == "logging_routes_through_the_logging_owner"
    )

    assert logging_rule.allowed_path_globs == ("rey_lib/logs/logging_setup.py",)


def test_the_test_suite_is_outside_the_logging_rule_scope() -> None:
    """A test asserting logging behaviour must construct a logger.

    That is not architectural drift, so tests are out of scope by path rather
    than by exemption — which keeps the exemption list meaningful.
    """
    rules = load_scan_rules(RULES_PATH)
    logging_rule = next(
        rule
        for rule in rules.rules_for("architecture_rules")
        if rule.rule_id == "logging_routes_through_the_logging_owner"
    )

    assert logging_rule.scope_path_globs == ("rey_lib/*",)


def test_the_logging_rule_forbids_the_logging_api_not_program_output() -> None:
    """print() is not a logging call and is deliberately outside this rule.

    Python logging is an event-recording system whose handlers route records to
    a configured destination; CLI output is a separate channel. The rule as
    first written forbade both, which would have moved folder_maker's summary
    off stdout. Pinned so print does not drift back in as a logging concern —
    whether library code may print at all is a separate policy question this
    rule does not answer.
    """
    rules = load_scan_rules(RULES_PATH)
    logging_rule = next(
        rule
        for rule in rules.rules_for("architecture_rules")
        if rule.rule_id == "logging_routes_through_the_logging_owner"
    )

    assert logging_rule.forbidden_target_globs == ("logging.getLogger",)


def test_cli_output_in_folder_maker_is_untouched() -> None:
    """_print_result still writes to stdout and stderr.

    Behavioural guard: the correction narrowed a rule rather than rewriting a
    CLI tool to satisfy it.
    """
    import inspect

    from rey_lib.installation import folder_maker

    source = inspect.getsource(folder_maker._print_result)

    assert source.count("print(") == 6
    assert "file=sys.stderr" in source

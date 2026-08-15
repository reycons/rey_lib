"""Focused tests for the migration manifest, retirement gate and STOP state.

Contract: rey_architecture_enforcement_layer.sgc.yaml (INC-006).

The gate's whole value is refusing a bad retirement, so every check is written
as a pair: the clean case that passes, and the specific fact that must stop it.
A gate proven only by passing has not been proven.

Each blocker here is a way a retired owner stays alive that the direct-call
check alone would miss — a registration id, a global publication, a template
entry point, or evidence too weak to prove anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map.migration import (
    BLOCKER_OLD_CALLER,
    BLOCKER_STALE_ENTRY_POINT,
    BLOCKER_STALE_PUBLICATION,
    BLOCKER_STALE_REGISTRATION,
    BLOCKER_STILL_REACHABLE,
    BLOCKER_UNPROVABLE,
    VERDICT_PROVEN,
    VERDICT_STOP,
    MigrationManifest,
    MigrationRow,
    load_migration_manifest,
    validate_migration_manifest,
    verify_retirement_ready,
)

MANIFEST = MigrationManifest(
    migration_id="retire_legacy_panel",
    capability="pane placement",
    old_owner_path_globs=("static/js/legacy_panel.js",),
    new_owner_path_globs=("frontend/src/panel_loader/*",),
    old_symbol_globs=("*LegacyPanel*",),
)


def _edge(source: str, to: str, kind: str = "call") -> dict:
    """Return one dependency-edge record."""
    return {
        "record_type": "dependency_edge",
        "record_id": f"edge:{source}:{to}",
        "source_path": source,
        "source_line": 12,
        "edge_kind": kind,
        "to": to,
    }


def _clean() -> list[dict]:
    """Return facts in which nothing reaches the retiring owner."""
    return [
        _edge("frontend/src/panel_loader/index.ts", "frontend/src/panel_view/index.ts"),
        {
            "record_type": "reachability",
            "record_id": "reach:static/js/legacy_panel.js",
            "target": "static/js/legacy_panel.js",
            "status": "unreferenced_candidate",
            "root": "",
        },
    ]


def test_a_clean_retirement_is_proven() -> None:
    """Nothing reaches the old owner and reachability is conclusive."""
    report = verify_retirement_ready(MANIFEST, _clean())

    assert report.verdict == VERDICT_PROVEN
    assert report.is_proven
    assert report.blockers == ()


def test_a_remaining_caller_stops_the_retirement() -> None:
    """The obvious case, and the only one a compiler would also catch."""
    report = verify_retirement_ready(
        MANIFEST, _clean() + [_edge("frontend/src/panel.ts", "static/js/legacy_panel.js")]
    )

    assert report.verdict == VERDICT_STOP
    assert not report.is_proven
    assert len(report.blockers_of(BLOCKER_OLD_CALLER)) == 1


def test_a_reference_by_symbol_name_stops_the_retirement() -> None:
    """An edge may name a symbol rather than a path; both retire together."""
    report = verify_retirement_ready(
        MANIFEST, _clean() + [_edge("frontend/src/panel.ts", "window.LegacyPanelHost.mount")]
    )

    assert report.blockers_of(BLOCKER_OLD_CALLER)


def test_the_old_owner_referring_to_itself_is_not_a_caller() -> None:
    """Internal references do not keep a module alive once nothing enters it."""
    report = verify_retirement_ready(
        MANIFEST,
        _clean() + [_edge("static/js/legacy_panel.js", "static/js/legacy_panel.js", "call")],
    )

    assert report.is_proven


def test_a_stale_registration_stops_the_retirement() -> None:
    """A caller deleted from source can survive as a registry id."""
    report = verify_retirement_ready(
        MANIFEST,
        _clean()
        + [
            {
                "record_type": "registration",
                "record_id": "reg:1",
                "source_path": "static/js/legacy_panel.js",
                "source_line": 5,
                "registry": "ReyEmbeddedHost",
                "registered_id": "legacy_panel",
                "implementation": "LegacyPanel",
                "registered_id_resolved": True,
            }
        ],
    )

    assert report.verdict == VERDICT_STOP
    assert report.blockers_of(BLOCKER_STALE_REGISTRATION)


def test_a_stale_publication_stops_the_retirement() -> None:
    """A global surface keeps a retired capability callable."""
    report = verify_retirement_ready(
        MANIFEST,
        _clean()
        + [
            {
                "record_type": "global_publication",
                "record_id": "pub:1",
                "source_path": "static/js/legacy_panel.js",
                "source_line": 3,
                "global": "window.LegacyPanel",
                "implementation": "LegacyPanel",
            }
        ],
    )

    assert report.blockers_of(BLOCKER_STALE_PUBLICATION)


def test_a_template_entry_point_stops_the_retirement() -> None:
    """A classic script loaded by a template has no import edge at all."""
    report = verify_retirement_ready(
        MANIFEST,
        _clean()
        + [
            {
                "record_type": "entry_point",
                "record_id": "entry:1",
                "source_path": "templates/index.html",
                "source_line": 40,
                "target": "static/js/legacy_panel.js",
                "entry_point_kind": "template_script",
                "window_or_host": "main",
            }
        ],
    )

    assert report.blockers_of(BLOCKER_STALE_ENTRY_POINT)


def test_still_being_reachable_stops_the_retirement() -> None:
    """Reachability is asked directly, not inferred from the absence of edges."""
    report = verify_retirement_ready(
        MANIFEST,
        [
            {
                "record_type": "reachability",
                "record_id": "reach:1",
                "target": "static/js/legacy_panel.js",
                "status": "definitely_reachable",
                "root": "templates/index.html",
            }
        ],
    )

    assert report.blockers_of(BLOCKER_STILL_REACHABLE)


@pytest.mark.parametrize("status", ["potentially_reachable", "reachability_unknown"])
def test_inconclusive_reachability_stops_rather_than_passes(status: str) -> None:
    """Absence of evidence is not evidence of absence.

    The weaker verdicts are the ones a retirement most wants to read as clean.
    They mean the scan could not decide, which is exactly when deleting is
    least safe.
    """
    report = verify_retirement_ready(
        MANIFEST,
        [
            {
                "record_type": "reachability",
                "record_id": "reach:1",
                "target": "static/js/legacy_panel.js",
                "status": status,
                "root": "",
            }
        ],
    )

    assert report.verdict == VERDICT_STOP
    assert report.blockers_of(BLOCKER_UNPROVABLE)


def test_an_unidentifiable_registration_stops_rather_than_passes() -> None:
    """A scanner that cannot follow a variable has not shown there is no caller.

    The protected behaviour: when nothing identifies a registration, it might
    be the retiring one and the gate must not pass.
    """
    report = verify_retirement_ready(
        MANIFEST,
        _clean()
        + [
            {
                "record_type": "registration",
                "record_id": "reg:2",
                "source_path": "frontend/src/other/index.ts",
                "source_line": 8,
                "registry": "ReyEmbeddedHost",
                "registered_id": "objectId",
                "implementation": "",
                "registered_id_resolved": False,
            }
        ],
    )

    assert report.verdict == VERDICT_STOP
    assert report.blockers_of(BLOCKER_UNPROVABLE)


def test_an_unrelated_dynamic_registration_does_not_block() -> None:
    """Narrowed after firing on real facts.

    rey_console has five registrations whose id is a variable, in modules with
    nothing to do with any given migration. Blocking on all of them made every
    retirement permanently unprovable, and a gate that can never pass protects
    nothing. Where the implementation is readable and names something else, the
    registration is ruled out on evidence.
    """
    report = verify_retirement_ready(
        MANIFEST,
        _clean()
        + [
            {
                "record_type": "registration",
                "record_id": "reg:3",
                "source_path": "frontend/src/json_viewer/JSONViewer.ts",
                "source_line": 567,
                "registry": "ReyEmbeddedHost",
                "registered_id": "jsonViewer",
                "implementation": "jsonViewer",
                "registered_id_resolved": False,
            }
        ],
    )

    assert report.is_proven


def test_the_report_records_what_it_examined() -> None:
    """A clean result must be distinguishable from a scan that saw nothing."""
    report = verify_retirement_ready(MANIFEST, _clean())

    assert report.examined["dependency_edge"] == 1
    assert report.examined["reachability"] == 1


def test_an_empty_fact_set_examines_nothing() -> None:
    """Proven with zero records examined is the shape of a false green.

    The verdict is still proven because nothing blocks, so the examined counts
    are what a caller must check. Recorded here so the limitation is explicit
    rather than discovered.
    """
    report = verify_retirement_ready(MANIFEST, [])

    assert report.is_proven
    assert sum(report.examined.values()) == 0


# The manifest. A row left undecided is how a value disappears during a move.


def test_a_complete_manifest_validates() -> None:
    """Every row carries a disposition and evidence."""
    manifest = MigrationManifest(
        migration_id="m",
        capability="c",
        old_owner_path_globs=("old/*",),
        new_owner_path_globs=("new/*",),
        rows=(
            MigrationRow(
                name="definition.installation",
                disposition="preserved",
                evidence="read at the new call site",
                new_source="mounted.definition.installation",
            ),
            MigrationRow(
                name="retry handling",
                kind="capability",
                disposition="replaced_by_canonical_capability",
                evidence="RunDispatcher owns retries",
            ),
        ),
    )

    assert validate_migration_manifest(manifest) == []


def test_a_row_without_a_disposition_blocks_implementation() -> None:
    """REQ-248: a migration cannot start with unresolved rows."""
    manifest = MigrationManifest(
        migration_id="m",
        capability="c",
        old_owner_path_globs=("old/*",),
        new_owner_path_globs=("new/*",),
        rows=(MigrationRow(name="placement target"),),
    )

    problems = validate_migration_manifest(manifest)

    assert any("no disposition" in problem for problem in problems)


def test_a_disposition_without_evidence_blocks_implementation() -> None:
    """Claiming a value is obsolete is not the same as showing it."""
    manifest = MigrationManifest(
        migration_id="m",
        capability="c",
        old_owner_path_globs=("old/*",),
        new_owner_path_globs=("new/*",),
        rows=(MigrationRow(name="x", disposition="proven_obsolete"),),
    )

    assert any("no evidence" in problem for problem in validate_migration_manifest(manifest))


def test_a_preserved_row_must_name_its_new_source() -> None:
    """A preserved value has to come from somewhere after the move."""
    manifest = MigrationManifest(
        migration_id="m",
        capability="c",
        old_owner_path_globs=("old/*",),
        new_owner_path_globs=("new/*",),
        rows=(MigrationRow(name="x", disposition="preserved", evidence="still needed"),),
    )

    assert any("names no new source" in problem for problem in validate_migration_manifest(manifest))


def test_an_unknown_disposition_is_refused() -> None:
    """The four dispositions are the vocabulary; a fifth is a typo."""
    manifest = MigrationManifest(
        migration_id="m",
        capability="c",
        old_owner_path_globs=("old/*",),
        new_owner_path_globs=("new/*",),
        rows=(MigrationRow(name="x", disposition="probably_fine", evidence="hand wave"),),
    )

    assert any("unknown disposition" in problem for problem in validate_migration_manifest(manifest))


def test_a_manifest_loads_from_its_declaration(tmp_path: Path) -> None:
    """The manifest is data, declared before code changes."""
    path = tmp_path / "m.migration.yaml"
    path.write_text(
        "migration:\n"
        "  migration_id: retire_thing\n"
        "  capability: pane placement\n"
        "  old_owner_path_globs: ['static/js/old.js']\n"
        "  new_owner_path_globs: ['frontend/src/new/*']\n"
        "  rows:\n"
        "    - name: host element\n"
        "      disposition: moved_to_new_owner\n"
        "      evidence: PanelLoader acquires it\n",
        encoding="utf-8",
    )

    manifest = load_migration_manifest(path)

    assert manifest.migration_id == "retire_thing"
    assert manifest.old_owner_path_globs == ("static/js/old.js",)
    assert validate_migration_manifest(manifest) == []


def test_a_manifest_missing_a_required_field_is_refused(tmp_path: Path) -> None:
    """An incomplete declaration is not a migration."""
    path = tmp_path / "m.yaml"
    path.write_text("migration:\n  migration_id: x\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing 'capability'"):
        load_migration_manifest(path)


def test_a_missing_manifest_raises_rather_than_guessing(tmp_path: Path) -> None:
    """The path is configuration and is never substituted."""
    with pytest.raises(FileNotFoundError):
        load_migration_manifest(tmp_path / "absent.yaml")

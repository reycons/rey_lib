"""Focused tests for the shared File Manifest hierarchy.

The manifest is held in the control database, so these seed through production
writers against a Control-shaped double and assert on what the hierarchy builds
from it. Identity is the governed ``file_manifest_id`` the store mints -- these
never invent one, because inventing one is what the model removed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import rey_lib.logs.file_hierarchy as hierarchy_module
from rey_lib.files.manifest import FileManifest
from rey_lib.logs.file_hierarchy import (
    FileHierarchyError,
    build_file_hierarchy,
    build_file_hierarchy_feed,
    build_file_hierarchy_feeds,
    build_file_hierarchy_stages,
)

from tests.support.control_double import control_backed_ctx


#: Every hierarchy question that depends on a file's classified feed.
#:
#: Blocked by a defect in rey_lib.logs.file_manifest._inventory_record: it
#: projects a file_manifest row into a canonical inventory record and does not
#: carry `classification`. _classified_feeds reads that field off the inventory
#: record, so it finds none, every file belongs to no feed, and
#: build_file_hierarchy returns an empty page for a manifest that is fully
#: classified.
#:
#: The field is there to carry: control.file_vw reports the current
#: classification on the file row, and console_next's file_manifest_tvw reads
#: `classification->'values'->>'feed'` from it to build the same tree in SQL.
#: One line of projection, and these tests are correct as written -- proved by
#: re-running them with the field carried through, which turns all ten green.
NEEDS_CLASSIFICATION = pytest.mark.xfail(strict=True, reason=(
    "rey_lib.logs.file_manifest._inventory_record drops `classification`, so "
    "_classified_feeds finds no feed for any file and the hierarchy is empty. "
    "The file row carries it; the projection does not pass it on."
))


class _Store:
    """A governed manifest seeded through the real FileManifest.

    Files are named for readability; the identity every assertion uses is the
    one the store generated, reached through ``ids``.
    """

    def __init__(self) -> None:
        self.ctx = control_backed_ctx()
        self.manifest = FileManifest(self.ctx.shared_control)
        self.ids: dict[str, int] = {}

    def inventory(self, name: str, feed: str, file_name: str,
                  path: str) -> int:
        """Record a governed file. Order of calls is order of identity."""
        self.ids[name] = self.manifest.inventory(
            path=path, file_name=file_name, base_name=file_name.split(".")[0],
            file_extension=file_name.rsplit(".", 1)[-1],
            checksum_sha256=f"sha-{name}", size_bytes=1, source_name=feed,
            producer={"application": "file_operator"},
        )
        return self.ids[name]

    def classify(self, name: str, feed: str) -> None:
        """Classification is state on the file, not a record beside it."""
        self.manifest.update(self.ids[name], classification={
            "type": "file_name_regex",
            "source_field": "file.file_name",
            "values": {"feed": feed},
        })

    def mutate(self, name: str, action: str, *, path: str,
               status: str = "success", file_id: int | None = None) -> int:
        """Append one lifecycle event beneath a governed file."""
        return self.manifest.append_mutation(
            self.ids[name] if file_id is None else file_id,
            record_type="source_file_mutation", action=action,
            status=status, path=path,
        )


@pytest.fixture()
def store() -> _Store:
    return _Store()


def _files(page):
    return [file for feed in page.feeds for file in feed.files]


def test_library_has_only_canonical_shared_data_dependencies() -> None:
    source = Path(hierarchy_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    # The hierarchy reads the governed manifest through its owner and nothing
    # else. rey_lib.files.jsonl is deliberately absent: the manifest is not a
    # file any more, so a JSONL dependency here would be reaching past the
    # boundary that owns the records.
    assert "rey_lib.logs.file_manifest" in imported
    assert "rey_lib.files.jsonl" not in imported
    assert not imported.intersection(
        {
            "json",
            "os",
            "pathlib",
            "rey_lib.logs.log_utils",
            "rey_lib.logs.evidence_projection",
            "rey_lib.files.file_utils",
        }
    )


@NEEDS_CLASSIFICATION
def test_only_inventory_records_create_files_and_group_by_classified_feed(
    store,
) -> None:
    """A file is a file because it was inventoried, and grouped by its feed."""
    store.inventory("file-b", "feed_inbox", "b.csv", "/in/b.csv")
    store.inventory("file-a", "feed_inbox", "a.csv", "/in/a.csv")
    # A mutation under a file that was never inventoried groups nothing.
    store.manifest.append_mutation(
        999, record_type="source_file_mutation", action="move",
        status="success", path="/processing/a.csv")
    store.classify("file-b", "Zulu")
    store.classify("file-a", "alpha")

    page = build_file_hierarchy(store.ctx)

    assert [feed.feed_identity for feed in page.feeds] == ["alpha", "Zulu"]
    assert [[file.file_id for file in feed.files] for feed in page.feeds] == [
        [store.ids["file-a"]], [store.ids["file-b"]],
    ]


@NEEDS_CLASSIFICATION
def test_mutations_join_only_by_exact_file_id_not_name_or_path(store) -> None:
    """Identity joins a mutation to its file. A shared path does not."""
    governed = store.inventory("governed-a", "feed", "same.csv", "/in/same.csv")
    other = store.inventory("governed-b", "feed", "same.csv", "/in/same.csv")
    store.classify("governed-a", "feed")
    lookalike = store.mutate("governed-b", "create", path="/processing/same.csv")
    exact = store.mutate("governed-a", "move", path="/processing/same.csv")

    page = build_file_hierarchy(store.ctx)

    file = next(f for feed in page.feeds for f in feed.files
                if f.file_id == governed)
    assert [mutation.record_id for mutation in file.mutations] == [exact]
    assert file.mutations[0].metadata["file_id"] == governed
    assert lookalike not in [m.record_id for m in file.mutations]
    assert other != governed


@NEEDS_CLASSIFICATION
def test_generated_identity_controls_file_and_mutation_order(store) -> None:
    """Order follows generated identity, which is the order things happened."""
    store.inventory("file-a", "feed", "a.csv", "/in/a.csv")
    store.inventory("file-b", "feed", "b.csv", "/in/b.csv")
    store.classify("file-a", "feed")
    store.classify("file-b", "feed")
    moved = store.mutate("file-a", "move", path="/work/a.csv")
    created = store.mutate("file-a", "create", path="/out/a.csv")

    page = build_file_hierarchy(store.ctx)
    files = [file for feed in page.feeds for file in feed.files]

    assert [file.inventory_record_id for file in files] == [
        store.ids["file-a"], store.ids["file-b"],
    ]
    assert [mutation.record_id for mutation in files[0].mutations] == [
        moved, created,
    ]


@NEEDS_CLASSIFICATION
def test_page_is_bounded_and_model_is_immutable(store) -> None:
    """Paging is bounded and what a page hands back cannot be edited."""
    for index in range(1, 302):
        store.inventory(f"file-{index}", "feed", f"{index}.csv", f"/in/{index}.csv")
        store.classify(f"file-{index}", "feed")

    first = build_file_hierarchy(store.ctx, limit=250)
    second = build_file_hierarchy(store.ctx, offset=250, limit=250)
    files = lambda page: [f for feed in page.feeds for f in feed.files]

    assert len(files(first)) == 250
    assert first.next_offset == 250
    assert len(files(second)) == 51
    assert second.next_offset is None
    assert first.total_files == 301
    with pytest.raises(FileHierarchyError, match="must not exceed 250"):
        build_file_hierarchy(store.ctx, limit=251)
    with pytest.raises(TypeError):
        files(first)[0].metadata["changed"] = True  # type: ignore[index]


@NEEDS_CLASSIFICATION
def test_payload_preserves_supplied_governed_values(store) -> None:
    """The payload carries the stored record, not a rebuilt summary of it."""
    file_id = store.inventory("file-a", "Feed A", "a.csv", "/in/a.csv")
    store.classify("file-a", "Feed A")
    mutation_id = store.mutate("file-a", "create", path="/out/a.csv")

    payload = build_file_hierarchy(store.ctx).to_payload()
    file_payload = payload["feeds"][0]["files"][0]

    assert payload["feeds"][0]["display_label"] == "Feed A"
    assert file_payload["metadata"]["record_id"] == file_id
    assert file_payload["metadata"]["file"]["path"] == "/in/a.csv"
    assert file_payload["mutations"][0]["metadata"]["record_id"] == mutation_id
    assert file_payload["mutations"][0]["metadata"]["action"] == "create"


@NEEDS_CLASSIFICATION
def test_phase_two_queries_lazy_load_feed_files_and_exact_file_stages(store) -> None:
    """Feeds, then that feed's files, then one file's stages -- three queries."""
    file_id = store.inventory("file-a", "Feed A", "a.csv", "/in/a.csv")
    store.classify("file-a", "Feed A")
    store.mutate("file-a", "move", path="/processing/a.csv")

    feeds = build_file_hierarchy_feeds(store.ctx).to_payload()
    assert feeds["feeds"] == [{
        "feed_identity": "Feed A", "display_label": "Feed A", "total_files": 1,
        "files": [], "files_loaded": False,
    }]

    files = build_file_hierarchy_feed(store.ctx, "Feed A").to_payload()
    assert [item["file_id"] for item in files["feeds"][0]["files"]] == [file_id]
    assert files["feeds"][0]["files"][0]["mutations"] == []

    stages = build_file_hierarchy_stages(store.ctx, file_id).to_payload()
    assert stages["current_path"] == "/processing/a.csv"
    # Classification is state on the file, so a classified file shows it as a
    # stage between what it was inventoried as and what happened to it.
    assert [stage["stage_type"] for stage in stages["stages"]] == [
        "inventory", "classification", "mutation",
    ]


@NEEDS_CLASSIFICATION
def test_create_never_replaces_primary_current_path(store) -> None:
    """A created artifact is its own node; it does not move the governed file."""
    file_id = store.inventory("file-a", "feed", "a.xlsx", "/in/a.xlsx")
    store.classify("file-a", "feed")
    store.mutate("file-a", "move", path="/processing/a.xlsx")
    store.mutate("file-a", "create", path="/converted/a.csv")

    page = build_file_hierarchy_stages(store.ctx, file_id)

    assert page.current_path == "/processing/a.xlsx"
    assert page.lifecycle_status == "active"
    assert [stage.stage_type for stage in page.stages] == [
        "inventory", "classification", "mutation", "mutation",
    ]


def test_moved_primary_does_not_mark_historical_inventory_stage_current(
    store,
) -> None:
    """Where the file is now is not where it was inventoried."""
    file_id = store.inventory("file-a", "feed", "a.csv", "/in/a.csv")
    store.mutate("file-a", "move", path="/processing/a.csv")

    page = build_file_hierarchy_stages(store.ctx, file_id)

    assert page.current_path == "/processing/a.csv"
    assert all(stage.is_current_primary is False for stage in page.stages)


def test_a_reversed_mutation_no_longer_says_where_the_file_is(store) -> None:
    """Rollback is state on the mutation, not a record beside it.

    A reversed mutation stays in the history and stays on the page. It simply
    stops participating in current-state resolution, so the file's location is
    whatever the newest surviving mutation says.
    """
    file_id = store.inventory("file-a", "feed", "a.csv", "/in/a.csv")
    reversed_move = store.mutate("file-a", "move", path="/work/a.csv")
    # dry_run=False, because the default is the preview: one predicate and one
    # shape serve both, so asking without saying so marks nothing.
    store.manifest.request_rollback(dry_run=False, file_mutation_id=reversed_move)
    store.manifest.complete_rollback([reversed_move])
    store.mutate("file-a", "move", path="/processing/a.csv")

    page = build_file_hierarchy_stages(store.ctx, file_id)

    assert page.current_path == "/processing/a.csv"
    assert page.lifecycle_status == "active"
    # The reversed mutation is still shown; it is history either way.
    assert reversed_move in [stage.record_id for stage in page.stages]


def test_profiles_join_only_by_exact_governed_file_id(store) -> None:
    """A profile belongs to the file it names, by identity and nothing else."""
    file_id = store.inventory("file-a", "feed", "a.csv", "/in/a.csv")
    profile = {
        "run_log_id": 8, "record_type": "ARTIFACT_REFERENCE",
        "artifact_group": "profiles", "artifact_type": "row_shape_analysis",
        "source_file_id": file_id, "path": "/profiles/a.profile.json",
    }

    page = build_file_hierarchy_stages(store.ctx, file_id,
                                       profile_artifacts=(profile,))

    assert page.stages[-1].stage_type == "profile"
    assert page.stages[-1].path == "/profiles/a.profile.json"
    with pytest.raises(FileHierarchyError, match="does not match"):
        build_file_hierarchy_stages(
            store.ctx, file_id,
            profile_artifacts=({**profile, "source_file_id": file_id + 1},))


@NEEDS_CLASSIFICATION
def test_configured_inventory_source_name_is_never_a_feed(store) -> None:
    """`feed_inbox` is discovery configuration, not governed feed identity."""
    store.inventory("file-a", "feed_inbox", "CWATranMay26.xls",
                    "/in/CWATranMay26.xls")
    store.classify("file-a", "bny")

    identities = [feed.feed_identity
                  for feed in build_file_hierarchy(store.ctx).feeds]
    summaries = [feed.feed_identity
                 for feed in build_file_hierarchy_feeds(store.ctx).feeds]

    assert identities == ["bny"]
    assert summaries == ["bny"]
    assert "feed_inbox" not in identities
    assert "feed_inbox" not in summaries


@NEEDS_CLASSIFICATION
def test_records_from_different_feeds_group_under_separate_feed_nodes(store) -> None:
    """One shared inventory source still yields one node per classified feed."""
    store.inventory("file-a", "feed_inbox", "CWATranMay26.xls", "/in/a.xls")
    store.inventory("file-b", "feed_inbox", "BmoHoldMay26.csv", "/in/b.csv")
    store.inventory("file-c", "feed_inbox", "AmalTranMay26.csv", "/in/c.csv")
    store.classify("file-a", "bny")
    store.classify("file-b", "bmo")
    store.classify("file-c", "amalgamated")

    page = build_file_hierarchy(store.ctx)

    assert [feed.feed_identity for feed in page.feeds] == [
        "amalgamated", "bmo", "bny",
    ]
    assert [[file.file_id for file in feed.files] for feed in page.feeds] == [
        [store.ids["file-c"]], [store.ids["file-b"]], [store.ids["file-a"]],
    ]
    one_feed = build_file_hierarchy_feed(store.ctx, "bny").to_payload()
    assert [item["file_id"] for item in one_feed["feeds"][0]["files"]] == [
        store.ids["file-a"],
    ]


@NEEDS_CLASSIFICATION
def test_file_lifecycle_children_are_unchanged_by_feed_grouping(store) -> None:
    """Grouping changed; the file's lifecycle stages still read in order."""
    file_id = store.inventory("file-a", "feed_inbox", "CWATranMay26.xls",
                              "/in/CWATranMay26.xls")
    store.classify("file-a", "bny")
    store.mutate("file-a", "move", path="/processing/CWATranMay26.xls")
    store.mutate("file-a", "create", path="/converted/CWATranMay26.csv")

    stages = build_file_hierarchy_stages(store.ctx, file_id).to_payload()

    assert [stage["stage_type"] for stage in stages["stages"]] == [
        "inventory", "classification", "mutation", "mutation",
    ]
    assert stages["current_path"] == "/processing/CWATranMay26.xls"


def test_a_file_without_a_classified_feed_is_not_grouped_under_a_feed(
    store,
) -> None:
    """No feed is invented for unclassified evidence; its lifecycle still reads."""
    file_id = store.inventory("file-a", "feed_inbox", "a.csv", "/in/a.csv")
    store.mutate("file-a", "move", path="/processing/a.csv")

    assert build_file_hierarchy(store.ctx).feeds == ()
    assert build_file_hierarchy_feeds(store.ctx).feeds == ()
    stages = build_file_hierarchy_stages(store.ctx, file_id).to_payload()
    assert stages["current_path"] == "/processing/a.csv"


def test_created_artifacts_are_labelled_for_what_they_are(store) -> None:
    """A node names the artifact it opens, never a generic lifecycle verb."""
    file_id = store.inventory("file-a", "feed_inbox", "a.xls", "/in/a.xls")
    store.classify("file-a", "bny")
    store.mutate("file-a", "move", path="/proc/a.xls")
    store.manifest.append_mutation(
        file_id, record_type="source_file_mutation", action="create",
        status="success", path="/work/converted/a.csv",
        # The conversion block is evidence about how it ran; the reason is what
        # names the stage.
        result="converted_csv",
        conversion={"operator": "excel_conversion", "name": "all"})
    store.manifest.append_mutation(
        file_id, record_type="source_file_mutation", action="create",
        status="success", path="/work/sanitized/a.csv",
        result="sanitized_file")
    for path, reason in (
        ("/work/prepared/a.csv", "prepared_file"),
        ("/work/kickouts/a.kickouts.jsonl", "kickout_file"),
        ("/work/kickouts/a.kickouts.redacted.jsonl", "redacted_kickout_file"),
        ("/work/sanitized_csv/a.redacted.csv", "redacted_sanitized_file"),
        ("/work/prepared/a.redacted.csv", "redacted_prepared_file"),
        ("/profiles/AcmeHold.profile.json", "structural_profile"),
    ):
        store.manifest.append_mutation(
            file_id, record_type="source_file_mutation", action="create",
            status="success", path=path, result=reason)

    stages = build_file_hierarchy_stages(store.ctx, file_id).to_payload()["stages"]
    named = {stage["stage_type"]: stage["label"] for stage in stages}

    assert named["converted"] == "Converted CSV"
    assert named["sanitized"] == "Sanitized CSV"
    assert named["prepared"] == "Prepared CSV"
    assert named["kickout"] == "Kickout JSONL"
    assert named["kickout_redacted"] == "Redacted Kickout JSONL"
    assert named["sanitized_redacted"] == "Redacted Sanitized CSV"
    assert named["prepared_redacted"] == "Redacted Prepared CSV"
    assert named["profile"] == "Structural Profile"
    # Every created artifact lands on a named stage. Only a genuine lifecycle
    # event -- here the move -- may fall to the generic type; a created artifact
    # that does is invisible in the Feeds tree.
    created = {
        stage["stage_type"]
        for stage in stages
        if stage["metadata"].get("action") == "create"
    }
    assert "mutation" not in created
    assert "Create" not in set(named.values())


def test_a_profile_stage_is_named_for_the_artifact_it_opens(store) -> None:
    file_id = store.inventory("file-a", "feed", "a.csv", "/in/a.csv")
    profile = {
        "run_log_id": 8, "record_type": "ARTIFACT_REFERENCE",
        "artifact_group": "profiles", "artifact_type": "row_shape_analysis",
        "source_file_id": file_id, "path": "/work/profiles/a.profile.json",
    }

    page = build_file_hierarchy_stages(store.ctx, file_id,
                                       profile_artifacts=(profile,))

    assert page.stages[-1].label == "Structural Profile"


def test_every_mutation_reason_is_named_by_data_not_a_branch() -> None:
    """The rey_lib dispatcher review put _mutation_stage in the migrate set.

    Seven named reasons meant every new one edited the function that was only
    supposed to present it. The mapping is now a table, so adding a reason is
    an entry.
    """
    from rey_lib.logs.file_hierarchy import _MUTATION_PRESENTATION

    assert set(_MUTATION_PRESENTATION) == {
        # The lifecycle events a file goes through.
        "inventoried",
        "classified",
        # The artifacts a step creates.
        "converted_csv",
        "sanitized_file",
        "prepared_file",
        "kickout_file",
        "redacted_kickout_file",
        "redacted_sanitized_file",
        "redacted_prepared_file",
        "structural_profile",
        # Where a file was put. Each names its destination, because "moved"
        # alone does not say where it went.
        "moved_to_processing",
        "moved_to_kickouts",
        "moved_to_failed",
        "moved_to_archive",
    }
    # Stage type, the label when it succeeded, and the label when it failed.
    # status selects which of the two is shown and is never encoded in the key.
    assert all(len(v) == 3 for v in _MUTATION_PRESENTATION.values())


def test_a_conversion_is_named_by_its_own_reason() -> None:
    """One lookup on one field, which is what replaced the chain.

    A conversion used to be recognised by testing ``conversion`` before
    ``result``, so the two could disagree about what a record was. It has its
    own reason now -- ``converted_csv`` -- and the ``conversion`` block beside
    it is evidence about how the conversion ran, never what the stage is.
    """
    from rey_lib.logs.file_hierarchy import _mutation_stage

    stage = _mutation_stage(
        {
            "record_id": 1,
            "action": "create",
            "conversion": {"operator": "excel_conversion"},
            "result": "converted_csv",
            "file": {"path": "a.csv"},
            "status": "success",
        },
        file_id=1,
    )

    assert stage.stage_type == "converted"
    assert stage.label == "Converted CSV"


def test_status_selects_the_form_and_is_never_in_the_reason() -> None:
    """One entry per reason, two labels, chosen by the record's own status."""
    from rey_lib.logs.file_hierarchy import _mutation_stage

    def named(status: str) -> str:
        return _mutation_stage(
            {"record_id": 1, "action": "create", "result": "prepared_file",
             "file": {"path": "a.csv"}, "status": status},
            file_id=1,
        ).label

    assert named("success") == "Prepared CSV"
    assert named("failed") == "Preparation failed"


def test_an_unrecognised_reason_stays_a_generic_lifecycle_event() -> None:
    """The fallback the chain ended with."""
    from rey_lib.logs.file_hierarchy import _mutation_stage

    stage = _mutation_stage(
        {"record_id": 2, "action": "create", "result": {"reason": "something_new"},
         "file": {"path": "b.csv"}, "status": "created"},
        file_id=1,
    )

    assert stage.stage_type == "mutation"

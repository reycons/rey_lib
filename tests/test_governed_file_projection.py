"""What the governed file projection guarantees, and what it must not repeat.

Three defects the storage move introduced, each found by running real consumers
against real records rather than around them. They are pinned here directly
because the consumers only cover them incidentally.
"""

from __future__ import annotations

import pytest

from rey_lib.files.manifest import FileManifest
from rey_lib.logs.file_hierarchy import FileHierarchyError, build_file_hierarchy
from rey_lib.logs.file_manifest import read_records_from_control

from tests.support.control_double import control_backed_ctx


#: The same defect the file-hierarchy suite is blocked on.
#:
#: rey_lib.logs.file_manifest._inventory_record does not carry `classification`
#: into the inventory record, so _classified_feeds finds no feed and
#: build_file_hierarchy returns an empty page. Both of these read the built
#: hierarchy, so both see nothing to assert against.
NEEDS_CLASSIFICATION = pytest.mark.xfail(strict=True, reason=(
    "rey_lib.logs.file_manifest._inventory_record drops `classification`, so "
    "the built hierarchy is empty and there is no projected file to assert on."
))


@pytest.fixture()
def seeded():
    """One governed file, classified, with one mutation beneath it."""
    ctx = control_backed_ctx()
    manifest = FileManifest(ctx.shared_control)
    file_id = manifest.inventory(
        path="/in/a.csv", file_name="a.csv", base_name="a",
        file_extension="csv", checksum_sha256="sha-a", size_bytes=1,
        source_name="feed",
    )
    manifest.update(file_id, classification={
        "type": "file_name_regex", "source_field": "file.file_name",
        "values": {"feed": "feed"},
    })
    mutation_id = manifest.append_mutation(
        file_id, record_type="source_file_mutation", action="move",
        status="success", path="/work/a.csv",
    )
    return ctx, manifest, file_id, mutation_id


@NEEDS_CLASSIFICATION
def test_a_governed_identity_is_an_integer(seeded) -> None:
    """The identity is the generated key, carried as itself.

    It is never rendered as a string on the way through: a second
    representation is how one object ends up with two identities.
    """
    ctx, _manifest, file_id, _mutation_id = seeded

    page = build_file_hierarchy(ctx)
    files = [file for feed in page.feeds for file in feed.files]

    assert files[0].file_id == file_id
    assert isinstance(files[0].file_id, int)


@NEEDS_CLASSIFICATION
def test_a_file_and_a_mutation_may_share_a_number(seeded) -> None:
    """Identity is unique within its kind, not across kinds.

    ``file_manifest_id`` and ``file_mutation_id`` are separate generated keys,
    so the same number names both a file and some mutation. Only a collision
    inside one kind is a duplicate.
    """
    ctx, manifest, first_file, move_id = seeded
    # A second file whose identity is the number an existing mutation already
    # holds -- the collision the two sequences make inevitable.
    second_file = manifest.inventory(
        path="/in/b.csv", file_name="b.csv", base_name="b",
        file_extension="csv", checksum_sha256="sha-b", size_bytes=1,
        source_name="feed",
    )
    manifest.update(second_file, classification={
        "type": "file_name_regex", "source_field": "file.file_name",
        "values": {"feed": "feed"},
    })
    assert second_file == move_id, "the fixture must actually collide"

    records = read_records_from_control(ctx)
    kinds = {(record["record_type"], record["record_id"]) for record in records}

    assert ("source_file_inventory", second_file) in kinds
    assert ("source_file_mutation", move_id) in kinds
    # The consumer builds from them rather than refusing them.
    files = [file for feed in build_file_hierarchy(ctx).feeds
             for file in feed.files]
    assert sorted(file.file_id for file in files) == [first_file, second_file]


def test_a_real_duplicate_within_one_kind_is_still_refused(seeded) -> None:
    """The check was narrowed to the kind, not removed."""
    ctx, _manifest, file_id, _mutation_id = seeded
    duplicate = dict(ctx.shared_control.files[0])
    ctx.shared_control.files.append(duplicate)

    with pytest.raises(FileHierarchyError, match="is duplicated"):
        build_file_hierarchy(ctx)


class TestTheBaselineIsRecordedOnce:
    """Recording a file writes its baseline mutation. It is one fact."""

    def test_the_projection_does_not_repeat_it(self, seeded) -> None:
        """The file record already says the file was inventoried."""
        ctx, _manifest, file_id, _mutation_id = seeded

        inventories = [record for record in read_records_from_control(ctx)
                       if record["record_type"] == "source_file_inventory"]

        assert [record["record_id"] for record in inventories] == [file_id]

    def test_history_still_carries_it(self, seeded) -> None:
        """History is where the baseline belongs, and it is first."""
        ctx, manifest, file_id, _mutation_id = seeded

        history = manifest.history(file_id)

        assert [row["record_type"] for row in history] == [
            "source_file_inventory", "source_file_mutation",
        ]
        assert history[0]["action"] == "record_only"

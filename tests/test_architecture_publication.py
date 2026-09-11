"""The authored architecture, as rows the code schema can publish.

It serializes; it does not resolve. Which file or symbol a reference names is
``code.f_realization_resolution``'s answer, enforced by ``code.p_publish``, and
restating it here would be a second implementation of one rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.repository_map.architecture_projection import ArchitectureProjectionError
from rey_lib.repository_map.architecture_publication import architecture_rows


def _written(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "01_core_architecture.yaml"
    path.write_text(body, encoding="utf-8")
    return path


NESTED = """
architecture_concepts:
  concepts:
    - key: console
      label: Console
      statement: The reader's surface.
      realized_by:
        - frontend/src/explorer_panel/
        - rey_console/routes.py
      concepts:
        - key: explorer
          label: Explorer
          statement: How a reader navigates.
          realized_by:
            - rey_console.tree_definitions.TreeTabs
    - key: apps
      label: Apps
      statement: The applications.
"""


class TestTheConceptTree:
    """Dotted keys, declared order, and a domain naming no parent."""

    def test_a_concept_key_is_its_dotted_ancestor_path(self, tmp_path: Path) -> None:
        """That path is code.concept's stable identity.

        The surrogate ``concept_id`` is regenerated on every publication, so
        nothing may lean on it -- which is why the staging tables name a parent
        by key rather than by id.
        """
        rows = architecture_rows(_written(tmp_path, NESTED))

        assert [c["concept_key"] for c in rows.concepts] == [
            "console", "console.explorer", "apps",
        ]

    def test_a_domain_names_the_empty_string_as_its_parent(
        self, tmp_path: Path
    ) -> None:
        """Empty, not None: the column is NOT NULL and the schema compares to ''."""
        rows = architecture_rows(_written(tmp_path, NESTED))
        parents = {c["concept_key"]: c["parent_concept_key"] for c in rows.concepts}

        assert parents["console"] == ""
        assert parents["apps"] == ""
        assert parents["console.explorer"] == "console"

    def test_declared_order_is_sort_order_among_siblings(
        self, tmp_path: Path
    ) -> None:
        """Nothing is sorted. The document's order is the display order."""
        rows = architecture_rows(_written(tmp_path, NESTED))
        order = {c["concept_key"]: c["sort_order"] for c in rows.concepts}

        assert order["console"] == 0
        assert order["apps"] == 1
        assert order["console.explorer"] == 0

    def test_a_missing_label_falls_back_to_the_key(self, tmp_path: Path) -> None:
        rows = architecture_rows(
            _written(tmp_path, "architecture_concepts:\n  concepts:\n    - key: bare\n")
        )

        assert rows.concepts[0]["label"] == "bare"

    def test_a_statement_is_never_None(self, tmp_path: Path) -> None:
        """``code.concept.statement`` is NOT NULL.

        A concept with no authored prose is a real state and stores as the
        empty string; None would fail at the insert.
        """
        rows = architecture_rows(
            _written(tmp_path, "architecture_concepts:\n  concepts:\n    - key: bare\n")
        )

        assert rows.concepts[0]["statement"] == ""

    def test_a_concept_without_a_key_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ArchitectureProjectionError):
            architecture_rows(
                _written(tmp_path, "architecture_concepts:\n  concepts:\n    - label: no key\n")
            )

    def test_a_document_declaring_no_concepts_is_refused(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ArchitectureProjectionError):
            architecture_rows(_written(tmp_path, "architecture_concepts:\n  concepts: []\n"))


class TestRealizationsAreStagedAsAuthored:
    """The reference is a string until the schema resolves it."""

    def test_every_reference_is_carried_verbatim(self, tmp_path: Path) -> None:
        """Compared as a set: row order carries no meaning at staging.

        ``sort_order`` is the ordering fact, per concept. The emission order
        follows the traversal -- a concept's sub-concepts are walked before its
        own references, exactly as the JSONL derivation walks them -- and
        asserting that sequence would pin an incidental detail.
        """
        rows = architecture_rows(_written(tmp_path, NESTED))

        assert {(r["concept_key"], r["reference"]) for r in rows.realizations} == {
            ("console", "frontend/src/explorer_panel/"),
            ("console", "rey_console/routes.py"),
            ("console.explorer", "rey_console.tree_definitions.TreeTabs"),
        }

    def test_nothing_is_resolved(self, tmp_path: Path) -> None:
        """A directory, a file and a dotted symbol are three shapes here.

        They are three *kinds* only after ``f_realization_resolution`` says so.
        A ``match_kind`` decided in Python would be the rule stated twice.
        """
        rows = architecture_rows(_written(tmp_path, NESTED))

        for row in rows.realizations:
            assert set(row) == {"concept_key", "reference", "sort_order"}

    def test_reference_order_is_preserved_per_concept(self, tmp_path: Path) -> None:
        rows = architecture_rows(_written(tmp_path, NESTED))
        console = [r for r in rows.realizations if r["concept_key"] == "console"]

        assert [r["sort_order"] for r in console] == [0, 1]

    def test_a_concept_naming_nothing_contributes_no_rows(
        self, tmp_path: Path
    ) -> None:
        """And no empty container either. That is a tree decision, not a fact."""
        rows = architecture_rows(_written(tmp_path, NESTED))

        assert not [r for r in rows.realizations if r["concept_key"] == "apps"]

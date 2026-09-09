"""The architecture projection: an authored concept tree, joined to exact evidence.

What these prove is mostly what the projection *refuses*. A join that emitted
its best guess would be worse than no artifact at all: a consumer cannot tell a
resolved reference from a nearly-resolved one, and the whole point is that a
node's presence means something.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from rey_lib.repository_map import (
    ArchitectureProjection,
    ArchitectureProjectionError,
    build_architecture_projection,
    system_membership,
    validate_architecture_projection,
)
from rey_lib.repository_map.architecture_projection import (
    CONSUMED_OWNERSHIP_SECTIONS,
    NODE_TYPE_CONCEPT,
    NODE_TYPE_MODULE,
    NODE_TYPE_SYMBOL,
    dotted_identity,
)
from rey_lib.repository_map.writer import RepositoryMap


def _map(*records: dict[str, Any], head: str = "abc123", content: str = "hash-1") -> RepositoryMap:
    """Return a parsed map with the header fields the projection reads."""
    return RepositoryMap(
        header={"head_commit": head, "content_hash": content},
        records=list(records),
    )


def _file(path: str) -> dict[str, Any]:
    return {"record_type": "file", "record_id": f"file:{path}", "path": path}


def _symbol(
    path: str,
    name: str,
    *,
    owner: str = "",
    kind: str = "function",
    line: int = 1,
    exported: bool = True,
) -> dict[str, Any]:
    qualified = f"{owner}.{name}" if owner else name
    return {
        "record_type": "symbol",
        "record_id": f"symbol:{path}:{name}:{line}:0",
        "source_path": path,
        "source_line": line,
        "source_column": 0,
        "name": name,
        "symbol_kind": kind,
        "exported": exported,
        "owner": owner,
        "qualified_name": qualified,
    }


def _reachability(path: str, status: str) -> dict[str, Any]:
    return {
        "record_type": "reachability",
        "record_id": f"reachability:file:{path}",
        "target": f"file:{path}",
        "status": status,
    }


def _authority(
    tmp_path: Path,
    concepts: str,
    members: str = "      - alpha\n",
    ownership: str = "",
) -> Path:
    """Write a minimal architecture context and return its path."""
    path = tmp_path / "01_core_architecture.yaml"
    path.write_text(
        "system_membership:\n  repositories:\n" + members
        + ("canonical_ownership:\n  repositories:\n" + ownership if ownership else "")
        + "architecture_concepts:\n  concepts:\n" + concepts,
        encoding="utf-8",
    )
    return path


ONE_CONCEPT = (
    "  - key: files\n"
    "    label: Files\n"
    "    statement: governed files\n"
    "    realized_by:\n"
    "      - alpha/core.py\n"
)


# -- identity ----------------------------------------------------------------


class TestDottedIdentity:
    """One rule for every language, so no extension has a code path."""

    @pytest.mark.parametrize(
        ("path", "qualified", "expected"),
        [
            ("rey_lib/ai/ai.py", "AI.execute", "rey_lib.ai.ai.AI.execute"),
            ("rey_lib/logs/__init__.py", "log_run_record", "rey_lib.logs.log_run_record"),
            ("frontend/src/panel/Panel.ts", "Panel.onAction",
             "frontend.src.panel.Panel.Panel.onAction"),
        ],
    )
    def test_a_path_and_a_qualified_name_make_one_identity(
        self, path: str, qualified: str, expected: str,
    ) -> None:
        assert dotted_identity(path, qualified) == expected


# -- the hierarchy is the architecture ---------------------------------------


class TestTheHierarchy:
    """Concepts nest; evidence hangs beneath them. Repository is never a level."""

    def test_a_concept_is_the_top_of_the_tree(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, ONE_CONCEPT)
        projection = build_architecture_projection(
            path, {"alpha": _map(_file("alpha/core.py"))},
        )
        roots = [r for r in projection.records if r["parent_id"] is None]

        assert [r["node_type"] for r in roots] == [NODE_TYPE_CONCEPT]
        assert roots[0]["label"] == "Files"
        assert roots[0]["record_id"] == "architecture_node:files"

    def test_concepts_nest_in_declared_order(self, tmp_path: Path) -> None:
        """Declared order is display order, so nothing is sorted."""
        path = _authority(tmp_path, (
            "  - key: console\n"
            "    label: Rey Console\n"
            "    concepts:\n"
            "      - key: explorer\n"
            "        label: Explorer\n"
            "        concepts:\n"
            "          - key: tabs\n"
            "            label: Tabs\n"
            "          - key: tree\n"
            "            label: Tree\n"
        ))
        projection = build_architecture_projection(path, {"alpha": _map()})
        by_id = {r["record_id"]: r for r in projection.records}

        assert [r["label"] for r in projection.records] == [
            "Rey Console", "Explorer", "Tabs", "Tree",
        ]
        assert by_id["architecture_node:console.explorer.tabs"]["parent_id"] == (
            "architecture_node:console.explorer"
        )

    def test_a_concept_carries_no_structural_field(self, tmp_path: Path) -> None:
        """A concept is not a file, and inventing a path for one would put
        source organization back into the hierarchy."""
        path = _authority(tmp_path, ONE_CONCEPT)
        projection = build_architecture_projection(
            path, {"alpha": _map(_file("alpha/core.py"))},
        )
        concept = next(r for r in projection.records if r["node_type"] == NODE_TYPE_CONCEPT)

        for absent in ("repository", "source_path", "source_line", "symbol_kind",
                       "owner", "qualified_name", "exported", "reachability",
                       "evidence_id"):
            assert concept[absent] is None, absent
        assert concept["statement"] == "governed files"

    def test_repository_is_a_fact_on_the_evidence_and_not_a_level(
        self, tmp_path: Path,
    ) -> None:
        """A concept spans repositories; a repository never contains a concept."""
        path = _authority(tmp_path, (
            "  - key: logging\n"
            "    label: Logging\n"
            "    realized_by:\n"
            "      - alpha/core.py\n"
            "      - beta/other.py\n"
        ), members="      - alpha\n      - beta\n")
        projection = build_architecture_projection(path, {
            "alpha": _map(_file("alpha/core.py")),
            "beta": _map(_file("beta/other.py")),
        })
        evidence = [r for r in projection.records if r["node_type"] == NODE_TYPE_MODULE]

        assert {r["repository"] for r in evidence} == {"alpha", "beta"}
        assert {r["parent_id"] for r in evidence} == {"architecture_node:logging"}
        assert not [r for r in projection.records if r["node_type"] == "repository"]


# -- what an evidence node carries -------------------------------------------


class TestEvidence:
    """Structural fields are copied; nothing is manufactured."""

    def test_a_module_carries_its_path_and_its_authored_prose(
        self, tmp_path: Path,
    ) -> None:
        """canonical_ownership is no longer the hierarchy, but it is still
        where a module's own meaning is written."""
        path = _authority(tmp_path, ONE_CONCEPT, ownership=(
            "    alpha:\n"
            "      canonical_modules:\n"
            "        alpha/core.py: the one place work happens\n"
        ))
        projection = build_architecture_projection(
            path, {"alpha": _map(_file("alpha/core.py"),
                                 _reachability("alpha/core.py", "definitely_reachable"))},
        )
        module = next(r for r in projection.records if r["node_type"] == NODE_TYPE_MODULE)

        assert module["source_path"] == "alpha/core.py"
        assert module["statement"] == "the one place work happens"
        assert module["reachability"] == "definitely_reachable"
        assert module["evidence_id"] == "alpha:alpha/core.py"
        assert module["owner"] is None

    def test_a_directory_claims_no_file_facts(self, tmp_path: Path) -> None:
        """Reachability is a fact about a file; a package has none to copy."""
        path = _authority(tmp_path, (
            "  - key: files\n"
            "    label: Files\n"
            "    realized_by:\n"
            "      - alpha/pkg/\n"
        ))
        projection = build_architecture_projection(
            path, {"alpha": _map(_file("alpha/pkg/thing.py"),
                                 _reachability("alpha/pkg/thing.py", "definitely_reachable"))},
        )
        module = next(r for r in projection.records if r["node_type"] == NODE_TYPE_MODULE)

        assert module["source_path"] == "alpha/pkg/"
        assert module["reachability"] is None

    def test_a_symbol_copies_its_structural_fields_unchanged(
        self, tmp_path: Path,
    ) -> None:
        """owner "" stays a real fact: a top-level declaration."""
        path = _authority(tmp_path, (
            "  - key: files\n"
            "    label: Files\n"
            "    realized_by:\n"
            "      - alpha.core.handler\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(_file("alpha/core.py"),
                           _symbol("alpha/core.py", "handler", line=12))},
        )
        symbol = next(r for r in projection.records if r["node_type"] == NODE_TYPE_SYMBOL)

        assert symbol["owner"] == ""
        assert symbol["qualified_name"] == "handler"
        assert symbol["source_line"] == 12
        assert symbol["exported"] is True
        assert symbol["evidence_id"] == "alpha:alpha/core.py:handler"

    def test_a_typescript_method_resolves_by_the_same_rule(self, tmp_path: Path) -> None:
        """Nothing about this record is Python."""
        path = _authority(tmp_path, (
            "  - key: presentation\n"
            "    label: Presentation\n"
            "    realized_by:\n"
            "      - frontend.src.panel.Panel.Panel.onAction\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(
                _file("frontend/src/panel/Panel.ts"),
                _symbol("frontend/src/panel/Panel.ts", "onAction",
                        owner="Panel", kind="method", line=453, exported=False),
            )},
        )
        symbol = next(r for r in projection.records if r["node_type"] == NODE_TYPE_SYMBOL)

        assert symbol["owner"] == "Panel"
        assert symbol["source_line"] == 453
        # protected, so not part of the exported surface under the extractor
        # contract, even though Panel itself is exported.
        assert symbol["exported"] is False


# -- multiplicity ------------------------------------------------------------


class TestMultiplicity:
    """One identity, several occurrences -- the estate's own rule."""

    def test_one_symbol_under_two_concepts_is_two_occurrences(
        self, tmp_path: Path,
    ) -> None:
        """*"One object reached down two branches is one identity and two
        occurrences."* record_id differs per placement; evidence_id does not.

        Nothing is deduplicated and nothing is refused: an exclusivity rule
        would be architecture, and none is authored.
        """
        path = _authority(tmp_path, (
            "  - key: explorer\n"
            "    label: Explorer\n"
            "    realized_by:\n"
            "      - alpha.core.handler\n"
            "  - key: presentation\n"
            "    label: Presentation\n"
            "    realized_by:\n"
            "      - alpha.core.handler\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(_file("alpha/core.py"), _symbol("alpha/core.py", "handler"))},
        )
        occurrences = [r for r in projection.records if r["node_type"] == NODE_TYPE_SYMBOL]

        assert len(occurrences) == 2
        assert len({r["record_id"] for r in occurrences}) == 2
        assert len({r["evidence_id"] for r in occurrences}) == 1
        assert {r["parent_id"] for r in occurrences} == {
            "architecture_node:explorer", "architecture_node:presentation",
        }


# -- what it refuses ---------------------------------------------------------


class TestItRefuses:
    """Every failure is a contradiction between two authorities, with no default."""

    def test_a_reference_naming_nothing(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, ONE_CONCEPT)

        with pytest.raises(ArchitectureProjectionError, match="names no file"):
            build_architecture_projection(path, {"alpha": _map(_file("alpha/other.py"))})

    def test_a_reference_resolving_in_two_repositories(self, tmp_path: Path) -> None:
        """Guessing between them would attach a concept to code that does not
        realize it."""
        path = _authority(tmp_path, ONE_CONCEPT, members="      - alpha\n      - beta\n")

        with pytest.raises(ArchitectureProjectionError, match="several places"):
            build_architecture_projection(path, {
                "alpha": _map(_file("alpha/core.py")),
                "beta": _map(_file("alpha/core.py")),
            })

    def test_two_symbol_records_at_one_identity(self, tmp_path: Path) -> None:
        """The evidence index keeps every record an identity names, so a
        contradiction reaches the caller instead of being silently decided."""
        path = _authority(tmp_path, (
            "  - key: files\n"
            "    label: Files\n"
            "    realized_by:\n"
            "      - alpha.core.handler\n"
        ))

        with pytest.raises(ArchitectureProjectionError, match="several places"):
            build_architecture_projection(
                path,
                {"alpha": _map(_file("alpha/core.py"),
                               _symbol("alpha/core.py", "handler", line=3),
                               _symbol("alpha/core.py", "handler", line=9))},
            )

    def test_a_concept_declaring_no_key(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, "  - label: Nameless\n")

        with pytest.raises(ArchitectureProjectionError, match="declares no key"):
            build_architecture_projection(path, {"alpha": _map()})

    def test_a_document_declaring_no_concepts(self, tmp_path: Path) -> None:
        path = tmp_path / "01_core_architecture.yaml"
        path.write_text("system_membership:\n  repositories:\n      - alpha\n", encoding="utf-8")

        with pytest.raises(ArchitectureProjectionError, match="architecture_concepts"):
            build_architecture_projection(path, {"alpha": _map()})

    def test_a_declared_member_with_no_map(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, ONE_CONCEPT, members="      - alpha\n      - beta\n")

        with pytest.raises(ArchitectureProjectionError, match="Missing"):
            build_architecture_projection(path, {"alpha": _map(_file("alpha/core.py"))})

    def test_a_map_no_member_declared(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, ONE_CONCEPT)

        with pytest.raises(ArchitectureProjectionError, match="Unexpected"):
            build_architecture_projection(
                path, {"alpha": _map(_file("alpha/core.py")), "ghost": _map()},
            )


# -- source evidence ---------------------------------------------------------


def test_a_source_capability_statement_lends_no_prose(tmp_path: Path) -> None:
    """It describes a system that was migrated away from, so nothing current
    realizes it and no evidence node borrows its words."""
    path = _authority(tmp_path, ONE_CONCEPT, ownership=(
        "    alpha:\n"
        "      canonical_modules:\n"
        "        alpha/core.py:\n"
        "          summary: In the retired system, this did the work.\n"
        "          role_in_migration: SOURCE_CAPABILITY_ARCHITECTURE\n"
    ))
    projection = build_architecture_projection(
        path, {"alpha": _map(_file("alpha/core.py"))},
    )
    module = next(r for r in projection.records if r["node_type"] == NODE_TYPE_MODULE)

    assert module["statement"] is None


def test_scripts_is_excluded_by_decision() -> None:
    """Packaging metadata is not architectural ownership.

    canonical_ownership.<repo>.scripts lists console-script names with no
    statement beside them, and the name-to-target mapping lives in
    pyproject.toml -- a third file, and a second structural authority.
    """
    assert CONSUMED_OWNERSHIP_SECTIONS == ("canonical_modules", "canonical_symbols")
    assert "scripts" not in CONSUMED_OWNERSHIP_SECTIONS


# -- staleness ---------------------------------------------------------------


class TestValidation:
    """A join is only as current as the staler of the two authorities."""

    def _built(
        self, tmp_path: Path,
    ) -> tuple[ArchitectureProjection, dict[str, RepositoryMap], Path]:
        path = _authority(tmp_path, ONE_CONCEPT)
        maps = {"alpha": _map(_file("alpha/core.py"))}
        return build_architecture_projection(path, maps), maps, path

    def test_a_fresh_projection_is_current(self, tmp_path: Path) -> None:
        projection, maps, path = self._built(tmp_path)

        assert validate_architecture_projection(projection, maps, path) == []

    def test_a_regenerated_map_makes_it_stale(self, tmp_path: Path) -> None:
        projection, _, path = self._built(tmp_path)
        moved = {"alpha": _map(_file("alpha/core.py"), content="hash-2")}

        assert validate_architecture_projection(projection, moved, path) == [
            "alpha: map has been regenerated since the projection"
        ]

    def test_edited_meaning_makes_it_stale_even_with_unchanged_maps(
        self, tmp_path: Path,
    ) -> None:
        """A concept added, renamed or re-pointed changes what the artifact
        should say while every map hash still matches."""
        projection, maps, path = self._built(tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8").replace("label: Files", "label: Governed Files"),
            encoding="utf-8",
        )

        assert validate_architecture_projection(projection, maps, path) == [
            "01_core_architecture.yaml: the architecture context has been edited "
            "since the projection"
        ]


# -- determinism -------------------------------------------------------------


def test_the_same_inputs_hash_the_same(tmp_path: Path) -> None:
    """generated_at is outside the hash, or the contract fights itself."""
    path = _authority(tmp_path, ONE_CONCEPT)
    maps = {"alpha": _map(_file("alpha/core.py"))}

    first = build_architecture_projection(path, maps)
    second = build_architecture_projection(path, maps)

    assert first.header["content_hash"] == second.header["content_hash"]
    assert first.records == second.records


def test_membership_is_read_from_the_architecture(tmp_path: Path) -> None:
    """One membership authority, and the command is not allowed to be a second."""
    path = _authority(tmp_path, ONE_CONCEPT, members="      - alpha\n      - beta\n")

    assert system_membership(path) == ["alpha", "beta"]


# -- the boundary, held structurally -----------------------------------------

BUILDER = Path(__file__).resolve().parent.parent / "rey_lib/repository_map/architecture_projection.py"


def test_the_builder_names_no_concept() -> None:
    """The taxonomy is authored, never compiled in.

    A concept key in this module would be architecture living in the generator,
    and the next taxonomy change would be a code change.
    """
    tree = ast.parse(BUILDER.read_text(encoding="utf-8"))
    prose = {
        id(node.body[0].value)
        for node in [tree, *ast.walk(tree)]
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    used = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in prose
    ]

    for named in ("explorer", "workbench", "rey_console", "presentation"):
        assert not [text for text in used if named in text.lower()], named

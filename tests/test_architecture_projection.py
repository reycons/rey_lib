"""The architecture projection: authored meaning joined to exact structural evidence.

What these prove is mostly what the projection *refuses*. A join that emitted
its best guess would be worse than no artifact at all: a consumer cannot tell a
resolved statement from a nearly-resolved one, and the whole point is that a
node's presence means something.
"""

from __future__ import annotations

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
    NODE_TYPE_MODULE,
    NODE_TYPE_REPOSITORY,
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


def _authority(tmp_path: Path, ownership: str, members: str = "      - alpha\n") -> Path:
    """Write a minimal architecture context and return its path."""
    path = tmp_path / "01_core_architecture.yaml"
    path.write_text(
        "system_membership:\n  repositories:\n" + members
        + "canonical_ownership:\n  repositories:\n" + ownership,
        encoding="utf-8",
    )
    return path


ONE_MODULE = (
    "    alpha:\n"
    "      canonical_modules:\n"
    "        alpha/core.py: the one place work happens\n"
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
            ("frontend/src/splitter_view/mount.tsx", "mount",
             "frontend.src.splitter_view.mount.mount"),
        ],
    )
    def test_a_path_and_a_qualified_name_make_one_identity(
        self, path: str, qualified: str, expected: str,
    ) -> None:
        assert dotted_identity(path, qualified) == expected


# -- the shape it emits ------------------------------------------------------


class TestWhatItEmits:
    """Three node types, and no fourth invented to hold the others together."""

    def test_a_repository_is_the_top_of_the_tree(self, tmp_path: Path) -> None:
        """No artifact root: the Console owns its tree root, and this owns none."""
        path = _authority(tmp_path, ONE_MODULE)
        projection = build_architecture_projection(
            path, {"alpha": _map(_file("alpha/core.py"))},
        )

        roots = [r for r in projection.records if r["parent_id"] is None]

        assert [r["node_type"] for r in roots] == [NODE_TYPE_REPOSITORY]
        assert roots[0]["record_id"] == "architecture_node:alpha"

    def test_a_module_carries_its_statement_and_no_symbol_fields(
        self, tmp_path: Path,
    ) -> None:
        """null means the field does not apply, never a missing structural value."""
        path = _authority(tmp_path, ONE_MODULE)
        projection = build_architecture_projection(
            path, {"alpha": _map(_file("alpha/core.py"),
                                 _reachability("alpha/core.py", "definitely_reachable"))},
        )
        module = next(r for r in projection.records if r["node_type"] == NODE_TYPE_MODULE)

        assert module["statement"] == "the one place work happens"
        assert module["source_path"] == "alpha/core.py"
        assert module["reachability"] == "definitely_reachable"
        assert module["owner"] is None
        assert module["qualified_name"] is None
        assert module["symbol_kind"] is None
        assert module["exported"] is None
        assert module["source_line"] is None

    def test_a_directory_module_claims_no_file_facts(self, tmp_path: Path) -> None:
        """Reachability is a fact about a file; a package has none to copy."""
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_modules:\n"
            "        alpha/pkg/: the package\n"
        ))
        projection = build_architecture_projection(
            path, {"alpha": _map(_file("alpha/pkg/thing.py"),
                                 _reachability("alpha/pkg/thing.py", "definitely_reachable"))},
        )
        module = next(r for r in projection.records if r["node_type"] == NODE_TYPE_MODULE)

        assert module["source_path"] == "alpha/pkg/"
        assert module["reachability"] is None

    def test_a_symbol_copies_its_structural_fields_unchanged(self, tmp_path: Path) -> None:
        """owner "" stays a real fact: a top-level declaration, not an absent field."""
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.core.handler\n"
            "          statement: the one entry point\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(_file("alpha/core.py"),
                           _symbol("alpha/core.py", "handler", line=12))},
        )
        symbol = next(r for r in projection.records if r["node_type"] == NODE_TYPE_SYMBOL)

        assert symbol["record_id"] == "architecture_node:alpha:alpha/core.py:handler"
        assert symbol["owner"] == ""
        assert symbol["qualified_name"] == "handler"
        assert symbol["source_line"] == 12
        assert symbol["exported"] is True
        assert symbol["statement"] == "the one entry point"

    def test_a_method_keeps_its_owner(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.core.Engine.run\n"
            "          statement: the one execution surface\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(_file("alpha/core.py"),
                           _symbol("alpha/core.py", "run", owner="Engine", kind="method"))},
        )
        symbol = next(r for r in projection.records if r["node_type"] == NODE_TYPE_SYMBOL)

        assert symbol["owner"] == "Engine"
        assert symbol["qualified_name"] == "Engine.run"
        assert symbol["record_id"] == "architecture_node:alpha:alpha/core.py:Engine.run"

    def test_a_typescript_method_resolves_by_the_same_rule(self, tmp_path: Path) -> None:
        """The builder joins on the derived identity, not a reconstructed filename.

        Nothing about this record is Python. An earlier draft rebuilt a
        candidate path from the authored reference and could only ever find
        .py, which would have left the Console half of the estate unaddressable
        while every Python test still passed.
        """
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_modules:\n"
            "        frontend/src/panel/: the panel object\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - frontend.src.panel.Panel.Panel.onAction\n"
            "          statement: how a panel is told an action was pressed\n"
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

        assert symbol["record_id"] == (
            "architecture_node:alpha:frontend/src/panel/Panel.ts:Panel.onAction"
        )
        assert symbol["parent_id"] == "architecture_node:alpha:frontend/src/panel/"
        assert symbol["owner"] == "Panel"
        assert symbol["source_line"] == 453
        # protected, so not part of the exported surface under the
        # extractor contract, even though Panel itself is exported.
        assert symbol["exported"] is False


# -- where a symbol hangs ----------------------------------------------------


class TestSymbolParent:
    """The deepest canonical module containing it, and the repository otherwise."""

    def test_a_symbol_hangs_under_the_deepest_containing_module(
        self, tmp_path: Path,
    ) -> None:
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_modules:\n"
            "        alpha/: the whole package\n"
            "        alpha/deep/: the inner one\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.deep.core.handler\n"
            "          statement: the entry point\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(_file("alpha/deep/core.py"),
                           _symbol("alpha/deep/core.py", "handler"))},
        )
        symbol = next(r for r in projection.records if r["node_type"] == NODE_TYPE_SYMBOL)

        assert symbol["parent_id"] == "architecture_node:alpha:alpha/deep/"

    def test_a_symbol_with_no_containing_module_hangs_under_its_repository(
        self, tmp_path: Path,
    ) -> None:
        """No synthetic module: every module node is an authored statement."""
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.core.handler\n"
            "          statement: the entry point\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(_file("alpha/core.py"), _symbol("alpha/core.py", "handler"))},
        )
        symbol = next(r for r in projection.records if r["node_type"] == NODE_TYPE_SYMBOL)

        assert symbol["parent_id"] == "architecture_node:alpha"
        assert [r["node_type"] for r in projection.records] == [
            NODE_TYPE_REPOSITORY, NODE_TYPE_SYMBOL,
        ]

    def test_containment_is_on_path_boundaries(self, tmp_path: Path) -> None:
        """alpha/core/ must not swallow a file under alpha/core_helpers/."""
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_modules:\n"
            "        alpha/core/: the inner package\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.core_helpers.thing.handler\n"
            "          statement: elsewhere entirely\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(_file("alpha/core/a.py"), _file("alpha/core_helpers/thing.py"),
                           _symbol("alpha/core_helpers/thing.py", "handler"))},
        )
        symbol = next(r for r in projection.records if r["node_type"] == NODE_TYPE_SYMBOL)

        assert symbol["parent_id"] == "architecture_node:alpha"

    def test_has_children_comes_from_the_assembled_set(self, tmp_path: Path) -> None:
        """Never inferred from node type: a module may own nothing."""
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_modules:\n"
            "        alpha/core.py: owns a symbol\n"
            "        alpha/lonely.py: owns none\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.core.handler\n"
            "          statement: the entry point\n"
        ))
        projection = build_architecture_projection(
            path,
            {"alpha": _map(_file("alpha/core.py"), _file("alpha/lonely.py"),
                           _symbol("alpha/core.py", "handler"))},
        )
        by_id = {r["record_id"]: r for r in projection.records}

        assert by_id["architecture_node:alpha:alpha/core.py"]["has_children"] is True
        assert by_id["architecture_node:alpha:alpha/lonely.py"]["has_children"] is False
        assert by_id["architecture_node:alpha"]["has_children"] is True


# -- what it refuses ---------------------------------------------------------


class TestItRefuses:
    """Every failure is a contradiction between two authorities, with no default."""

    def test_a_module_naming_nothing_the_map_records(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, ONE_MODULE)

        with pytest.raises(ArchitectureProjectionError, match="canonical module"):
            build_architecture_projection(path, {"alpha": _map(_file("alpha/other.py"))})

    def test_a_symbol_naming_nothing_the_map_records(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.core.gone\n"
            "          statement: it used to be here\n"
        ))

        with pytest.raises(ArchitectureProjectionError, match="resolves to no symbol"):
            build_architecture_projection(
                path, {"alpha": _map(_file("alpha/core.py"),
                                     _symbol("alpha/core.py", "present"))},
            )

    def test_two_records_at_one_path_and_name(self, tmp_path: Path) -> None:
        """The evidence index must not keep only the first record it saw.

        Proved at the storage boundary: same path, same qualified name, two
        distinct records. An index that overwrote would report one match and
        the contradiction would never reach anyone.
        """
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.core.handler\n"
            "          statement: which one?\n"
        ))

        with pytest.raises(ArchitectureProjectionError, match="resolves to several"):
            build_architecture_projection(
                path,
                {"alpha": _map(_file("alpha/core.py"),
                               _symbol("alpha/core.py", "handler", line=3),
                               _symbol("alpha/core.py", "handler", line=9))},
            )

    def test_two_paths_collapsing_to_one_identity(self, tmp_path: Path) -> None:
        """And proved at the identity boundary, where the paths differ.

        A module and a same-named package's __init__ derive the same dotted
        identity, so the authored reference genuinely names both. That is
        ambiguity in the authority, not in the index, and it fails too.
        """
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.core.handler\n"
            "          statement: the module one, or the package one?\n"
        ))

        with pytest.raises(ArchitectureProjectionError, match="resolves to several"):
            build_architecture_projection(
                path,
                {"alpha": _map(_file("alpha/core.py"), _file("alpha/core/__init__.py"),
                               _symbol("alpha/core.py", "handler"),
                               _symbol("alpha/core/__init__.py", "handler"))},
            )

    def test_a_declared_member_with_no_map(self, tmp_path: Path) -> None:
        path = _authority(tmp_path, ONE_MODULE, members="      - alpha\n      - beta\n")

        with pytest.raises(ArchitectureProjectionError, match="Missing"):
            build_architecture_projection(path, {"alpha": _map(_file("alpha/core.py"))})

    def test_a_map_no_member_declared(self, tmp_path: Path) -> None:
        """Drift fails in both directions, or the command becomes a second authority."""
        path = _authority(tmp_path, ONE_MODULE)

        with pytest.raises(ArchitectureProjectionError, match="Unexpected"):
            build_architecture_projection(
                path,
                {"alpha": _map(_file("alpha/core.py")), "ghost": _map()},
            )


# -- source evidence ---------------------------------------------------------


class TestSourceEvidence:
    """Recognized, then excluded -- before resolution is ever attempted."""

    def test_a_source_module_naming_absent_code_neither_resolves_nor_fails(
        self, tmp_path: Path,
    ) -> None:
        """The identity is deliberately one no map could answer.

        That is the proof: exclusion happens ahead of resolution, rather than
        the statement happening to resolve and being dropped afterwards.
        """
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_modules:\n"
            "        alpha/core.py: the one place work happens\n"
            "        alpha/retired/gone_forever.py:\n"
            "          summary: In the retired system, this did the work.\n"
            "          role_in_migration: SOURCE_CAPABILITY_ARCHITECTURE\n"
        ))
        projection = build_architecture_projection(
            path, {"alpha": _map(_file("alpha/core.py"))},
        )

        assert [r["architecture_key"] for r in projection.records
                if r["node_type"] == NODE_TYPE_MODULE] == ["alpha/core.py"]

    def test_a_source_symbol_naming_absent_code_neither_resolves_nor_fails(
        self, tmp_path: Path,
    ) -> None:
        path = _authority(tmp_path, (
            "    alpha:\n"
            "      canonical_symbols:\n"
            "        - symbols:\n"
            "            - alpha.retired.gone_forever.handler\n"
            "          statement: In the retired system, this handled it.\n"
            "          role_in_migration: SOURCE_CAPABILITY_ARCHITECTURE\n"
        ))
        projection = build_architecture_projection(path, {"alpha": _map()})

        assert [r["node_type"] for r in projection.records] == [NODE_TYPE_REPOSITORY]


# -- the sections it consumes ------------------------------------------------


def test_scripts_is_excluded_by_decision() -> None:
    """Packaging metadata is not architectural ownership.

    canonical_ownership.<repo>.scripts lists console-script names with no
    statement beside them, and the name-to-target mapping lives in
    pyproject.toml -- a third file, and a second structural authority. Its
    absence from the projection is recorded here so it stays a decision rather
    than becoming something the builder happens not to read.
    """
    assert CONSUMED_OWNERSHIP_SECTIONS == ("canonical_modules", "canonical_symbols")
    assert "scripts" not in CONSUMED_OWNERSHIP_SECTIONS


def test_a_scripts_section_is_carried_past_without_complaint(tmp_path: Path) -> None:
    """Its presence in the authority is valid; only its projection is refused."""
    path = _authority(tmp_path, (
        "    alpha:\n"
        "      canonical_modules:\n"
        "        alpha/core.py: the one place work happens\n"
        "      scripts:\n"
        "        - alpha-do-thing\n"
    ))
    projection = build_architecture_projection(
        path, {"alpha": _map(_file("alpha/core.py"))},
    )

    assert {r["node_type"] for r in projection.records} == {
        NODE_TYPE_REPOSITORY, NODE_TYPE_MODULE,
    }


# -- staleness ---------------------------------------------------------------


class TestValidation:
    """A join is only as current as the staler of the two authorities."""

    def _built(
        self, tmp_path: Path,
    ) -> tuple[ArchitectureProjection, dict[str, RepositoryMap], Path]:
        path = _authority(tmp_path, ONE_MODULE)
        maps = {"alpha": _map(_file("alpha/core.py"))}
        return build_architecture_projection(path, maps), maps, path

    def test_a_fresh_projection_is_current(self, tmp_path: Path) -> None:
        projection, maps, path = self._built(tmp_path)

        assert validate_architecture_projection(projection, maps, path) == []

    def test_a_regenerated_map_makes_it_stale(self, tmp_path: Path) -> None:
        projection, _, path = self._built(tmp_path)
        moved = {"alpha": _map(_file("alpha/core.py"), content="hash-2")}

        reasons = validate_architecture_projection(projection, moved, path)

        assert reasons == ["alpha: map has been regenerated since the projection"]

    def test_edited_meaning_makes_it_stale_even_with_unchanged_maps(
        self, tmp_path: Path,
    ) -> None:
        """The half that structural hashes cannot see.

        A statement rewritten, retired or newly authored changes what the
        artifact should say while every map hash still matches.
        """
        projection, maps, path = self._built(tmp_path)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "the one place work happens", "the one place work happens, restated"
            ),
            encoding="utf-8",
        )

        reasons = validate_architecture_projection(projection, maps, path)

        assert reasons == [
            "01_core_architecture.yaml: the architecture context has been edited "
            "since the projection"
        ]


# -- determinism -------------------------------------------------------------


def test_the_same_inputs_hash_the_same(tmp_path: Path) -> None:
    """generated_at is outside the hash, or the contract fights itself."""
    path = _authority(tmp_path, ONE_MODULE)
    maps = {"alpha": _map(_file("alpha/core.py"))}

    first = build_architecture_projection(path, maps)
    second = build_architecture_projection(path, maps)

    assert first.header["content_hash"] == second.header["content_hash"]
    assert first.records == second.records


def test_membership_is_read_from_the_architecture(tmp_path: Path) -> None:
    """One membership authority, and the command is not allowed to be a second."""
    path = _authority(tmp_path, ONE_MODULE, members="      - alpha\n      - beta\n")

    assert system_membership(path) == ["alpha", "beta"]

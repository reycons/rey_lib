"""Semantic reference evidence (backlog 872, #868 S2).

The fixture tests run the real pinned indexers: they are the regression that
ties each pin to its measured position encoding, so they fail -- not skip --
where the tools are not provisioned.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rey_lib.repository_map import semantic_index
from rey_lib.repository_map.extractors import extract_executable_references, extract_symbols
from rey_lib.repository_map.semantic_index import (
    MEASURED_POSITION_ENCODING,
    PRODUCERS,
    semantic_evidence,
)

_PY = '''class Thing:
    def méthode(self) -> int:
        return 1


def use(t: Thing) -> int:
    x = "😀é"; return t.méthode()
'''

_TS = '''export class Thing {
  méthode(): number { return 1; }
}
export function use(t: Thing): number {
  const x = "😀é"; return t.méthode();
}
'''


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A repository with one Python and one TypeScript module, each with an
    emoji before the reference, where UTF-8, UTF-16 and UTF-32 offsets differ."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text(_PY, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.ts").write_text(_TS, encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions": {"target": "es2020", "strict": true}, "include": ["src"]}',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    return tmp_path


_LANGUAGES = {
    "pkg/__init__.py": "Python",
    "pkg/mod.py": "Python",
    "src/mod.ts": "TypeScript",
    "tsconfig.json": "unknown",
    "pyproject.toml": "unknown",
}


def _edges(root: Path) -> list:
    """Extract every edge in the fixture."""
    edges = []
    for path, language in _LANGUAGES.items():
        if language != "unknown":
            edges += extract_executable_references(root / path, language, path)
    return edges


class TestThePinsAreMeasured:
    """Every pinned producer has a measured encoding, and only the pins do."""

    def test_the_table_names_exactly_the_locked_versions(self) -> None:
        """Changing a pin fails here until its encoding is remeasured."""
        lock = json.loads((semantic_index.TOOLS / "package-lock.json").read_text())
        pinned = {
            (producer.key, lock["packages"][f"node_modules/{producer.package}"]["version"])
            for producer in PRODUCERS
        }
        assert set(MEASURED_POSITION_ENCODING) == pinned


class TestTheFixtureThroughTheRealIndexers:
    """The measured encodings convert to Rey's positions exactly."""

    def test_both_producers_ran_in_utf16(self, fixture_repo: Path) -> None:
        """Both pinned tools ran, read as their measured UTF-16."""
        evidence = semantic_evidence(fixture_repo, _LANGUAGES, _edges(fixture_repo))

        by_producer = {coverage.producer: coverage for coverage in evidence.coverage}
        for key in ("scip-python", "scip-typescript"):
            assert by_producer[key].status == "ran", by_producer[key]
            assert by_producer[key].position_encoding == "UTF16CodeUnitOffsetFromLineStart"
            assert by_producer[key].refused_occurrences == 0

    @pytest.mark.parametrize(("path", "language"), [("pkg/mod.py", "Python"),
                                                    ("src/mod.ts", "TypeScript")])
    def test_the_reference_after_the_emoji_lands_on_its_edge(
        self, fixture_repo: Path, path: str, language: str
    ) -> None:
        """t.méthode, after an emoji, attaches at the edge's exact end."""
        edges = _edges(fixture_repo)
        evidence = semantic_evidence(fixture_repo, _LANGUAGES, edges)

        call = next(e for e in edges if e.source_path == path and e.to == "t.méthode")
        attached = [
            r for r in evidence.references
            if (r.source_path, r.end_line, r.end_column) == (path, call.end_line, call.end_column)
        ]
        assert len(attached) == 1
        assert attached[0].occurrence_kind == "indexed"
        assert "méthode" in attached[0].symbol

    @pytest.mark.parametrize(("path", "language"), [("pkg/mod.py", "Python"),
                                                    ("src/mod.ts", "TypeScript")])
    def test_a_definition_matches_the_rey_symbol_exactly(
        self, fixture_repo: Path, path: str, language: str
    ) -> None:
        """Thing.méthode's definition carries the Rey symbol's own name position."""
        evidence = semantic_evidence(fixture_repo, _LANGUAGES, _edges(fixture_repo))
        symbol = next(
            s for s in extract_symbols(fixture_repo / path, language, path).symbols
            if s.qualified_name == "Thing.méthode"
        )

        definition = next(
            d for d in evidence.definitions
            if d.source_path == path and d.qualified_name == "Thing.méthode"
        )
        assert (definition.name_line, definition.name_column) == (
            symbol.name_line, symbol.name_column,
        )

    def test_an_unmeasured_version_yields_no_evidence(
        self, fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No declared and no measured encoding: failed, and nothing emitted."""
        monkeypatch.setattr(semantic_index, "MEASURED_POSITION_ENCODING", {})
        evidence = semantic_evidence(fixture_repo, _LANGUAGES, _edges(fixture_repo))

        assert {c.status for c in evidence.coverage} == {"failed"}
        assert {c.reason for c in evidence.coverage} == {"unknown_position_encoding"}
        assert not evidence.references and not evidence.definitions


class TestRepeatedOccurrences:
    """A producer reporting one occurrence twice yields one fact."""

    def test_a_repeated_reference_is_kept_once(self, tmp_path: Path) -> None:
        """Two identical occurrences at one edge end are one reference."""
        from rey_lib.repository_map import scip_pb2

        (tmp_path / "m.py").write_text("String(1)\n", encoding="utf-8")
        symbol = "scip-typescript npm typescript 5.9.3 lib/`lib.es5.d.ts`/String#"
        twice = [scip_pb2.Occurrence(range=[0, 0, 6], symbol=symbol) for _ in range(2)]
        document = scip_pb2.Document(relative_path="m.py", occurrences=twice)
        producer = PRODUCERS[0]

        evidence = semantic_index._read_documents(
            producer, "0.6.6", tmp_path, {"m.py": "Python"}, {("m.py", 0, 6)},
            [("m.py", document)],
        )

        assert len(evidence.references) == 1
        assert evidence.coverage[0].occurrences == 2


class TestCoverageSaysWhatHappened:
    """failed and not_applicable never stand in for each other."""

    def test_an_unprovisioned_tool_is_failed(
        self, fixture_repo: Path, tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A repository with the language and no installed tool is failed."""
        empty = tmp_path_factory.mktemp("tools")
        (empty / "package-lock.json").write_text(
            (semantic_index.TOOLS / "package-lock.json").read_text(), encoding="utf-8"
        )
        monkeypatch.setattr(semantic_index, "TOOLS", empty)
        evidence = semantic_evidence(fixture_repo, _LANGUAGES, [])

        assert {(c.status, c.reason) for c in evidence.coverage} == {
            ("failed", "tool_not_installed"),
        }

    def test_a_repository_without_the_language_is_not_applicable(self, tmp_path: Path) -> None:
        """No TypeScript files: the TypeScript producer does not apply."""
        evidence = semantic_evidence(tmp_path, {"README.md": "unknown"}, [])

        assert {(c.producer, c.status) for c in evidence.coverage} == {
            ("scip-python", "not_applicable"), ("scip-typescript", "not_applicable"),
        }


class TestPositions:
    """Producer columns become UTF-8 byte offsets, or nothing."""

    LINE = 'x = "😀é"; t.m()'

    @pytest.mark.parametrize(("encoding", "offset"), [
        ("UTF8CodeUnitOffsetFromLineStart", 14),
        ("UTF16CodeUnitOffsetFromLineStart", 11),
        ("UTF32CodeUnitOffsetFromLineStart", 10),
    ])
    def test_each_unit_reaches_the_same_byte(self, encoding: str, offset: int) -> None:
        """The same character, counted three ways, is one byte offset."""
        assert semantic_index._byte_offset(self.LINE, offset, encoding) == 14

    def test_a_column_inside_a_character_is_refused(self) -> None:
        """UTF-16 unit 6 falls inside the emoji's surrogate pair."""
        assert semantic_index._byte_offset(self.LINE, 6, "UTF16CodeUnitOffsetFromLineStart") is None

    def test_a_column_past_the_line_is_refused(self) -> None:
        """Nothing is clamped."""
        assert semantic_index._byte_offset("ab", 3, "UTF8CodeUnitOffsetFromLineStart") is None

    def test_unsupported_range_shapes_are_refused(self) -> None:
        """Two elements, five elements and the typed fields are not read."""
        from rey_lib.repository_map import scip_pb2

        lines = ["abcdef"]
        for values in ([0, 1], [0, 1, 2, 3, 4]):
            occurrence = scip_pb2.Occurrence(range=values)
            assert semantic_index._canonical_range(occurrence, lines, "UTF8CodeUnitOffsetFromLineStart") is None
        typed = scip_pb2.Occurrence(single_line_range=scip_pb2.SingleLineRange())
        assert semantic_index._canonical_range(typed, lines, "UTF8CodeUnitOffsetFromLineStart") is None


class TestDescriptors:
    """Only declarations Rey records get a qualified name."""

    @pytest.mark.parametrize(("symbol", "expected"), [
        ("scip-python python p 1 `pkg.mod`/Thing#méthode().", "Thing.méthode"),
        ("scip-python python p 1 `pkg.mod`/Thing#", "Thing"),
        ("scip-python python p 1 `pkg.mod`/LIMIT.", "LIMIT"),
        ("scip-typescript npm . . src/`mod.ts`/use().", "use"),
        ("scip-python python p 1 `pkg.mod`/use().(t)", None),
        ("scip-typescript npm . . src/`mod.ts`/Box#[T]", None),
        ("scip-python python p 1 `pkg.mod`/__init__:", None),
        ("scip-python python p 1 `pkg.mod`/outer().inner().", None),
        ("local 4", None),
    ])
    def test_qualified_names(self, symbol: str, expected: str | None) -> None:
        """Parameters, type parameters, meta and nested declarations are not Rey's."""
        assert semantic_index._qualified_name(symbol) == expected


def test_importing_the_index_path_loads_no_protobuf() -> None:
    """Nothing imports the protobuf runtime until an index is actually read."""
    probe = (
        "import sys, rey_lib, rey_lib.repository_map.emitters, "
        "rey_lib.repository_map.semantic_index; "
        "print(sorted(m for m in sys.modules if m.startswith('google.protobuf') "
        "or m.endswith('scip_pb2')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "[]"

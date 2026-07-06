"""
Tests for the primitive file I/O layer (SGC_Rey_Lib_Primitive_File_IO_Layer).

Cover the low-level write/append primitives, parent-directory creation, JSONL
object-per-line semantics, clear error surfacing, and the architectural rule
that this foundation imports neither file_utils nor log_utils.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from rey_lib.files import primitive_file_io as pio


def test_write_text_creates_parent_directories(tmp_path: Path) -> None:
    """write_text creates missing parent directories before writing."""
    target = tmp_path / "a" / "b" / "note.txt"
    pio.write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_bytes_creates_parent_directories(tmp_path: Path) -> None:
    """write_bytes creates missing parent directories before writing."""
    target = tmp_path / "deep" / "nested" / "blob.bin"
    pio.write_bytes(target, b"\x00\x01\x02")
    assert target.read_bytes() == b"\x00\x01\x02"


def test_append_text_appends_without_overwriting(tmp_path: Path) -> None:
    """append_text preserves existing content and appends after it."""
    target = tmp_path / "log.txt"
    pio.write_text(target, "line1\n")
    pio.append_text(target, "line2\n")
    assert target.read_text(encoding="utf-8") == "line1\nline2\n"


def test_append_jsonl_writes_one_object_per_line(tmp_path: Path) -> None:
    """append_jsonl writes exactly one JSON object per line."""
    target = tmp_path / "records.jsonl"
    pio.append_jsonl(target, {"a": 1})
    pio.append_jsonl(target, {"b": 2})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": 2}


def test_append_jsonl_creates_parent_directories(tmp_path: Path) -> None:
    """append_jsonl creates missing parent directories before appending."""
    target = tmp_path / "runs" / "run.jsonl"
    pio.append_jsonl(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8").strip()) == {"ok": True}


def test_append_jsonl_stringifies_non_native_values(tmp_path: Path) -> None:
    """Non-JSON-native values are stringified rather than raising (default=str)."""
    target = tmp_path / "r.jsonl"
    pio.append_jsonl(target, {"path": Path("/tmp/x")})
    assert json.loads(target.read_text(encoding="utf-8").strip()) == {"path": "/tmp/x"}


def test_errors_are_surfaced_clearly(tmp_path: Path) -> None:
    """A write to an impossible path surfaces an OSError rather than being swallowed."""
    # A file used as a directory component makes parent creation fail with OSError.
    blocker = tmp_path / "file"
    pio.write_text(blocker, "x")
    with pytest.raises(OSError):
        pio.write_text(blocker / "child.txt", "y")


def test_atomic_write_replaces_content(tmp_path: Path) -> None:
    """atomic_write_text leaves the fully written content in place."""
    target = tmp_path / "atomic" / "out.txt"
    pio.atomic_write_text(target, "v1")
    pio.atomic_write_text(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"
    # No leftover temp files remain in the directory.
    assert [p.name for p in target.parent.iterdir()] == ["out.txt"]


def test_primitive_layer_does_not_import_file_or_log_utils() -> None:
    """The foundation must not import file_utils or log_utils (dependency shape)."""
    source = Path(pio.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("file_utils" in name for name in imported)
    assert not any("log_utils" in name for name in imported)
    # And nothing from the higher layers it must stay below.
    assert not any(name.startswith(("rey_lib.workflow", "rey_lib.logs")) for name in imported)

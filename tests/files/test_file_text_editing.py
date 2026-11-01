"""The held File owns unsaved text and persists it only on explicit Save."""
import pytest

from rey_lib.errors.error_utils import AppError
from rey_lib.files.file import File


def test_edit_toggle_save_and_lock_keep_working_text_on_the_same_object(tmp_path):
    path = tmp_path / "example.py"
    path.write_bytes(b"old\n")
    held = File(path)
    assert not held.editing_projection(100)["enabled"]
    held.set_editing(True, 100)
    held.edit_text("new\n")
    assert path.read_bytes() == b"old\n"
    assert held.content(100).text == "new\n"
    assert held.content(2).truncated
    assert len(held.content(2).text.encode("utf-8")) <= 2
    assert any(a["role"]["act"] == "save" for a in held.editing_projection(100)["actions"])
    held.set_editing(False, 100)
    assert held.content(100).text == "new\n"
    with pytest.raises(AppError, match="Enable"):
        held.edit_text("refused")
    held.save_text()
    assert path.read_bytes() == b"new\n"
    assert not any(a["role"]["act"] == "save" for a in held.editing_projection(100)["actions"])


def test_save_preserves_bom_and_refuses_external_changes(tmp_path):
    path = tmp_path / "example.txt"
    path.write_bytes(b"\xef\xbb\xbfold")
    held = File(path)
    held.set_editing(True, 100)
    held.edit_text("new")
    held.save_text()
    assert path.read_bytes() == b"\xef\xbb\xbfnew"
    held.edit_text("another")
    path.write_bytes(b"external")
    with pytest.raises(AppError, match="changed outside"):
        held.save_text()
    assert path.read_bytes() == b"external"
    assert held.content(100).text == "another"


@pytest.mark.parametrize("name,raw,limit", [
    ("large.txt", b"too large", 3),
    ("binary.txt", b"a\x00b", 100),
    (".env", b"secret", 100),
])
def test_incomplete_and_refused_files_declare_no_edits(tmp_path, name, raw, limit):
    path = tmp_path / name
    path.write_bytes(raw)
    held = File(path)
    assert held.editing_projection(limit)["actions"] == []
    with pytest.raises(AppError):
        held.set_editing(True, limit)
    assert path.read_bytes() == raw

from pathlib import Path
import pytest
from rey_lib.files.file import File
from rey_lib.errors.error_utils import AppError


def test_file_owns_its_source_focus_and_projects_a_copy():
    held = File(Path('module.py'))
    assert held.location is None
    held.focus(line=72)
    assert held.location == {'line':72, 'revision':1}
    snapshot = held.location
    snapshot['line'] = 1
    assert held.location['line'] == 72
    held.focus(line=72)
    assert held.location == {'line':72, 'revision':2}


@pytest.mark.parametrize('line', [0, -1, True, 1.5, '72', None])
def test_invalid_focus_does_not_change_the_file(line):
    held = File(Path('module.py'))
    held.focus(line=4)
    with pytest.raises(AppError, match='positive source line'):
        held.focus(line=line)
    assert held.location == {'line':4, 'revision':1}

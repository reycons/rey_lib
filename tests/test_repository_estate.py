"""Read-only cross-repository evidence; no provider or AI service required."""
from pathlib import Path
import pytest
from rey_lib.encryption import sha256_text
from rey_lib.errors.error_utils import AppError
from rey_lib.git.repo import run_git
from rey_lib.repository_map.estate import RepositoryEstate


@pytest.fixture
def estate(tmp_path):
    for name, text in [('frontend', 'from backend import handle\nhandle()\n'),
                       ('backend', 'def handle():\n    raise ValueError("broken")\n')]:
        repo = tmp_path / name
        repo.mkdir()
        (repo / 'code.py').write_text(text)
        run_git(repo, 'init', '-q')
        run_git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'add', '.')
        run_git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    return RepositoryEstate(tmp_path)


def test_follow_a_call_across_repositories_and_later_source_spans(estate):
    result = estate.search('handle')
    assert {r['repository_key'] for r in result['matches']} == {'frontend', 'backend'}
    caller = estate.read_source('frontend', 'code.py')
    target = estate.read_source('backend', 'code.py', start_line=2, line_count=1)
    assert 'handle()' in caller['text']
    assert 'raise ValueError' in target['text']
    assert len(caller['revision']) == len(target['revision']) == 40
    assert caller['content_hash'] == sha256_text('from backend import handle\nhandle()\n')
    assert target['start_line'] == target['end_line'] == 2
    assert target['numbered_text'] == '2:     raise ValueError("broken")\n'
    assert target['source'] == 'working_tree' and target['atomic_snapshot'] is False
    # Pagination is across the authorized estate, not limited to the first hit.
    one = estate.search('handle', limit=1)
    two = estate.search('handle', limit=1, offset=one['next_offset'])
    assert one['matches'] != two['matches']


@pytest.mark.parametrize('path', ['../outside.py', '/etc/passwd', '.env', '.git/config', 'foo/../../code.py', 'password.key'])
def test_path_policy_refuses_escape_hidden_and_secret_source(estate, path):
    for operation in (estate.read_source, estate.diff):
        with pytest.raises(AppError):
            operation('backend', path)


def test_symlinks_are_refused_in_every_read_path(estate, tmp_path):
    target = tmp_path / 'backend' / 'link.py'
    target.symlink_to(tmp_path / 'frontend' / 'code.py')
    with pytest.raises(AppError, match='symlink'):
        estate.read_source('backend', 'link.py')
    assert not any(r['relative_path']=='link.py' for r in estate.search('handle')['matches'])


def test_diff_covers_staged_unstaged_untracked_and_does_not_write(estate):
    root = estate.root / 'backend'
    path = root / 'code.py'
    path.write_text('def handle():\n    return "staged"\n')
    run_git(root, 'add', 'code.py')
    path.write_text('def handle():\n    return "working"\n')
    (root / 'new.py').write_text('new = True\n')
    before = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    read = estate.diff('backend', 'code.py')
    new = estate.diff('backend', 'new.py')
    assert '+    return "staged"' in read['staged']
    assert '+    return "working"' in read['unstaged']
    assert new['untracked'] and not new['staged'] and not new['unstaged']
    assert estate.read_source('backend', 'new.py')['text'] == 'new = True\n'
    assert read['changes_applied'] is False
    after = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert before == after


def test_deleted_path_has_a_diff_and_no_working_source(estate):
    (estate.root / 'backend' / 'code.py').unlink()
    result = estate.diff('backend', 'code.py')
    assert '-def handle():' in result['unstaged'] and result['content_hash'] is None


def test_limits_and_exclusions_are_explicit(estate):
    with pytest.raises(AppError):
        estate.read_source('backend', 'code.py', line_count=401)
    (estate.root / 'backend' / 'secret_values.py').write_text('DO NOT EXPOSE\n')
    assert estate.search('DO NOT EXPOSE')['matches'] == []
    estate.max_source_bytes = 2
    with pytest.raises(AppError, match='bounded'):
        estate.read_source('backend', 'code.py')


def test_diff_cannot_pass_executable_options_and_binary_is_refused(estate):
    root = estate.root / 'backend'
    (root / 'binary.py').write_bytes(b'\x00binary')
    with pytest.raises(AppError, match='Binary'):
        estate.read_source('backend', 'binary.py')
    with pytest.raises(TypeError):
        estate.read_source('backend', 'code.py', command='touch bad')

"""The live env-key path, which had no tests at all.

That absence is why rey_lib.encryption could raise ImportError for ten weeks
without anything noticing, taking the whole trade_analyzer CLI with it. These
tests cover ensure_env_key, its one real caller's use of it, and the import
itself.

Every test writes to a pytest temp path. Nothing here reads or writes a real
.env file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rey_lib.encryption import ensure_env_key, generate_fernet_key


def _names_in(env_file: Path) -> list[str]:
    """Variable names declared in the file, in order."""
    return [
        line.split("=", 1)[0].strip()
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    ]


def test_the_module_imports() -> None:
    """The regression that mattered.

    A missing name in an import line is invisible until something imports the
    module, and nothing did.
    """
    import rey_lib.encryption as encryption

    assert encryption.ensure_env_key is ensure_env_key


def test_a_missing_key_is_generated_and_written(tmp_path: Path) -> None:
    env_file = tmp_path / "env"

    created = ensure_env_key(env_file, "TRADE_ENCRYPTION_KEY")

    assert created is True
    assert _names_in(env_file) == ["TRADE_ENCRYPTION_KEY"]


def test_an_existing_key_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    """The value must not be regenerated: it decrypts existing data."""
    env_file = tmp_path / "env"
    env_file.write_text("TRADE_ENCRYPTION_KEY=original-value\n", encoding="utf-8")

    created = ensure_env_key(env_file, "TRADE_ENCRYPTION_KEY")

    assert created is False
    assert env_file.read_text(encoding="utf-8") == "TRADE_ENCRYPTION_KEY=original-value\n"


def test_other_variables_and_comments_survive(tmp_path: Path) -> None:
    """Appending must not rewrite a file someone else owns."""
    env_file = tmp_path / "env"
    original = "# a comment\nEXISTING=1\n\nOTHER=two\n"
    env_file.write_text(original, encoding="utf-8")

    ensure_env_key(env_file, "NEW_KEY")

    content = env_file.read_text(encoding="utf-8")
    assert content.startswith(original)
    assert _names_in(env_file) == ["EXISTING", "OTHER", "NEW_KEY"]


def test_a_file_without_a_trailing_newline_does_not_join_two_variables(
    tmp_path: Path,
) -> None:
    """The concatenation bug this deserves a test for."""
    env_file = tmp_path / "env"
    env_file.write_text("EXISTING=1", encoding="utf-8")  # no newline

    ensure_env_key(env_file, "NEW_KEY")

    assert _names_in(env_file) == ["EXISTING", "NEW_KEY"]
    assert "1NEW_KEY" not in env_file.read_text(encoding="utf-8")


def test_a_commented_out_variable_does_not_count_as_declared(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("# NEW_KEY=disabled\n", encoding="utf-8")

    created = ensure_env_key(env_file, "NEW_KEY")

    assert created is True
    assert _names_in(env_file) == ["NEW_KEY"]


def test_the_parent_directory_is_created(tmp_path: Path) -> None:
    env_file = tmp_path / "nested" / "deeper" / "env"

    assert ensure_env_key(env_file, "KEY") is True
    assert env_file.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission model")
def test_the_file_is_not_left_world_readable(tmp_path: Path) -> None:
    """A generated key is a secret from the moment it is written."""
    env_file = tmp_path / "env"
    ensure_env_key(env_file, "KEY")

    mode = stat.S_IMODE(env_file.stat().st_mode)

    assert mode == 0o600, oct(mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission model")
def test_permissions_are_tightened_even_when_the_key_already_exists(
    tmp_path: Path,
) -> None:
    """The early return still secures the file rather than skipping it."""
    env_file = tmp_path / "env"
    env_file.write_text("KEY=value\n", encoding="utf-8")
    env_file.chmod(0o644)

    assert ensure_env_key(env_file, "KEY") is False
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_generated_keys_are_distinct(tmp_path: Path) -> None:
    """Two calls must not produce the same key."""
    pytest.importorskip("cryptography")

    assert generate_fernet_key() != generate_fernet_key()


def test_the_retired_config_driven_generator_is_gone() -> None:
    """ensure_generated_env_keys read config/config.<env>.yaml.

    Env-based config was removed in 3080748; no such file exists anywhere in
    the tree, the function had no callers, and it raised for every possible
    input. This asserts it does not come back by accident.
    """
    import rey_lib.encryption as encryption

    assert not hasattr(encryption, "ensure_generated_env_keys")
    assert "ensure_generated_env_keys" not in encryption.__all__


def test_encryption_depends_on_nothing_else_in_rey_lib() -> None:
    """A primitive with no reverse dependency on the packages that use it.

    file_utils delegates its digests here, so an import back into rey_lib.files
    would be a cycle. Deleting the config-driven generator removed the last
    reason for one.
    """
    source = Path(encryption_file()).read_text(encoding="utf-8")
    imported = [
        line.strip()
        for line in source.splitlines()
        if line.startswith("from rey_lib") or line.startswith("import rey_lib")
    ]

    assert imported == ["from rey_lib.errors.error_utils import ConfigError"]


def encryption_file() -> str:
    """Return the path to the encryption module source."""
    import rey_lib.encryption as encryption

    return encryption.__file__

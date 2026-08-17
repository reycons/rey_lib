"""Environment references through top-level env declarations.

A profile declares the name it wants and keeps that name: configuration says
which variable holds a value, and the subsystem that uses the value reads it at
the moment it is used. So the finalized context holds references and never
values, and there is nothing resolved in it to expose.

Loading the installation's .env is separate from that, and still happens during
the build: it populates the process environment so a consumer has something to
read later. These use dummy values supplied by the test process, so nothing here
depends on any real credential.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rey_lib.config.config_context import build_ctx_from_path
from rey_lib.errors.error_utils import ConfigError

_INSTALLATION = """
installation:
  name: fixture

paths:
  - name: root
    path: '{config_dir}'

env:
  - name: openai_api_key
    env_var: FIXTURE_OPENAI_API_KEY
    generate: false
  - name: gemini_api_key
    env_var: FIXTURE_GEMINI_API_KEY
    generate: false

llm:
  - name: hosted
    provider: openai
    model: gpt-4o
    api_key: env.openai_api_key
  - name: gemini
    provider: gemini
    model: gemini-2.5-flash
    api_key: env.gemini_api_key
  - name: local
    provider: ollama
    model: qwen2.5-coder:32b
    api_key: ''

config_loading:
  default_behavior: none
  apps:
    fixture_app:
      include: []
"""


def _write(tmp_path: Path, text: str) -> Path:
    config_dir = tmp_path / "install"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "installation.yaml"
    path.write_text(text.replace("{config_dir}", str(tmp_path)), encoding="utf-8")
    return path


def test_a_profile_keeps_the_reference_it_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile declares env.<name> and keeps it, set variable or not."""
    monkeypatch.setenv("FIXTURE_OPENAI_API_KEY", "dummy-openai")
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    ctx = build_ctx_from_path(_write(tmp_path, _INSTALLATION), app_name="fixture_app")
    profiles = {profile.name: profile for profile in ctx.llm}

    assert profiles["hosted"].api_key == "env.openai_api_key"
    assert profiles["gemini"].api_key == "env.gemini_api_key"
    # A keyless provider keeps its empty key rather than acquiring one.
    assert profiles["local"].api_key == ""
    # The value was there to be taken, and was not taken.
    assert "dummy-openai" not in {profile.api_key for profile in ctx.llm}


def test_a_missing_variable_neither_fails_the_build_nor_empties_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is read during the build, so nothing can be missing yet.

    The reference stays whole, to be resolved when the provider asks for it --
    and the provider reports its own missing-credential failure then. An empty
    key stored here would lose the name and turn a clear failure into a request
    sent with no credential at all.
    """
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    ctx = build_ctx_from_path(_write(tmp_path, _INSTALLATION), app_name="fixture_app")
    profiles = {profile.name: profile for profile in ctx.llm}

    assert profiles["hosted"].api_key == "env.openai_api_key"


def test_an_undeclared_reference_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reference with no declaration stops config loading.

    This is why the declarations live in a shared file every app includes: an
    app scope that reached a profile without them would not start.
    """
    monkeypatch.setenv("FIXTURE_OPENAI_API_KEY", "dummy-openai")
    undeclared = _INSTALLATION.replace("env.gemini_api_key", "env.never_declared")

    with pytest.raises(ConfigError, match="Unknown env reference"):
        build_ctx_from_path(_write(tmp_path, undeclared), app_name="fixture_app")


_WITH_ROOT = _INSTALLATION.replace(
    """paths:
  - name: root
    path: '{config_dir}'
""",
    """paths:
  - name: root
    path: '{config_dir}'
  - name: installation_root
    path: '{config_dir}'
""",
)


def test_the_env_file_is_read_from_the_declared_installation_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared installation_root is where the installation's .env lives.

    Installations lay their configuration out differently, so the root is
    declared rather than derived from the config file's position.
    """
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FIXTURE_GEMINI_API_KEY", raising=False)

    config_path = _write(tmp_path, _WITH_ROOT)
    # The root is tmp_path; the config lives a directory below it.
    (tmp_path / ".env").write_text(
        "FIXTURE_OPENAI_API_KEY=from-installation-root\n"
        "FIXTURE_GEMINI_API_KEY=gemini-from-root\n",
        encoding="utf-8",
    )

    build_ctx_from_path(config_path, app_name="fixture_app")

    # Into the process environment, for a consumer to read later; the context
    # itself holds the reference.
    assert os.environ["FIXTURE_OPENAI_API_KEY"] == "from-installation-root"
    assert os.environ["FIXTURE_GEMINI_API_KEY"] == "gemini-from-root"


def test_without_a_declared_root_the_config_directory_is_still_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The long-standing location stays the default for undeclared installations."""
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    config_path = _write(tmp_path, _INSTALLATION)
    (config_path.parent / ".env").write_text(
        "FIXTURE_OPENAI_API_KEY=beside-the-config\n", encoding="utf-8"
    )

    build_ctx_from_path(config_path, app_name="fixture_app")

    assert os.environ["FIXTURE_OPENAI_API_KEY"] == "beside-the-config"


def test_a_root_env_file_is_not_read_when_no_root_is_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No upward search: an undeclared installation never reaches outside itself."""
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    config_path = _write(tmp_path, _INSTALLATION)
    (tmp_path / ".env").write_text(
        "FIXTURE_OPENAI_API_KEY=should-not-be-read\n", encoding="utf-8"
    )

    build_ctx_from_path(config_path, app_name="fixture_app")

    assert "FIXTURE_OPENAI_API_KEY" not in os.environ


def test_a_real_environment_value_still_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_dotenv runs with override=False, so the process environment leads."""
    monkeypatch.setenv("FIXTURE_OPENAI_API_KEY", "from-the-process")
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    config_path = _write(tmp_path, _WITH_ROOT)
    (tmp_path / ".env").write_text(
        "FIXTURE_OPENAI_API_KEY=from-the-file\n", encoding="utf-8"
    )

    build_ctx_from_path(config_path, app_name="fixture_app")

    assert os.environ["FIXTURE_OPENAI_API_KEY"] == "from-the-process"


def test_the_declaration_block_is_not_rewritten_by_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env block keeps naming variables; it never holds their values."""
    monkeypatch.setenv("FIXTURE_OPENAI_API_KEY", "dummy-openai")
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    ctx = build_ctx_from_path(_write(tmp_path, _INSTALLATION), app_name="fixture_app")
    declared = {entry.name: entry.env_var for entry in ctx.env}

    assert declared["openai_api_key"] == "FIXTURE_OPENAI_API_KEY"
    assert "dummy-openai" not in declared.values()


def test_the_nested_form_declares_the_variable_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So one lookup rule serves both spellings.

    The direct form names a declared entry; the nested form names the variable.
    Declaring the latter under its own name means whoever resolves a reference
    later always finds it in the same block, with no second way to read one.
    """
    monkeypatch.setenv("FIXTURE_MESSAGING_PASSWORD", "dummy-messaging")
    nested = _INSTALLATION + """
messaging:
  user: someone
  env:
    password: FIXTURE_MESSAGING_PASSWORD
"""

    ctx = build_ctx_from_path(_write(tmp_path, nested), app_name="fixture_app")
    declared = {entry.name: entry.env_var for entry in ctx.env}

    assert ctx.messaging.password == "env.FIXTURE_MESSAGING_PASSWORD"
    assert declared["FIXTURE_MESSAGING_PASSWORD"] == "FIXTURE_MESSAGING_PASSWORD"
    # The declarations written by hand are still there, unchanged.
    assert declared["openai_api_key"] == "FIXTURE_OPENAI_API_KEY"

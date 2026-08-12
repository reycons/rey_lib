"""Secret resolution through top-level env declarations.

Configuration resolution is the single owner of secrets: a profile declares the
name it wants and receives the value, and no provider reads the environment for
itself. These use dummy values supplied by the test process, so nothing here
depends on a .env file or on any real credential.
"""

from __future__ import annotations

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


def test_declared_secrets_reach_the_profile_that_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile declares env.<name> and receives that variable's value."""
    monkeypatch.setenv("FIXTURE_OPENAI_API_KEY", "dummy-openai")
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    ctx = build_ctx_from_path(_write(tmp_path, _INSTALLATION), app_name="fixture_app")
    profiles = {profile.name: profile for profile in ctx.llm}

    assert profiles["hosted"].api_key == "dummy-openai"
    assert profiles["gemini"].api_key == "dummy-gemini"
    # A keyless provider keeps its empty key rather than acquiring one.
    assert profiles["local"].api_key == ""


def test_a_missing_variable_resolves_empty_rather_than_leaking_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset variable yields an empty key, never the literal 'env.<name>'.

    The provider then reports its own missing-credential failure, which is a
    clearer error than a request carrying 'env.openai_api_key' as the key.
    """
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    ctx = build_ctx_from_path(_write(tmp_path, _INSTALLATION), app_name="fixture_app")
    profiles = {profile.name: profile for profile in ctx.llm}

    assert profiles["hosted"].api_key == ""
    assert "env." not in profiles["hosted"].api_key


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

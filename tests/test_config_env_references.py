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


def _with_env_file(declared: str) -> str:
    """The fixture installation, declaring ``declared`` as its environment file."""
    return _INSTALLATION.replace(
        "installation:\n  name: fixture",
        f"installation:\n  name: fixture\n\nsecurity:\n  env_file: \"{declared}\"",
    )


def test_the_declared_file_is_read_wherever_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """security.env_file is the whole answer, and it may be anywhere.

    The file here is outside the installation entirely and is not named .env,
    which is the point: an operator moves the environment file without the
    loader knowing anything about the installation's layout.
    """
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FIXTURE_GEMINI_API_KEY", raising=False)

    secrets = tmp_path.parent / f"{tmp_path.name}-secrets"
    secrets.mkdir(exist_ok=True)
    env_file = secrets / "runtime.env"
    env_file.write_text(
        "FIXTURE_OPENAI_API_KEY=from-the-declared-file\n"
        "FIXTURE_GEMINI_API_KEY=gemini-from-the-declared-file\n",
        encoding="utf-8",
    )

    build_ctx_from_path(_write(tmp_path, _with_env_file(str(env_file))), app_name="fixture_app")

    assert os.environ["FIXTURE_OPENAI_API_KEY"] == "from-the-declared-file"
    assert os.environ["FIXTURE_GEMINI_API_KEY"] == "gemini-from-the-declared-file"


def test_an_undeclared_installation_reads_no_environment_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is probed. An installation declaring no file has none.

    Both directories the old lookup used are seeded here -- the installation
    root and the directory holding the config -- and neither is read. That is
    the hidden convention being gone, stated as a test rather than assumed.
    """
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FIXTURE_GEMINI_API_KEY", raising=False)

    config_path = _write(tmp_path, _INSTALLATION)
    (tmp_path / ".env").write_text(
        "FIXTURE_OPENAI_API_KEY=root-should-not-be-read\n", encoding="utf-8"
    )
    (config_path.parent / ".env").write_text(
        "FIXTURE_GEMINI_API_KEY=config-dir-should-not-be-read\n", encoding="utf-8"
    )

    build_ctx_from_path(config_path, app_name="fixture_app")

    assert "FIXTURE_OPENAI_API_KEY" not in os.environ
    assert "FIXTURE_GEMINI_API_KEY" not in os.environ


def test_a_relative_declaration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path would have to be resolved against something.

    Every candidate base -- the config file, the installation root, the working
    directory -- is a convention this declaration exists to remove, so the
    ambiguity is refused rather than resolved.
    """
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    (tmp_path / "install" / ".env").parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ConfigError, match="must be an absolute path"):
        build_ctx_from_path(_write(tmp_path, _with_env_file(".env")), app_name="fixture_app")


def test_a_declared_file_that_is_not_there_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declaring a file and finding nothing is an error, not an absence.

    Skipping it silently would let the run proceed without values it was told
    exist, and fail later as an unresolved reference naming a variable rather
    than the file that should have held it.
    """
    monkeypatch.delenv("FIXTURE_OPENAI_API_KEY", raising=False)
    absent = str(tmp_path / "nowhere" / "runtime.env")

    with pytest.raises(ConfigError, match="does not exist"):
        build_ctx_from_path(_write(tmp_path, _with_env_file(absent)), app_name="fixture_app")


def test_a_real_environment_value_still_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_dotenv runs with override=False, so the process environment leads."""
    monkeypatch.setenv("FIXTURE_OPENAI_API_KEY", "from-the-process")
    monkeypatch.setenv("FIXTURE_GEMINI_API_KEY", "dummy-gemini")

    env_file = tmp_path / "runtime.env"
    env_file.write_text("FIXTURE_OPENAI_API_KEY=from-the-file\n", encoding="utf-8")

    build_ctx_from_path(
        _write(tmp_path, _with_env_file(str(env_file))), app_name="fixture_app"
    )

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

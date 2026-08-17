"""
A configured environment reference survives into the finalized context.

The finalized context holds references, never values. That is what makes it
safe to serialize, log, or hand to a caller: there is nothing resolved in it to
expose. Every assertion here names environment variables only; none reads or
prints a resolved value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rey_lib.config.config_context import build_ctx_from_path
from rey_lib.config.config_utils import Namespace

APPS_VAR = "REY_TEST_APPS_PASSWORD"
NESTED_VAR = "REY_TEST_NESTED_PASSWORD"
KEY_VAR = "REY_TEST_OPENAI_KEY"
HOST_VAR = "REY_TEST_HOST"
PORT_VAR = "REY_TEST_PORT"


def _write(config_dir: Path, name: str, data: dict) -> None:
    (config_dir / name).write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    """One installation config using both spellings of a reference."""
    config = tmp_path / "config"
    config.mkdir()
    _write(config, "config.yaml", {
        "app": "test_app",
        "env": [
            {"name": APPS_VAR, "env_var": APPS_VAR},
            {"name": KEY_VAR, "env_var": KEY_VAR},
            {"name": HOST_VAR, "env_var": HOST_VAR},
            {"name": PORT_VAR, "env_var": PORT_VAR},
        ],
        # The direct spelling, on fields of every kind -- a secret-sounding one
        # and two ordinary ones, treated identically.
        "connections": [
            {
                "name": "rey_apps",
                "host": f"env.{HOST_VAR}",
                "port": f"env.{PORT_VAR}",
                "password": f"env.{APPS_VAR}",
            },
        ],
        "llm": [{"name": "openai", "api_key": f"env.{KEY_VAR}"}],
        # The nested spelling: a map of target attribute to variable.
        "messaging": {"user": "someone", "env": {"password": NESTED_VAR}},
    })
    return config / "config.yaml"


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch):
    """Every referenced variable is set, so resolution would be visible."""
    for name in (APPS_VAR, NESTED_VAR, KEY_VAR, HOST_VAR, PORT_VAR):
        monkeypatch.setenv(name, f"resolved-{name}")
    return monkeypatch


def _connection(ctx: Namespace, name: str):
    for entry in getattr(ctx, "connections", []) or []:
        if str(getattr(entry, "name", "")) == name:
            return entry
    raise AssertionError(f"no connection named {name}")


def test_finalized_ctx_preserves_env_reference(config_path: Path) -> None:
    ctx = build_ctx_from_path(config_path, app_name="test_app")
    assert _connection(ctx, "rey_apps").password == f"env.{APPS_VAR}"


def test_multiple_env_field_types_remain_symbolic(config_path: Path) -> None:
    """A host and a port are treated exactly like a password."""
    ctx = build_ctx_from_path(config_path, app_name="test_app")
    connection = _connection(ctx, "rey_apps")
    assert connection.host == f"env.{HOST_VAR}"
    assert connection.port == f"env.{PORT_VAR}"
    assert connection.password == f"env.{APPS_VAR}"
    assert getattr(ctx, "llm")[0].api_key == f"env.{KEY_VAR}"


def test_nested_env_block_becomes_the_same_symbolic_form(config_path: Path) -> None:
    """Both spellings mean the same thing, so both end up the same."""
    ctx = build_ctx_from_path(config_path, app_name="test_app")
    assert ctx.messaging.password == f"env.{NESTED_VAR}"


def test_no_resolved_value_appears_anywhere_in_the_context(config_path: Path) -> None:
    """The whole context, walked. Nothing in it is a resolved value."""
    ctx = build_ctx_from_path(config_path, app_name="test_app")
    resolved = {f"resolved-{name}" for name in (APPS_VAR, NESTED_VAR, KEY_VAR, HOST_VAR, PORT_VAR)}

    def walk(value, seen=None):
        seen = seen or set()
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, Namespace):
            for _, child in value.items():
                yield from walk(child, seen)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item, seen)
        elif isinstance(value, str):
            yield value

    assert resolved.isdisjoint(set(walk(ctx)))


def test_missing_env_variable_does_not_fail_the_build(
    config_path: Path, environment: pytest.MonkeyPatch,
) -> None:
    """Nothing is read at build time, so nothing can be missing yet."""
    environment.delenv(APPS_VAR, raising=False)
    ctx = build_ctx_from_path(config_path, app_name="test_app")
    # And it is not quietly emptied either: the reference is still there to
    # resolve, and failing is the consumer's business.
    assert _connection(ctx, "rey_apps").password == f"env.{APPS_VAR}"


def test_changing_the_environment_does_not_change_the_stored_reference(
    config_path: Path, environment: pytest.MonkeyPatch,
) -> None:
    ctx = build_ctx_from_path(config_path, app_name="test_app")
    environment.setenv(APPS_VAR, "something-else-entirely")
    assert _connection(ctx, "rey_apps").password == f"env.{APPS_VAR}"


def test_unknown_reference_is_still_a_configuration_error(tmp_path: Path) -> None:
    """A reference nobody declared is a mistake in the configuration.

    Unrelated to whether the variable is set: that is checked where it is used.
    """
    from rey_lib.errors.error_utils import ConfigError

    config = tmp_path / "config"
    config.mkdir()
    _write(config, "config.yaml", {
        "app": "test_app",
        "env": [{"name": APPS_VAR, "env_var": APPS_VAR}],
        "connections": [{"name": "x", "password": "env.NEVER_DECLARED"}],
    })
    with pytest.raises(ConfigError, match="Unknown env reference"):
        build_ctx_from_path(config / "config.yaml", app_name="test_app")


def test_db_connections_alias_still_reaches_connections(tmp_path: Path) -> None:
    """Alias behaviour is untouched by this change."""
    config = tmp_path / "config"
    config.mkdir()
    _write(config, "config.yaml", {
        "app": "test_app",
        "env": [{"name": APPS_VAR, "env_var": APPS_VAR}],
        "db_connections": [{"name": "rey_apps", "password": f"env.{APPS_VAR}"}],
    })
    ctx = build_ctx_from_path(config / "config.yaml", app_name="test_app")
    assert _connection(ctx, "rey_apps").password == f"env.{APPS_VAR}"
    assert getattr(ctx, "db_connections", None) is not None


def test_serialization_shows_references_not_values(config_path: Path) -> None:
    """What a caller can be handed contains references only."""
    from rey_lib.config.inventory import _thaw

    ctx = build_ctx_from_path(config_path, app_name="test_app")
    text = repr(_thaw(_connection(ctx, "rey_apps")))
    assert f"env.{APPS_VAR}" in text
    assert "resolved-" not in text

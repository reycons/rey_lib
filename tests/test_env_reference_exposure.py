"""Nothing resolved escapes through a generic surface.

Configuration reaches a great many places that were never written with secrets
in mind: an inventory dump, an API response, a debug log, a Tree payload. This
is what makes that safe -- not a rule about which field names to hide, but that
the context holds references and there is no resolved value in it to escape.

Each test puts a sentinel in the environment, behind a reference, and proves the
sentinel is not in what the surface produced. The sentinel is a value this test
process invented; no real credential is involved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from rey_lib.config.config_context import build_ctx_from_path
from rey_lib.config.inventory import build_installation_inventory, to_plain_data

PASSWORD_VAR = "REY_TEST_EXPOSURE_PASSWORD"
KEY_VAR = "REY_TEST_EXPOSURE_API_KEY"
HOST_VAR = "REY_TEST_EXPOSURE_HOST"

#: What must never appear in anything a surface hands out.
SENTINELS = {
    f"sentinel-{PASSWORD_VAR}",
    f"sentinel-{KEY_VAR}",
    f"sentinel-{HOST_VAR}",
}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every referenced variable is set, so a leak would be visible."""
    for name in (PASSWORD_VAR, KEY_VAR, HOST_VAR):
        monkeypatch.setenv(name, f"sentinel-{name}")


@pytest.fixture()
def ctx(tmp_path: Path) -> Any:
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text(yaml.safe_dump({
        "app": "test_app",
        "app_name": "test_app",
        "env": [
            {"name": PASSWORD_VAR, "env_var": PASSWORD_VAR},
            {"name": KEY_VAR, "env_var": KEY_VAR},
            {"name": HOST_VAR, "env_var": HOST_VAR},
        ],
        "apps": [{"name": "test_app"}],
        "connections": [{
            "name": "primary",
            "provider": "postgres",
            # An ordinary field and a secret-sounding one, treated alike.
            "host": f"env.{HOST_VAR}",
            "database": "reporting",
            "username": "reporting_reader",
            "password": f"env.{PASSWORD_VAR}",
        }],
        "llm_profiles": [{
            "name": "hosted",
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": f"env.{KEY_VAR}",
        }],
        "logging": {"level": "DEBUG", "token": f"env.{KEY_VAR}"},
        "messaging": {"user": "someone", "env": {"password": PASSWORD_VAR}},
    }), encoding="utf-8")
    return build_ctx_from_path(config / "config.yaml", app_name="test_app")


def leaked(payload: Any) -> set[str]:
    """Sentinels found anywhere in a payload, however deeply."""
    text = json.dumps(payload, default=str)
    return {sentinel for sentinel in SENTINELS if sentinel in text}


# -- the generic serializers -------------------------------------------------

def test_to_plain_data_returns_the_reference_it_was_given(ctx: Any) -> None:
    plain = to_plain_data(ctx.connections)

    assert plain[0]["password"] == f"env.{PASSWORD_VAR}"
    assert plain[0]["host"] == f"env.{HOST_VAR}"
    assert leaked(plain) == set()


def test_the_inventory_dump_carries_no_resolved_value(ctx: Any) -> None:
    """to_dict() is the whole inventory, walked and flattened."""
    inventory = build_installation_inventory(ctx)
    dumped = inventory.to_dict()

    assert leaked(dumped) == set()
    connections = {entry["name"]: entry for entry in dumped["connections"]}
    assert connections["primary"]["password"] == f"env.{PASSWORD_VAR}"


def test_thawing_the_inventory_carries_no_resolved_value(ctx: Any) -> None:
    """_thaw is what to_dict is made of; proven in its own right."""
    from rey_lib.config.inventory import _thaw

    inventory = build_installation_inventory(ctx)

    assert leaked(_thaw(inventory)) == set()
    assert leaked(_thaw(inventory.llm_profiles)) == set()


def test_a_normalized_nested_reference_is_also_only_a_reference(ctx: Any) -> None:
    plain = to_plain_data(ctx.messaging)

    assert plain["password"] == f"env.{PASSWORD_VAR}"
    assert leaked(plain) == set()


# -- debug logging -----------------------------------------------------------

def test_debug_output_prints_references_and_needs_no_masking(
    ctx: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole context at DEBUG, which is the loudest surface there is."""
    from rey_lib.config.config_context import _print_namespace

    with caplog.at_level(logging.DEBUG):
        _print_namespace(ctx, 0)
    printed = caplog.text

    assert leaked(printed) == set()
    # Printed as the reference it is, rather than hidden behind a mask.
    assert f"env.{PASSWORD_VAR}" in printed
    assert "***" not in printed


def test_debug_output_treats_every_field_the_same(
    ctx: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    """No field name is special, so none is quietly handled differently."""
    from rey_lib.config.config_context import _print_namespace

    with caplog.at_level(logging.DEBUG):
        _print_namespace(ctx.connections[0], 0)
        _print_namespace(ctx.logging, 0)
    printed = caplog.text

    # password, host and token: one rule, one appearance each.
    assert f"password: env.{PASSWORD_VAR}" in printed
    assert f"host: env.{HOST_VAR}" in printed
    assert f"token: env.{KEY_VAR}" in printed


# -- structural --------------------------------------------------------------

EXPOSURE_MODULES = (
    "rey_lib/config/inventory.py",
    "rey_lib/config/config_context.py",
)


@pytest.mark.parametrize("relative", EXPOSURE_MODULES)
def test_an_exposure_surface_never_resolves_anything(relative: str) -> None:
    """A serializer that could resolve would only have to be asked once.

    Config construction validates references, which is why config_context is
    listed here: it must know the syntax without ever reading a value.
    """
    source = (Path(__file__).resolve().parent.parent / relative).read_text(encoding="utf-8")

    assert "resolve_env_reference" not in source
    assert "os.environ" not in source
    assert "os.getenv" not in source


def test_no_serializer_hides_a_field_by_its_name() -> None:
    """Masking would mean a resolved value was expected to be there.

    It would also be the wrong shape for the job: a guess at which names hold
    secrets hides an ordinary field and misses an unrecognised one. What keeps
    these surfaces safe is that nothing resolved reaches them.
    """
    package = Path(__file__).resolve().parent.parent / "rey_lib"
    masking = ('"***"', "'***'")
    offenders = [
        path.relative_to(package.parent).as_posix()
        for path in sorted(package.rglob("*.py"))
        if not any(part.startswith(".") for part in path.parts)
        and any(mask in path.read_text(encoding="utf-8") for mask in masking)
    ]
    assert offenders == []

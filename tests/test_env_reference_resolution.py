"""Resolution at the point of use, through the one resolver.

Configuration names an environment variable; the subsystem that needs the value
reads it as it uses it. These prove both halves: that the resolver reads a
reference the one way it is meant to be read, and that each migrated consumer
reads only the field it is about to use, only when it uses it.

Every assertion names environment variables and compares against values this
test process set itself. Nothing here depends on a real credential.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from rey_lib.config.config_context import build_ctx_from_path
from rey_lib.config.env_reference import resolve_env_reference
from rey_lib.errors.error_utils import ConfigError

DIRECT_VAR = "REY_TEST_DIRECT_SECRET"
NESTED_VAR = "REY_TEST_NESTED_SECRET"
USER_VAR = "REY_TEST_USER"
KEY_VAR = "REY_TEST_API_KEY"

# The declaration name differs from the variable it names, which is what makes
# these tests able to tell the two apart.
DIRECT_DECLARATION = "direct_secret"


@pytest.fixture()
def ctx(tmp_path: Path):
    """A finalized context using both spellings of a reference."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text(yaml.safe_dump({
        "app": "test_app",
        "env": [
            {"name": DIRECT_DECLARATION, "env_var": DIRECT_VAR},
            {"name": USER_VAR, "env_var": USER_VAR},
            {"name": KEY_VAR, "env_var": KEY_VAR},
        ],
        "connections": [{
            "name": "primary",
            "provider": "postgres",
            "host": "db.example.internal",
            "port": 5432,
            "database": "reporting",
            "username": "reporting_reader",
            "user": "reporting_reader",
            "password": f"env.{DIRECT_DECLARATION}",
            "authentication": {"type": "sql_server"},
            "driver": "ODBC Driver 17 for SQL Server",
        }],
        "llm_profiles": [{
            "name": "hosted",
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": f"env.{KEY_VAR}",
        }],
        "ftp_site": {
            "host": "files.example.internal",
            "port": 21,
            "protocol": "ftp",
            "user": f"env.{USER_VAR}",
            "env": {"password": NESTED_VAR},
        },
    }), encoding="utf-8")
    return build_ctx_from_path(config / "config.yaml", app_name="test_app")


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch):
    for name in (DIRECT_VAR, NESTED_VAR, USER_VAR, KEY_VAR):
        monkeypatch.setenv(name, f"value-of-{name}")
    return monkeypatch


def connection(ctx: Any):
    return ctx.connections[0]


# -- the resolver ------------------------------------------------------------

@pytest.mark.parametrize("value", ["plain-text", "", 5432, None, True, ["env.x"]])
def test_a_value_that_names_nothing_is_returned_as_it_is(ctx: Any, value: Any) -> None:
    """So a caller may pass any field without first asking what kind it is."""
    assert resolve_env_reference(ctx, value) == value


def test_a_direct_reference_resolves_through_its_declaration(ctx: Any) -> None:
    assert resolve_env_reference(ctx, connection(ctx).password) == f"value-of-{DIRECT_VAR}"


def test_the_suffix_is_a_declaration_name_and_not_a_variable_name(
    ctx: Any, environment: pytest.MonkeyPatch,
) -> None:
    """The one way a reference is read.

    A variable named after the declaration rather than the declared env_var
    must not be picked up: that would be a second way to read a reference, and
    whichever one answered first would decide.
    """
    environment.setenv(DIRECT_DECLARATION, "value-of-the-wrong-variable")
    assert resolve_env_reference(ctx, connection(ctx).password) == f"value-of-{DIRECT_VAR}"


def test_a_normalized_nested_reference_resolves_the_same_way(ctx: Any) -> None:
    """The nested spelling arrives as a declaration like any other."""
    assert ctx.ftp_site.password == f"env.{NESTED_VAR}"
    assert resolve_env_reference(ctx, ctx.ftp_site.password) == f"value-of-{NESTED_VAR}"


def test_an_undeclared_reference_is_refused(ctx: Any) -> None:
    with pytest.raises(ConfigError, match="Unknown env reference"):
        resolve_env_reference(ctx, "env.never_declared")


def test_a_missing_variable_fails_at_use_and_names_itself(
    ctx: Any, environment: pytest.MonkeyPatch,
) -> None:
    """Not during the build, and not as an empty string."""
    environment.delenv(DIRECT_VAR, raising=False)
    with pytest.raises(ConfigError, match=DIRECT_VAR):
        resolve_env_reference(ctx, connection(ctx).password)


def test_a_changed_variable_is_seen_by_the_next_call(
    ctx: Any, environment: pytest.MonkeyPatch,
) -> None:
    """Nothing is cached, so a rotated credential needs no restart."""
    reference = connection(ctx).password
    assert resolve_env_reference(ctx, reference) == f"value-of-{DIRECT_VAR}"
    environment.setenv(DIRECT_VAR, "rotated")
    assert resolve_env_reference(ctx, reference) == "rotated"


def test_resolving_leaves_the_context_holding_the_reference(ctx: Any) -> None:
    """The value goes to the caller and nowhere else."""
    resolve_env_reference(ctx, connection(ctx).password)
    resolve_env_reference(ctx, ctx.ftp_site.password)
    assert connection(ctx).password == f"env.{DIRECT_DECLARATION}"
    assert ctx.ftp_site.password == f"env.{NESTED_VAR}"


# -- the consumers -----------------------------------------------------------

def test_postgres_resolves_the_password_as_it_opens_the_connection(
    ctx: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rey_lib.db import postgres_utils

    opened: dict[str, Any] = {}
    monkeypatch.setattr(postgres_utils, "_psycopg2", lambda: None)
    monkeypatch.setitem(
        __import__("sys").modules, "rey_lib.db._sqlalchemy",
        SimpleNamespace(open_connection=lambda *a, **kw: opened.update(kw) or "connection"),
    )

    assert postgres_utils.get_connection(connection(ctx), ctx=ctx) == "connection"
    assert opened["password"] == f"value-of-{DIRECT_VAR}"
    # And the configuration it came from is unchanged.
    assert connection(ctx).password == f"env.{DIRECT_DECLARATION}"


def test_mysql_resolves_the_password_as_it_opens_the_connection(
    ctx: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rey_lib.db import mysql_utils

    opened: dict[str, Any] = {}
    monkeypatch.setitem(
        __import__("sys").modules, "rey_lib.db._sqlalchemy",
        SimpleNamespace(open_connection=lambda *a, **kw: opened.update(kw) or "connection"),
    )

    assert mysql_utils.get_connection(connection(ctx), ctx=ctx) == "connection"
    assert opened["password"] == f"value-of-{DIRECT_VAR}"
    assert connection(ctx).password == f"env.{DIRECT_DECLARATION}"


def test_sqlserver_resolves_credentials_into_the_connection_string(ctx: Any) -> None:
    from rey_lib.db import sqlserver_utils

    conn_str = sqlserver_utils._build_connection_string(connection(ctx), ctx=ctx)

    assert f"PWD={f'value-of-{DIRECT_VAR}'};" in conn_str
    assert "env." not in conn_str
    assert connection(ctx).password == f"env.{DIRECT_DECLARATION}"


def test_ftp_resolves_user_and_password_at_login(
    ctx: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ftplib

    from rey_lib.ftp import ftp_client

    login: dict[str, Any] = {}

    class Recorder:
        def connect(self, **kwargs: Any) -> None: ...
        def login(self, **kwargs: Any) -> None:
            login.update(kwargs)
        def quit(self) -> None: ...
        def close(self) -> None: ...

    monkeypatch.setattr(ftplib, "FTP", Recorder)
    with ftp_client.ftp_session(ctx.ftp_site, ctx=ctx):
        pass

    assert login["user"] == f"value-of-{USER_VAR}"
    assert login["passwd"] == f"value-of-{NESTED_VAR}"
    assert ctx.ftp_site.user == f"env.{USER_VAR}"
    assert ctx.ftp_site.password == f"env.{NESTED_VAR}"


def test_no_consumer_resolves_a_whole_configuration_block() -> None:
    """Each call names one field, so nothing resolves a subtree by accident."""
    import re

    package = Path(__file__).resolve().parent.parent / "rey_lib"
    whole_block = re.compile(
        r"resolve_env_reference\(\s*ctx\s*,\s*(db_cfg|ftp_cfg|profile|llm_cfg|conn_cfg)\s*\)"
    )
    offenders = [
        path.relative_to(package.parent).as_posix()
        for path in package.rglob("*.py")
        if whole_block.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_only_one_module_turns_a_reference_into_a_value() -> None:
    """One resolver, so a reference cannot come to mean two things.

    A second implementation would not have to disagree to do damage: it would
    only have to be reached first. What is forbidden is a module that both
    knows the reference syntax and reads the environment -- validating the
    syntax without reading anything, as configuration construction does, is
    not resolution.
    """
    package = Path(__file__).resolve().parent.parent / "rey_lib"
    allowed = {
        "rey_lib/config/env_reference.py",   # the resolver itself
        "rey_lib/config/config_loader.py",   # loads the .env into the environment
        "rey_lib/config/cli.py",             # the same, before argument parsing
    }

    offenders = []
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package.parent).as_posix()
        # Never walk into hidden directories: stale worktree copies live there
        # and are not the package.
        if any(part.startswith(".") for part in path.parts):
            continue
        if relative in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        knows_syntax = "ENV_REFERENCE_PREFIX" in text or 'startswith("env.")' in text
        reads_environment = "os.environ" in text or "os.getenv" in text
        if knows_syntax and reads_environment:
            offenders.append(relative)
    assert offenders == []


def test_the_reference_syntax_is_defined_once() -> None:
    """Both spellings, both phases, one constant."""
    package = Path(__file__).resolve().parent.parent / "rey_lib"
    definitions = [
        path.relative_to(package.parent).as_posix()
        for path in sorted(package.rglob("*.py"))
        if not any(part.startswith(".") for part in path.parts)
        and "ENV_REFERENCE_PREFIX = " in path.read_text(encoding="utf-8")
    ]
    assert definitions == ["rey_lib/config/env_reference.py"]

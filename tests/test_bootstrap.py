"""Tests for rey_lib.config.bootstrap.

The bootstrap owns both context acquisition and logging initialization. The only
variation is whether it receives an existing context or builds one itself;
logging is never owned by the application entry point.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from rey_lib.config.bootstrap import build_ctx_for_app
from rey_lib.errors.error_utils import ConfigError


def test_build_ctx_for_app_loads_shared_installation_configs(tmp_path: Path) -> None:
    project_root = tmp_path / "apps" / "sample_app"
    config_root = tmp_path / "development" / "installations" / "ccc"
    app_dir = config_root / "apps"
    shared_dir = config_root / "shared"
    app_dir.mkdir(parents=True)
    shared_dir.mkdir(parents=True)

    (config_root / "config.yaml").write_text(
        "installation:\n"
        "  name: ccc\n"
        "paths:\n"
        "  - name: root\n"
        f"    path: {tmp_path}\n"
        "  - name: configs\n"
        "    path: '{root}/development/installations/ccc'\n"
        # The bootstrap starts logging from the resolved context, so the
        # context has to name a destination or the run log lands under the
        # home directory of whoever ran the suite.
        f"log_path: '{tmp_path}/logs/sample_app.{{operation}}.{{timestamp}}.log'\n"
        "config_loading:\n"
        "  apps:\n"
        "    sample_app:\n"
        "      include:\n"
        "        - '{configs}/apps/sample_app.yaml'\n"
        "        - '{configs}/shared'\n",
        encoding="utf-8",
    )
    (app_dir / "sample_app.yaml").write_text("name: sample_app\n", encoding="utf-8")
    (shared_dir / "app_registry.yaml").write_text(
        "apps:\n"
        "  - name: sample_app\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    ctx = build_ctx_for_app(config_root / "config.yaml", "sample_app", project_root)

    assert ctx.installation.name == "ccc"
    assert ctx.app_name == "sample_app"
    assert ctx.name == "sample_app"
    assert ctx.paths.resolve("configs") == config_root.resolve()
    assert ctx.apps[0].name == "sample_app"


def _jsonl_handlers() -> list[logging.Handler]:
    """Every JSONL run-log handler currently attached to the root logger."""
    return [
        handler for handler in logging.getLogger().handlers
        if type(handler).__name__ == "JsonlHandler"
    ]


def test_the_resolve_branch_starts_logging_from_the_context_it_built(tmp_path: Path) -> None:
    """No context supplied: the bootstrap resolves one, then logs from it."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "installation:\n"
        "  name: sample\n"
        "paths:\n"
        "  - name: root\n"
        f"    path: {tmp_path}\n"
        f"log_path: '{tmp_path}/logs/sample.{{operation}}.{{timestamp}}.log'\n",
        encoding="utf-8",
    )

    ctx = build_ctx_for_app(config, "sample_app", operation="resolve")

    assert ctx.installation.name == "sample"
    assert _jsonl_handlers(), "the bootstrap returned without starting logging"
    assert str(ctx.log_file).startswith(str(tmp_path))


def test_the_supplied_branch_uses_the_context_it_was_given(tmp_path: Path) -> None:
    """A caller that already holds a context hands it over and is not re-resolved."""
    supplied = SimpleNamespace(
        log_path=f"{tmp_path}/logs/supplied.{{operation}}.{{timestamp}}.log",
        log_level="INFO",
        marker="the caller's own context",
    )

    ctx = build_ctx_for_app(ctx=supplied, operation="supplied")

    assert ctx is supplied, "the supplied context was replaced rather than used"
    assert ctx.marker == "the caller's own context"
    assert _jsonl_handlers(), "the supplied branch returned without starting logging"


def test_logging_is_started_on_both_branches() -> None:
    """The rule the whole contract rests on, asserted directly."""
    import inspect

    from rey_lib.config import bootstrap

    source = inspect.getsource(bootstrap.build_ctx_for_app)
    # One unconditional call, reached however the context arrived.
    assert source.count("setup_logging(") == 1, (
        "logging must be started exactly once, on a path both branches reach"
    )
    resolve_line = source.index("_resolve_ctx(")
    logging_line = source.index("setup_logging(")
    assert logging_line > resolve_line, "logging must start after the context exists"


def test_neither_a_context_nor_a_path_is_a_configuration_error() -> None:
    """The bootstrap cannot invent a context, and says so rather than guessing."""
    with pytest.raises(ConfigError, match="either a resolved ctx or a config path"):
        build_ctx_for_app()

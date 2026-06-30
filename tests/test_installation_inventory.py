"""Tests for config-utils-backed installation inventory."""

from __future__ import annotations

import pytest

from rey_lib.config.config_utils import Namespace, PathResolver
from rey_lib.config.inventory import build_installation_inventory
from rey_lib.errors.error_utils import ConfigError


def _ctx() -> Namespace:
    ctx = Namespace(
        {
            "config_path": "/tmp/install/installation.yaml",
            "apps": [
                {
                    "name": "rey_loader",
                    "enabled": True,
                    "entry_point": "main.py",
                    "cli": {
                        "parameters": [
                            {"name": "workflow", "value_type": "string"},
                            {"name": "dry-run", "value_type": "flag"},
                        ]
                    },
                }
            ],
            "workflows": {
                "load_only": {
                    "app": "rey_loader",
                    "kind": "internal",
                    "steps": ["load"],
                }
            },
            "pipelines": {
                "daily": {
                    "steps": [
                        {"name": "load", "app": "rey_loader"},
                    ]
                }
            },
            "tools": [],
        }
    )
    ctx.paths = PathResolver({"root": "/tmp/install"})
    return ctx


def test_build_installation_inventory_from_ctx() -> None:
    """Inventory is derived from resolved ctx and exposes run actions."""
    inventory = build_installation_inventory(_ctx())

    assert inventory.apps[0]["name"] == "rey_loader"
    assert inventory.workflows[0]["name"] == "load_only"
    assert inventory.workflows_by_app["rey_loader"] == ("load_only",)
    assert inventory.workflow_run_actions[0]["command_preview"] == (
        "rey_loader run-workflow --workflow load_only --config-path "
        "/tmp/install/installation.yaml"
    )


def test_installation_inventory_is_read_only() -> None:
    """Inventory mappings cannot be mutated after build."""
    inventory = build_installation_inventory(_ctx())

    with pytest.raises(TypeError):
        inventory.apps[0]["name"] = "changed"


def test_installation_inventory_rejects_unknown_workflow_owner() -> None:
    """Validation fails when a workflow references an unknown owner app."""
    ctx = _ctx()
    ctx.workflows = {
        "missing_owner": {
            "app": "missing_app",
            "steps": [],
        }
    }

    with pytest.raises(ConfigError, match="known app name"):
        build_installation_inventory(ctx)

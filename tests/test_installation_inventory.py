"""Tests for config-utils-backed installation inventory."""

from __future__ import annotations

import pytest

from rey_lib.config.config_utils import Namespace, PathResolver, build_ctx_from_path
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
            "llm_profiles": [
                {"name": "local_precision"},
            ],
            "connections": [
                {"name": "warehouse"},
            ],
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
    assert inventory.workflow_run_actions[0]["source_config_file"] == "/tmp/install/installation.yaml"


def test_inventory_reads_list_schema_pipelines() -> None:
    """Pipelines authored as a list (``pipelines: - name:``) are discovered.

    Regression: the list migration left inventory._pipeline_entries expecting a keyed
    map, which returned no pipelines and emptied the console pipeline list so the
    runner had nothing to start.
    """
    ctx = _ctx()
    ctx.pipelines = [
        {"name": "trade_analyzer_generate_apply_ddl",
         "steps": [{"name": "prepare", "app": "rey_loader"}]},
        {"name": "file_onboarder", "steps": []},
    ]

    inventory = build_installation_inventory(ctx)

    assert [p["name"] for p in inventory.pipelines] == [
        "trade_analyzer_generate_apply_ddl", "file_onboarder",
    ]


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


def test_installation_inventory_rejects_unknown_execution_profile() -> None:
    """Validation fails when a workflow references an unknown execution profile."""
    ctx = _ctx()
    ctx.workflows = {
        "load_only": {
            "app": "rey_loader",
            "execution_profile": "missing_profile",
            "steps": [],
        }
    }

    with pytest.raises(ConfigError, match="known execution profile name"):
        build_installation_inventory(ctx)


def test_installation_inventory_rejects_unknown_connection() -> None:
    """Validation fails when a workflow references an unknown connection."""
    ctx = _ctx()
    ctx.workflows = {
        "load_only": {
            "app": "rey_loader",
            "target_connection": "missing_connection",
            "steps": [],
        }
    }

    with pytest.raises(ConfigError, match="known connection name"):
        build_installation_inventory(ctx)


def test_installation_inventory_rejects_missing_contract_file(tmp_path) -> None:
    """Validation reports missing explicit contract_file references."""
    ctx = _ctx()
    missing_contract = tmp_path / "contracts" / "missing.md"
    ctx.workflows = {
        "load_only": {
            "app": "rey_loader",
            "steps": [{"name": "contracted", "contract_file": str(missing_contract)}],
        }
    }

    with pytest.raises(ConfigError, match="existing contract file"):
        build_installation_inventory(ctx)


def test_installation_inventory_keeps_contract_alias_string() -> None:
    """Only contract_file paths are validated; contract aliases remain strings."""
    ctx = _ctx()
    ctx.workflows = {
        "load_only": {
            "app": "rey_loader",
            "steps": [{"name": "contracted", "contract": "plain_contract_name"}],
        }
    }

    inventory = build_installation_inventory(ctx)

    assert inventory.workflows[0]["steps"][0]["contract"] == "plain_contract_name"


def test_installation_inventory_uses_root_app_for_workflow_list() -> None:
    """Canonical workflow list entries inherit owner from root app."""
    ctx = _ctx()
    ctx.app = "rey_loader"
    ctx.workflows = [
        {
            "name": "load_only",
            "steps": ["load"],
        }
    ]

    inventory = build_installation_inventory(ctx)

    assert inventory.workflows[0]["app"] == "rey_loader"
    assert inventory.workflows[0]["name"] == "load_only"


def test_config_utils_appends_workflow_list_entries(tmp_path) -> None:
    """Multiple workflow files merge by appending named workflow entries."""
    workflows_dir = tmp_path / "workflows" / "rey_loader"
    workflows_dir.mkdir(parents=True)
    config_path = tmp_path / "installation.yaml"
    config_path.write_text(
        "\n".join(
            [
                "paths:",
                "  - name: configs",
                f"    path: {tmp_path}",
                "config_loading:",
                "  default_behavior: none",
                "  apps:",
                "    rey_loader:",
                "      include:",
                "        - '{configs}/workflows/rey_loader'",
                "apps:",
                "  - name: rey_loader",
                "    enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    (workflows_dir / "a.yaml").write_text(
        "\n".join(
            [
                "app: rey_loader",
                "workflows:",
                "  - name: workflow_a",
                "    steps: []",
            ]
        ),
        encoding="utf-8",
    )
    (workflows_dir / "b.yaml").write_text(
        "\n".join(
            [
                "app: rey_loader",
                "workflows:",
                "  - name: workflow_b",
                "    steps: []",
            ]
        ),
        encoding="utf-8",
    )

    ctx = build_ctx_from_path(config_path, app_name="rey_loader")
    inventory = build_installation_inventory(ctx)

    assert [workflow["name"] for workflow in inventory.workflows] == [
        "workflow_a",
        "workflow_b",
    ]


def test_workflow_capabilities_default_false() -> None:
    """step/range default false when the workflow declares no execution block."""
    action = build_installation_inventory(_ctx()).workflow_run_actions[0]
    assert action["execution"]["step"] is False
    assert action["execution"]["range"] is False


def test_workflow_capabilities_surface_from_execution_block() -> None:
    """A workflow's execution:{step,range} contract (ADR-007) surfaces on the action."""
    ctx = _ctx()
    ctx.workflows = {
        "load_only": {
            "app": "rey_loader",
            "kind": "internal",
            "steps": ["load"],
            "execution": {"full": True, "dry_run": True, "step": True, "range": True},
        }
    }
    action = build_installation_inventory(ctx).workflow_run_actions[0]
    assert action["execution"]["step"] is True
    assert action["execution"]["range"] is True

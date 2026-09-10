"""The workflow run action, resolved from a loaded context.

These used to be tests of an installation-wide inventory aggregate. The
aggregate had no runtime caller and was removed; what it was actually proving
-- how a workflow's execution contract resolves, and how workflow entries merge
as configuration loads -- is proved here against the surfaces that survived.
"""

from __future__ import annotations

import pytest

from tests.support.installed_applications import installed
from rey_lib.config.config_utils import Namespace, PathResolver, build_ctx_from_path
from rey_lib.config.inventory import resolve_workflow_run_action
from rey_lib.errors.error_utils import ConfigError


def _ctx() -> Namespace:
    ctx = Namespace(
        {
            "config_path": "/tmp/install/installation.yaml",
            "apps": [
                {
                    "name": "rey_loader",
                    "enabled": True,
                    # Installation-owned and required; never taken from the
                    # package.
                    "app_path": "/apps/rey_loader",
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
            "pipelines": [
                {
                    "name": "daily",
                    "steps": [
                        {"name": "load", "app": "rey_loader"},
                    ]
                }
            ],
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


def test_a_workflow_defaults_to_full_and_dry_run_but_not_partial() -> None:
    """step/range default false when the workflow declares no execution block."""
    action = resolve_workflow_run_action(_ctx(), "rey_loader", "load_only")

    assert action["execution"]["full"] is True
    assert action["execution"]["dry_run"] is True
    assert action["execution"]["step"] is False
    assert action["execution"]["range"] is False


def test_capabilities_surface_from_the_execution_block() -> None:
    """A workflow's execution:{step,range} contract (ADR-007) reaches the action."""
    ctx = _ctx()
    ctx.workflows = {
        "load_only": {
            "app": "rey_loader",
            "kind": "internal",
            "steps": ["load"],
            "execution": {"full": True, "dry_run": True, "step": True, "range": True},
        }
    }

    action = resolve_workflow_run_action(ctx, "rey_loader", "load_only")

    assert action["execution"]["step"] is True
    assert action["execution"]["range"] is True


def test_resolving_one_action_attaches_nothing_to_the_context() -> None:
    """The resolver answers with one action and leaves the context as it was."""
    ctx = _ctx()

    action = resolve_workflow_run_action(ctx, "rey_loader", "load_only")

    assert action["app"] == "rey_loader"
    assert action["workflow"] == "load_only"
    assert action["command_preview"].startswith("rey_loader run-workflow")
    assert not hasattr(ctx, "inventory")


def test_a_workflow_list_entry_inherits_the_root_app_as_owner() -> None:
    """Canonical workflow list entries inherit owner from root app."""
    ctx = _ctx()
    ctx.app = "rey_loader"
    ctx.workflows = [{"name": "load_only", "steps": ["load"]}]

    action = resolve_workflow_run_action(ctx, "rey_loader", "load_only")

    assert action["app"] == "rey_loader"
    assert action["workflow"] == "load_only"


def test_config_utils_appends_workflow_list_entries(tmp_path, monkeypatch) -> None:
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
                "    app_path: /apps/rey_loader",
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

    installed(monkeypatch, "rey_loader")
    ctx = build_ctx_from_path(config_path, app_name="rey_loader")

    assert [workflow["name"] for workflow in ctx.workflows] == [
        "workflow_a",
        "workflow_b",
    ]


def test_an_unknown_workflow_fails_closed() -> None:
    """Unknown workflow identity fails closed without compatibility lookup."""
    with pytest.raises(ConfigError, match="Workflow run action not found"):
        resolve_workflow_run_action(_ctx(), "rey_loader", "missing")


def test_an_unnamed_workflow_is_refused() -> None:
    """An empty identity is a caller error, not an empty search."""
    with pytest.raises(ConfigError, match="required"):
        resolve_workflow_run_action(_ctx(), "rey_loader", "")

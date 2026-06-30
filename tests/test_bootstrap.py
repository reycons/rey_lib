"""Tests for rey_lib.config.bootstrap."""

from __future__ import annotations

from pathlib import Path

from rey_lib.config.bootstrap import build_ctx_for_app


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

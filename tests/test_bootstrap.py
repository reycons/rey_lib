"""Tests for rey_lib.config.bootstrap."""

from __future__ import annotations

from pathlib import Path

from rey_lib.config.bootstrap import build_ctx_for_app


def test_build_ctx_for_app_loads_shared_installation_configs(tmp_path: Path) -> None:
    project_root = tmp_path / "apps" / "sample_app"
    config_root = tmp_path / "development" / "installations" / "ccc" / "configs" / "v01"
    app_dir = config_root / "sample_app"
    shared_dir = config_root / "shared"
    app_dir.mkdir(parents=True)
    shared_dir.mkdir(parents=True)

    (config_root / "config.dev.yaml").write_text(
        "installation: ccc\n"
        "apps:\n"
        "  sample_app:\n"
        "    config_path: sample_app/config.dev.yaml\n"
        "shared_configs:\n"
        "  app_registry: shared/app_registry.yaml\n",
        encoding="utf-8",
    )
    (app_dir / "config.dev.yaml").write_text("app_name: sample_app\n", encoding="utf-8")
    (shared_dir / "app_registry.yaml").write_text(
        "apps:\n"
        "  - name: sample_app\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    ctx = build_ctx_for_app(config_root / "config.dev.yaml", "sample_app", project_root)

    assert ctx.installation == "ccc"
    assert ctx.installation_root == tmp_path / "development" / "installations" / "ccc"
    assert ctx.environment_root == tmp_path / "development"
    assert ctx.shared_configs.app_registry.apps[0].name == "sample_app"
    assert ctx.shared_config_paths.app_registry.path == shared_dir / "app_registry.yaml"

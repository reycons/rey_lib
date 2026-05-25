"""Tests for source-annotated config loading helpers."""

from __future__ import annotations

from pathlib import Path

from rey_lib.config.config_utils import (
    build_config_sources_yaml,
    build_config_sources_yaml_from_path,
    build_ctx,
    config_path,
)


def test_config_sources_use_build_ctx_file_order(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    nested_dir = config_dir / "nested"
    nested_dir.mkdir(parents=True)

    (config_dir / "config.test.yaml").write_text(
        "app_name: sample\nsettings:\n  one: 1\n",
        encoding="utf-8",
    )
    (config_dir / "config.dev.yaml").write_text("app_name: dev_sample\n", encoding="utf-8")
    (config_dir / "app.yaml").write_text("settings:\n  two: 2\n", encoding="utf-8")
    (nested_dir / "extra.yaml").write_text("settings:\n  three: 3\n", encoding="utf-8")

    ctx = build_ctx(env="test", project_root=tmp_path, config_dir=config_dir)
    assembled = build_config_sources_yaml(env="test", project_root=tmp_path, config_dir=config_dir)

    assert ctx.app_name == "sample"
    assert ctx.settings.one == 1
    assert ctx.settings.two == 2
    assert ctx.settings.three == 3
    assert "# SOURCE: config.test.yaml" in assembled
    assert "# SOURCE: app.yaml" in assembled
    assert "# SOURCE: nested/extra.yaml" in assembled
    assert "dev_sample" not in assembled
    assert assembled.index("# SOURCE: config.test.yaml") < assembled.index("# SOURCE: app.yaml")


def test_config_sources_from_path_uses_config_parent(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.prod.yaml"
    config_path.write_text("app_name: sample\n", encoding="utf-8")

    assembled = build_config_sources_yaml_from_path(config_path, project_root=tmp_path)

    assert "# SOURCE: config.prod.yaml" in assembled
    assert "app_name: sample" in assembled


def test_config_path_returns_environment_config_path(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.test.yaml").write_text("app_name: sample\n", encoding="utf-8")

    path = config_path("test", config_dir)

    assert path == config_dir.resolve() / "config.test.yaml"

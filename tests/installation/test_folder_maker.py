"""Tests for rey_lib.installation.folder_maker."""

from __future__ import annotations

from pathlib import Path

import yaml

from rey_lib.installation.folder_maker import (
    FolderMakerResult,
    scaffold_config_root,
    scaffold_environment,
    scaffold_installation,
)


# ---------------------------------------------------------------------------
# FolderMakerResult
# ---------------------------------------------------------------------------

class TestFolderMakerResult:
    def test_success_when_no_errors(self):
        r = FolderMakerResult(created=["a"], existed=["b"])
        assert r.success is True

    def test_failure_when_errors(self):
        r = FolderMakerResult(errors=["something went wrong"])
        assert r.success is False

    def test_merge(self):
        a = FolderMakerResult(created=["x"], existed=["y"])
        b = FolderMakerResult(created=["p"], errors=["e"])
        a.merge(b)
        assert a.created == ["x", "p"]
        assert a.existed == ["y"]
        assert a.errors == ["e"]


# ---------------------------------------------------------------------------
# scaffold_config_root
# ---------------------------------------------------------------------------

class TestScaffoldConfigRoot:
    def test_creates_expected_app_folders(self, tmp_path):
        cr = tmp_path / "v01"
        result = scaffold_config_root(cr)

        assert result.success
        for app in ["ftp_sync", "rey_loader", "rey_analyzer", "pipeline_coordinator", "rey_console", "file_redactor"]:
            assert (cr / app).is_dir(), f"missing: {app}"
            assert (cr / app / "app.yaml").exists(), f"missing app.yaml: {app}"

    def test_creates_lifecycle_folders(self, tmp_path):
        cr = tmp_path / "v01"
        scaffold_config_root(cr)
        for lf in ["_draft", "_approved", "_archive"]:
            assert (cr / lf).is_dir()

    def test_creates_app_subfolders(self, tmp_path):
        cr = tmp_path / "v01"
        scaffold_config_root(cr)

        assert (cr / "ftp_sync" / "data_feeds").is_dir()
        assert (cr / "rey_loader" / "data_sources").is_dir()
        assert (cr / "rey_loader" / "sql_configs").is_dir()
        assert (cr / "rey_loader" / "diagnostics").is_dir()
        assert (cr / "rey_analyzer" / "analysis_configs").is_dir()
        assert (cr / "rey_analyzer" / "llm_configs").is_dir()
        assert (cr / "rey_analyzer" / "contracts").is_dir()
        assert (cr / "pipeline_coordinator" / "pipelines").is_dir()
        assert (cr / "file_redactor" / "redact").is_dir()

    def test_creates_diagnostics_default_yaml(self, tmp_path):
        cr = tmp_path / "v01"
        scaffold_config_root(cr)
        diag = cr / "rey_loader" / "diagnostics" / "default.yaml"
        assert diag.exists()
        data = yaml.safe_load(diag.read_text())
        assert "diagnostics" in data

    def test_creates_app_registry_for_pipeline_coordinator(self, tmp_path):
        cr = tmp_path / "v01"
        scaffold_config_root(cr)
        assert (cr / "pipeline_coordinator" / "app_registry.yaml").exists()

    def test_creates_gitkeep_in_empty_subfolders(self, tmp_path):
        cr = tmp_path / "v01"
        scaffold_config_root(cr)
        assert (cr / "ftp_sync" / "data_feeds" / ".gitkeep").exists()

    def test_does_not_overwrite_existing_app_yaml(self, tmp_path):
        cr = tmp_path / "v01"
        scaffold_config_root(cr)
        (cr / "ftp_sync" / "app.yaml").write_text("custom: true\n")

        scaffold_config_root(cr)
        assert (cr / "ftp_sync" / "app.yaml").read_text() == "custom: true\n"

    def test_force_overwrites_existing_app_yaml(self, tmp_path):
        cr = tmp_path / "v01"
        scaffold_config_root(cr)
        (cr / "ftp_sync" / "app.yaml").write_text("custom: true\n")

        scaffold_config_root(cr, force=True)
        data = yaml.safe_load((cr / "ftp_sync" / "app.yaml").read_text())
        assert data["name"] == "ftp_sync"

    def test_reports_created_and_existed(self, tmp_path):
        cr = tmp_path / "v01"
        r1 = scaffold_config_root(cr)
        assert len(r1.created) > 0
        assert len(r1.existed) == 0

        r2 = scaffold_config_root(cr)
        assert len(r2.existed) > 0

    def test_idempotent(self, tmp_path):
        cr = tmp_path / "v01"
        scaffold_config_root(cr)
        r2 = scaffold_config_root(cr)
        assert r2.success
        assert len(r2.created) == 0


# ---------------------------------------------------------------------------
# scaffold_environment
# ---------------------------------------------------------------------------

class TestScaffoldEnvironment:
    def test_creates_shared_folders(self, tmp_path):
        result = scaffold_environment(tmp_path, "development")

        assert result.success
        env = tmp_path / "development"
        assert (env / "apps").is_dir()
        assert (env / "installations").is_dir()
        assert (env / "logs" / "installer").is_dir()
        assert (env / "python").is_dir()
        assert (env / "etc").is_dir()

    def test_idempotent(self, tmp_path):
        scaffold_environment(tmp_path, "development")
        r2 = scaffold_environment(tmp_path, "development")
        assert r2.success
        assert len(r2.created) == 0

    def test_environments_are_independent(self, tmp_path):
        scaffold_environment(tmp_path, "development")
        scaffold_environment(tmp_path, "test")
        assert (tmp_path / "development" / "apps").is_dir()
        assert (tmp_path / "test" / "apps").is_dir()


# ---------------------------------------------------------------------------
# scaffold_installation
# ---------------------------------------------------------------------------

class TestScaffoldInstallation:

    # -- Environment-level folders created by scaffold_installation -----------

    def test_creates_environment_folders(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        env = tmp_path / "development"
        assert (env / "apps").is_dir()
        assert (env / "logs" / "installer").is_dir()
        assert (env / "python").is_dir()
        assert (env / "etc").is_dir()

    # -- Installation-level folders -------------------------------------------

    def test_creates_installation_root(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        assert (tmp_path / "development" / "installations" / "ccc").is_dir()

    def test_creates_versioned_config_root(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        cfg = tmp_path / "development" / "installations" / "ccc" / "configs" / "v01"
        assert cfg.is_dir()
        assert (cfg / "app.yaml").exists()

    def test_creates_versioned_llm_contracts(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        llm = tmp_path / "development" / "installations" / "ccc" / "llm_contracts" / "v01"
        assert llm.is_dir()

    def test_creates_data_subfolders(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        data = tmp_path / "development" / "installations" / "ccc" / "data"
        for sub in ["inbox", "processing", "converted", "loaded", "rejected", "archive", "temp"]:
            assert (data / sub).is_dir(), f"missing: {sub}"

    def test_creates_logs_folder(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        assert (tmp_path / "development" / "installations" / "ccc" / "logs").is_dir()

    def test_creates_runtime_folder(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        assert (tmp_path / "development" / "installations" / "ccc" / "runtime").is_dir()

    def test_creates_env_template(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        env_file = tmp_path / "development" / "installations" / "ccc" / ".env"
        assert env_file.exists()
        assert "ccc" in env_file.read_text()

    def test_does_not_overwrite_existing_env(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        env_file = tmp_path / "development" / "installations" / "ccc" / ".env"
        env_file.write_text("DB_PASSWORD=secret\n")

        scaffold_installation(tmp_path, "development", "ccc")
        assert env_file.read_text() == "DB_PASSWORD=secret\n"

    def test_force_overwrites_env(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        env_file = tmp_path / "development" / "installations" / "ccc" / ".env"
        env_file.write_text("DB_PASSWORD=secret\n")

        scaffold_installation(tmp_path, "development", "ccc", force=True)
        assert "DB_PASSWORD=secret" not in env_file.read_text()

    # -- Versioning -----------------------------------------------------------

    def test_custom_config_version(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc", config_version="v02")
        assert (tmp_path / "development" / "installations" / "ccc" / "configs" / "v02").is_dir()
        assert not (tmp_path / "development" / "installations" / "ccc" / "configs" / "v01").exists()

    def test_custom_contract_version(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc", contract_version="v02")
        assert (tmp_path / "development" / "installations" / "ccc" / "llm_contracts" / "v02").is_dir()

    # -- Isolation ------------------------------------------------------------

    def test_installations_isolated_within_environment(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        scaffold_installation(tmp_path, "development", "lupo")
        assert (tmp_path / "development" / "installations" / "ccc").is_dir()
        assert (tmp_path / "development" / "installations" / "lupo").is_dir()

    def test_environments_isolated_from_each_other(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        scaffold_installation(tmp_path, "test", "ccc")
        assert (tmp_path / "development" / "installations" / "ccc").is_dir()
        assert (tmp_path / "test" / "installations" / "ccc").is_dir()
        # separate app trees
        assert (tmp_path / "development" / "apps").is_dir()
        assert (tmp_path / "test" / "apps").is_dir()

    def test_no_old_layout_created(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        assert not (tmp_path / "config").exists()
        assert not (tmp_path / "configs").exists()
        assert not (tmp_path / "data").exists()
        assert not (tmp_path / "artifacts").exists()
        assert not (tmp_path / "runs").exists()

    # -- Idempotency ----------------------------------------------------------

    def test_idempotent(self, tmp_path):
        scaffold_installation(tmp_path, "development", "ccc")
        r2 = scaffold_installation(tmp_path, "development", "ccc")
        assert r2.success
        assert len(r2.created) == 0

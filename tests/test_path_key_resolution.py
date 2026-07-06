"""
Tests for explicit path-key resolution in config_utils.

Proves that filesystem-path behaviour is driven solely by the explicit
``_PATH_KEYS`` allowlist — never inferred from key spelling (no suffix/name/
regex matching, no fallback). See SGC_Remove_Implicit_Path_Key_Detection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rey_lib.config.config_utils import (
    Namespace,
    PathResolver,
    _apply_path_resolver,
    _is_path_key,
    _resolve_paths,
)
from rey_lib.errors.error_utils import ConfigError

_BASE = Path("/base")


def _resolve(data: dict) -> dict:
    """Run the load-time path resolver against a flat config dict."""
    return _resolve_paths(data, _BASE, parent_key="")


# ---- undeclared path-like keys are NOT resolved (no suffix inference) ------

def test_undeclared_output_path_not_resolved() -> None:
    """'output_path' is not in the allowlist -> left as a plain string."""
    out = _resolve({"output_path": "some/rel"})
    assert out["output_path"] == "some/rel"
    assert not isinstance(out["output_path"], Path)


def test_undeclared_log_dir_not_resolved() -> None:
    """'log_dir' is not in the allowlist -> left as a plain string."""
    out = _resolve({"log_dir": "logs/x"})
    assert out["log_dir"] == "logs/x"


def test_bare_path_not_resolved() -> None:
    """Bare 'path' is dropped from generic resolution -> unchanged string."""
    out = _resolve({"path": "rel/thing"})
    assert out["path"] == "rel/thing"


def test_no_suffix_fallback_for_arbitrary_path_suffix() -> None:
    """A '_path'-suffixed key not in the allowlist is NOT resolved (no fallback)."""
    out = _resolve({"foo_path": "rel/y", "weird_root": "z", "thing_file": "f"})
    assert out["foo_path"] == "rel/y"
    assert out["weird_root"] == "z"
    assert out["thing_file"] == "f"


def test_contract_file_is_resolved() -> None:
    """'contract_file' is an explicit path key and resolves like other paths."""
    out = _resolve({"contract_file": "ddl_comment_enrichment.md"})
    assert isinstance(out["contract_file"], Path)
    assert out["contract_file"] == (_BASE / "ddl_comment_enrichment.md").resolve()


def test_contract_file_path_root_token_resolves() -> None:
    """Path-root tokens in contract_file resolve to a concrete Path."""
    ctx = Namespace({
        "assist_sql": {"contract_file": "{llmcontracts}/db_admin/ddl_comment_enrichment.md"}
    })
    resolver = PathResolver({"llmcontracts": Path("/contracts")})

    _apply_path_resolver(ctx, resolver)

    assert ctx.assist_sql.contract_file == Path(
        "/contracts/db_admin/ddl_comment_enrichment.md"
    )


def test_workflow_local_token_resolves_path_key_at_load() -> None:
    """Workflow-local tokens expand into concrete path-bearing process values."""
    ctx = Namespace({
        "workflows": [
            {
                "name": "postgres_version_lint_comment",
                "tokens": {"ddl_root": "{data}/rey_db_admin/database_ddl"},
                "processes": {
                    "export_database_ddl": {"output_root": "{ddl_root}"},
                    "git_commit": {"repo_root": "{ddl_root}"},
                    "commit_message": {"message_template": "Export {engine} DDL"},
                },
            }
        ]
    })
    resolver = PathResolver({"data": Path("/rey/data")})

    _apply_path_resolver(ctx, resolver)

    workflow = ctx.workflows[0]
    expected = Path("/rey/data/rey_db_admin/database_ddl")
    assert workflow.tokens.ddl_root == str(expected)
    assert workflow.processes.export_database_ddl.output_root == expected
    assert workflow.processes.git_commit.repo_root == expected
    assert workflow.processes.commit_message.message_template == "Export {engine} DDL"


def test_workflow_local_token_unknown_reference_fails_at_load() -> None:
    """Unknown placeholders inside workflow-local token definitions fail closed."""
    ctx = Namespace({
        "workflows": [
            {
                "name": "bad",
                "tokens": {"ddl_root": "{missing}/ddl"},
                "processes": {"export": {"output_root": "{ddl_root}"}},
            }
        ]
    })

    with pytest.raises(ConfigError, match="missing"):
        _apply_path_resolver(ctx, PathResolver({"data": Path("/rey/data")}))


def test_workflow_local_token_global_name_collision_fails_at_load() -> None:
    """Workflow-local token names may not silently shadow installation paths."""
    ctx = Namespace({
        "workflows": [
            {
                "name": "bad",
                "tokens": {"data": "/other"},
                "processes": {"export": {"output_root": "{data}/ddl"}},
            }
        ]
    })

    with pytest.raises(ConfigError, match="conflicts"):
        _apply_path_resolver(ctx, PathResolver({"data": Path("/rey/data")}))


def test_contract_remains_string() -> None:
    """'contract' is not a path key; consumers resolve contract names explicitly."""
    out = _resolve({"contract": "contracts/analyze.md"})
    assert out["contract"] == "contracts/analyze.md"
    assert not isinstance(out["contract"], Path)


# ---- declared path keys ARE resolved --------------------------------------

def test_declared_path_key_is_resolved() -> None:
    """A declared key with a relative value resolves to an absolute Path."""
    out = _resolve({"inbox_path": "data/inbox"})
    assert isinstance(out["inbox_path"], Path)
    assert out["inbox_path"] == (_BASE / "data/inbox").resolve()


def test_unknown_key_unchanged() -> None:
    """A non-path key is passed through untouched."""
    out = _resolve({"description": "data/inbox", "version": "v01"})
    assert out["description"] == "data/inbox"
    assert out["version"] == "v01"


# ---- predicate is pure membership -----------------------------------------

def test_is_path_key_is_membership_only() -> None:
    """_is_path_key resolves only via the explicit allowlist."""
    assert _is_path_key("inbox_path")
    assert _is_path_key("env_file")
    assert not _is_path_key("output_path")
    assert not _is_path_key("log_dir")
    assert not _is_path_key("path")
    assert _is_path_key("contract_file")
    assert not _is_path_key("contract")
    assert not _is_path_key("foo_path")

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
from rey_lib.config.config_paths import _build_path_resolver
from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs.execution_records import log_run_restore_policy

_BASE = Path("/base")


def _resolve(data: dict) -> dict:
    """Run the load-time path resolver against a flat config dict."""
    return _resolve_paths(data, _BASE, parent_key="")


def test_configured_date_tokens_resolve_from_supplied_startup_values() -> None:
    """The existing resolver applies every exact date-token spelling."""
    runtime_tokens = {
        "date": "20260720",
        "yyyy": "2026",
        "mm": "07",
        "dd": "20",
        "yyymm": "202607",
        "yyymmdd": "20260720",
    }
    resolver = _build_path_resolver([
        {"name": "logs", "path": "/resolved/logs"},
        {
            "name": "dated",
            "path": (
                "{logs}/{date}/{yyyy}/{mm}/{dd}/{yyymm}/"
                "llm_evaluation_payloads.{yyymmdd}.jsonl"
            ),
        },
    ], runtime_tokens)

    assert resolver.resolve("dated") == Path(
        "/resolved/logs/20260720/2026/07/20/202607/"
        "llm_evaluation_payloads.20260720.jsonl"
    )

    ctx = Namespace({
        "payload_log_path": "{logs}/llm_evaluation/payloads.{yyymmdd}.jsonl",
    })
    _apply_path_resolver(ctx, resolver)
    assert ctx.payload_log_path == (
        "/resolved/logs/llm_evaluation/payloads.20260720.jsonl"
    )


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


def test_pipeline_local_tokens_resolve_across_owned_entry() -> None:
    """Pipeline tokens resolve restore mappings, args, overrides, and nested lists."""
    ctx = Namespace({
        "pipelines": [
            {
                "name": "trade",
                "tokens": {
                    "trade_root": "{data}/trade",
                    "trade_inbox": "{trade_root}/inbox",
                    "trade_processed": "{trade_root}/processed",
                },
                "restore_mappings": [
                    {"from": "{trade_processed}", "to": "{trade_inbox}"}
                ],
                "steps": [
                    {
                        "name": "prepare",
                        "args": ["--inbox", "{trade_inbox}"],
                        "ctx_overrides": {
                            "processed": "{trade_processed}",
                            "nested": [{"source": "{trade_inbox}"}],
                        },
                    }
                ],
            }
        ]
    })

    _apply_path_resolver(ctx, PathResolver({"data": Path("/rey/data")}))

    pipeline = ctx.pipelines[0]
    assert pipeline.tokens.trade_root == "/rey/data/trade"
    assert pipeline.tokens.trade_inbox == "/rey/data/trade/inbox"
    assert getattr(pipeline.restore_mappings[0], "from") == "/rey/data/trade/processed"
    assert pipeline.restore_mappings[0].to == "/rey/data/trade/inbox"
    assert pipeline.steps[0].args == ["--inbox", "/rey/data/trade/inbox"]
    assert pipeline.steps[0].ctx_overrides.processed == "/rey/data/trade/processed"
    assert pipeline.steps[0].ctx_overrides.nested[0].source == "/rey/data/trade/inbox"


def test_pipeline_scoped_tokens_are_isolated() -> None:
    """A pipeline cannot consume another pipeline's local token scope."""
    ctx = Namespace({
        "pipelines": [
            {"name": "first", "tokens": {"owned": "{data}/first"}, "value": "{owned}"},
            {"name": "second", "tokens": {"other": "{data}/second"}, "value": "{owned}"},
        ]
    })

    _apply_path_resolver(ctx, PathResolver({"data": Path("/rey/data")}))

    assert ctx.pipelines[0].value == "/rey/data/first"
    assert ctx.pipelines[1].value == "{owned}"


def test_circular_pipeline_scoped_tokens_fail_explicitly() -> None:
    """Circular local dependencies fail instead of surviving configuration load."""
    ctx = Namespace({
        "pipelines": [
            {"name": "bad", "tokens": {"one": "{two}", "two": "{one}"}}
        ]
    })

    with pytest.raises(ConfigError, match="Circular scoped token reference"):
        _apply_path_resolver(ctx, PathResolver({"data": Path("/rey/data")}))


def test_approved_late_bound_pipeline_token_may_survive() -> None:
    """The established source_subfolder runtime placeholder remains late-bound."""
    ctx = Namespace({
        "pipelines": [
            {
                "name": "parameterized",
                "tokens": {"processed": "{data}/processed/{source_subfolder}"},
                "value": "{processed}",
            }
        ]
    })

    _apply_path_resolver(ctx, PathResolver({"data": Path("/rey/data")}))

    assert ctx.pipelines[0].value == "/rey/data/processed/{source_subfolder}"


def test_run_restore_policy_receives_resolved_paths(monkeypatch) -> None:
    """The log helper receives resolved mappings and performs no token resolution."""
    ctx = Namespace({
        "pipelines": [
            {
                "name": "trade",
                "tokens": {
                    "inbox": "{data}/trade/inbox",
                    "processed": "{data}/trade/processed",
                },
                "restore_mappings": [{"from": "{processed}", "to": "{inbox}"}],
            }
        ]
    })
    _apply_path_resolver(ctx, PathResolver({"data": Path("/rey/data")}))
    captured: dict[str, object] = {}

    def capture(_ctx, record_type, **fields):
        captured.update({"record_type": record_type, **fields})

    monkeypatch.setattr("rey_lib.logs.execution_records.log_run_record", capture)
    resolved_mappings = [
        {key: getattr(mapping, key) for key in mapping.keys()}
        for mapping in ctx.pipelines[0].restore_mappings
    ]
    log_run_restore_policy(ctx, resolved_mappings)

    assert captured == {
        "record_type": "RUN_RESTORE_POLICY",
        "restore_rules": [
            {"from": "/rey/data/trade/processed", "to": "/rey/data/trade/inbox"}
        ],
    }


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


# ---- installation-scoped log root (SGC_Rey_Installation_Scoped_Log_Ownership) --

def _installation_paths(installation: str) -> list[dict[str, str]]:
    """Mirror the authoritative installation ``paths:`` shape after the migration.

    ``logs`` now derives from ``{configs}`` (the installation directory) rather
    than the shared ``{root}/logs``; every subordinate runtime log path derives
    from ``{logs}`` and therefore follows automatically.
    """
    return [
        {"name": "root", "path": "/rey"},
        {"name": "configs", "path": "{root}/installations/" + installation},
        {"name": "logs", "path": "{configs}/logs"},
        {"name": "pipeline_log_dir", "path": "{logs}/pipeline"},
        {"name": "rey_console_log",
         "path": "{logs}/rey_console/rey_console.{operation}.{timestamp}.log"},
        {"name": "llm_evaluation_payload_log",
         "path": "{logs}/llm_evaluation/llm_evaluation_payloads.{yyymmdd}.jsonl"},
        {"name": "llm_evaluation_run_log",
         "path": "{logs}/llm_evaluation/llm_evaluation_runs.{yyymmdd}.jsonl"},
    ]


def test_log_root_is_installation_scoped_and_distinct_per_installation() -> None:
    """local and wolff_popper resolve distinct roots, each under its own dir."""
    local = _build_path_resolver(_installation_paths("local"))
    wolff = _build_path_resolver(_installation_paths("wolff_popper"))

    local_logs = local.resolve("logs")
    wolff_logs = wolff.resolve("logs")

    assert local_logs != wolff_logs
    assert local_logs == Path("/rey/installations/local/logs")
    assert wolff_logs == Path("/rey/installations/wolff_popper/logs")
    # Each log root is beneath its own installation directory (== {configs}).
    assert local_logs.parent == local.resolve("configs")
    assert wolff_logs.parent == wolff.resolve("configs")


def test_subordinate_log_paths_follow_the_installation_root() -> None:
    """Every subordinate runtime log path derives from the installation logs root."""
    wolff = _build_path_resolver(_installation_paths("wolff_popper"))
    root = wolff.resolve("logs")

    for name in ("pipeline_log_dir", "rey_console_log",
                 "llm_evaluation_payload_log", "llm_evaluation_run_log"):
        resolved = wolff.resolve(name)
        assert str(resolved).startswith(str(root) + "/"), name
        # No subordinate path resolves under the former shared /rey/logs root.
        assert not str(resolved).startswith("/rey/logs/"), name


def test_dated_llm_evaluation_filenames_are_preserved_through_resolution() -> None:
    """The {yyymmdd} rotation token survives path resolution unchanged."""
    wolff = _build_path_resolver(_installation_paths("wolff_popper"))

    payload = wolff.resolve("llm_evaluation_payload_log")
    run = wolff.resolve("llm_evaluation_run_log")

    assert payload == Path(
        "/rey/installations/wolff_popper/logs/llm_evaluation/"
        "llm_evaluation_payloads.{yyymmdd}.jsonl"
    )
    assert run == Path(
        "/rey/installations/wolff_popper/logs/llm_evaluation/"
        "llm_evaluation_runs.{yyymmdd}.jsonl"
    )


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

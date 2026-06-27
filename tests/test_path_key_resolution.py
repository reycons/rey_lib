"""
Tests for explicit path-key resolution in config_utils.

Proves that filesystem-path behaviour is driven solely by the explicit
``_PATH_KEYS`` allowlist — never inferred from key spelling (no suffix/name/
regex matching, no fallback). See SGC_Remove_Implicit_Path_Key_Detection.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.config.config_utils import _is_path_key, _resolve_paths

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


def test_contract_file_not_resolved() -> None:
    """'contract_file' is a logical identifier, not a path -> unchanged string."""
    out = _resolve({"contract_file": "ddl_comment_enrichment.md"})
    assert out["contract_file"] == "ddl_comment_enrichment.md"


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
    assert not _is_path_key("contract_file")
    assert not _is_path_key("foo_path")

"""
Path token resolution and path-key handling for rey_lib config.

Owns the reviewed path-key allowlists, the PathResolver, and the logical
``{token}`` substitution applied across the merged config tree. Split out of
``config_utils`` (SGC_Rey_Lib_Config_Utils_Responsibility_Split); token
resolution, allowlists, and path normalisation are unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.config.config_namespace import Namespace
from rey_lib.errors.error_utils import ConfigError

# Keys whose string values are filesystem paths and are resolved to Path
# objects. Explicit, reviewed membership only — path behaviour is never inferred
# from how a key is spelled (no suffix/name/regex matching, no fallback). Add a
# key here, under review, to opt it in. Notably absent: bare 'path' (the
# 'paths:' block is resolved explicitly by _build_path_resolver).
_PATH_KEYS = frozenset({
    "app_path", "artifacts_path", "config_path", "contracts_root",
    "contract_file", "converted_path", "env_file", "failed_path",
    "inbox_path", "jsonl_path", "jsonl_root", "output_root",
    "pipeline_log_dir", "processing_path", "raw_output_path",
    "readable_root", "records_path", "rejected_path", "repo_root",
    "results_path", "script_path", "sql_path", "success_path", "venv_path",
    "working_dir",
})

# Keys whose string values are log path templates — placeholders must survive resolution.
_LOG_PATH_KEYS = ("log_path",)

class PathResolver:
    """Resolved named paths from the installation config ``paths:`` list.

    Attached to ``ctx.paths`` after loading a ``config.yaml``.
    Call ``ctx.paths.resolve("name")`` to get the physical ``Path``.
    """

    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = dict(paths)

    def resolve(self, name: str) -> Path:
        """Return the resolved ``Path`` for the given logical name."""
        if name not in self._paths:
            raise ConfigError(f"Unknown path name: {name!r}")
        return self._paths[name]

    def __repr__(self) -> str:
        return f"PathResolver({list(self._paths)})"

def _build_path_resolver(raw_paths: Any) -> PathResolver:
    """Process a ``paths:`` list into a ``PathResolver``.

    Each entry must have ``name`` and ``path`` keys.  Values may reference
    earlier-resolved names with ``{name}`` placeholders.
    """
    resolved_strs: dict[str, str] = {}
    resolved_paths: dict[str, Path] = {}

    entries: list[Any] = raw_paths if isinstance(raw_paths, list) else []
    for entry in entries:
        if isinstance(entry, Namespace):
            name = str(getattr(entry, "name", "") or "")
            template = str(getattr(entry, "path", "") or "")
        elif isinstance(entry, dict):
            name = str(entry.get("name", "") or "")
            template = str(entry.get("path", "") or "")
        else:
            continue
        if not name or not template:
            continue
        path_str = template.format_map(_SafePathFormat(resolved_strs))
        path = Path(path_str).expanduser().resolve()
        resolved_strs[name] = str(path)
        resolved_paths[name] = path

    return PathResolver(resolved_paths)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _is_path_key(key: str) -> bool:
    """Return True only for keys explicitly declared as filesystem paths."""
    return key in _PATH_KEYS

def _is_log_path_key(key: str) -> bool:
    return key in _LOG_PATH_KEYS

def _resolve_path_value(value: str, base: Path) -> Path:
    if "{" in value:
        return Path(value)  # logical path template — substituted after PathResolver is built
    if value.startswith("~"):
        return Path(value).expanduser().resolve()
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()

def _resolve_log_path_value(value: str, base: Path) -> str:
    if "{" in value:
        return value  # logical path template — substituted after PathResolver is built
    if value.startswith("~"):
        return str(Path(value).expanduser())
    p = Path(value)
    return str(p) if p.is_absolute() else str((base / p).resolve())

def _resolve_paths(data: dict[str, Any], base: Path, parent_key: str) -> dict[str, Any]:
    """Recursively walk a config dict and resolve path-like string values."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = _resolve_paths(
                value, base, f"{parent_key}.{key}" if parent_key else key
            )
        elif isinstance(value, list):
            result[key] = [
                _resolve_paths(item, base, parent_key) if isinstance(item, dict) else item
                for item in value
            ]
        elif isinstance(value, str) and _is_log_path_key(key):
            result[key] = _resolve_log_path_value(value, base)
        elif isinstance(value, str) and _is_path_key(key) and not value.startswith("ctx."):
            result[key] = _resolve_path_value(value, base)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Private — Namespace construction
# ---------------------------------------------------------------------------

class _SafePathFormat(dict):  # type: ignore[type-arg]
    """dict subclass that returns the key in braces for missing keys."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"

def _apply_path_resolver(obj: Any, resolver: PathResolver) -> None:
    """Walk a Namespace tree and substitute {logicalname} references using *resolver*.

    Only names present in the PathResolver are substituted; unknown placeholders
    like {operation} and {timestamp} survive unchanged.
    """
    resolver_strs: dict[str, str] = {
        name: str(resolver.resolve(name)) for name in resolver._paths
    }
    _walk_logical_refs(obj, resolver_strs)

def _walk_logical_refs(obj: Any, resolver_strs: dict[str, str]) -> None:
    """Recursive worker for :func:`_apply_path_resolver`."""
    if isinstance(obj, Namespace):
        for key in obj.keys():
            value = object.__getattribute__(obj, key)
            raw = str(value) if isinstance(value, Path) else value
            if isinstance(raw, str) and "{" in raw:
                substituted = raw.format_map(_SafePathFormat(resolver_strs))
                if _is_path_key(key):
                    object.__setattr__(obj, key, Path(substituted).expanduser())
                else:
                    object.__setattr__(obj, key, substituted)
            elif isinstance(value, (Namespace, list)):
                _walk_logical_refs(value, resolver_strs)
    elif isinstance(obj, list):
        for item in obj:
            _walk_logical_refs(item, resolver_strs)

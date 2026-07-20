"""
Path token resolution and path-key handling for rey_lib config.

Owns the reviewed path-key allowlists, the PathResolver, and the logical
``{token}`` substitution applied across the merged config tree. Split out of
``config_utils`` (SGC_Rey_Lib_Config_Utils_Responsibility_Split); token
resolution, allowlists, and path normalisation are unchanged.
"""

from __future__ import annotations

import re
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

_TOKEN_PATTERN = re.compile(r"{([^{}]+)}")
_RUNTIME_PATH_PLACEHOLDERS = frozenset(
    {"operation", "timestamp", "run_id", "source_subfolder"}
)

class PathResolver:
    """Resolved named paths from the installation config ``paths:`` list.

    Attached to ``ctx.paths`` after loading a ``config.yaml``.
    Call ``ctx.paths.resolve("name")`` to get the physical ``Path``.
    """

    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = dict(paths)
        self._runtime_tokens: dict[str, str] = {}

    def resolve(self, name: str) -> Path:
        """Return the resolved ``Path`` for the given logical name."""
        if name not in self._paths:
            raise ConfigError(f"Unknown path name: {name!r}")
        return self._paths[name]

    def __repr__(self) -> str:
        return f"PathResolver({list(self._paths)})"

def _build_path_resolver(
    raw_paths: Any,
    tokens: dict[str, str] | None = None,
) -> PathResolver:
    """Process a ``paths:`` list into a ``PathResolver``.

    Each entry must have ``name`` and ``path`` keys.  Values may reference
    earlier-resolved names with ``{name}`` placeholders.
    """
    runtime_tokens = dict(tokens or {})
    resolved_strs: dict[str, str] = dict(runtime_tokens)
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

    resolver = PathResolver(resolved_paths)
    resolver._runtime_tokens = runtime_tokens
    return resolver


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
    resolver_strs: dict[str, str] = dict(resolver._runtime_tokens)
    resolver_strs.update({
        name: str(resolver.resolve(name)) for name in resolver._paths
    })
    _walk_logical_refs(obj, resolver_strs)
    _resolve_scoped_tokens(obj, "workflows", resolver_strs)
    _resolve_scoped_tokens(obj, "pipelines", resolver_strs)
    _walk_logical_refs(obj, resolver_strs)
    _validate_no_unresolved_path_tokens(obj)

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


def _resolve_scoped_tokens(
    obj: Any, section: str, global_tokens: dict[str, str]
) -> None:
    """Resolve entry-local tokens within each item in ``section``.

    Workflows and pipelines share this implementation. Each entry receives only
    the global tokens plus its own resolved local tokens, so scopes never leak
    between entries.
    """
    entries = _child(obj, section)
    for entry in _scoped_items(entries):
        tokens = _child(entry, "tokens")
        token_map = _resolve_local_tokens(tokens, global_tokens, section)
        if token_map:
            combined = dict(global_tokens)
            combined.update(token_map)
            _walk_scoped_refs(entry, combined)


def _scoped_items(entries: Any) -> list[Any]:
    if entries is None:
        return []
    if isinstance(entries, Namespace):
        return [item for _, item in entries.items()]
    if isinstance(entries, dict):
        return list(entries.values())
    if isinstance(entries, list):
        return list(entries)
    return []


def _resolve_local_tokens(
    tokens: Any, global_tokens: dict[str, str], section: str
) -> dict[str, str]:
    raw = _mapping_items(tokens)
    if not raw:
        return {}

    for key in raw:
        if key in global_tokens:
            raise ConfigError(
                f"Scoped token '{key}' in {section} conflicts with installation path token '{key}'."
            )

    authored = {key: str(value) for key, value in raw.items()}
    resolved: dict[str, str] = {}
    resolving: list[str] = []

    def resolve(key: str) -> str:
        if key in resolved:
            return resolved[key]
        if key in resolving:
            cycle = " -> ".join([*resolving[resolving.index(key):], key])
            raise ConfigError(
                f"Circular scoped token reference in {section}: {cycle}."
            )

        resolving.append(key)
        value = authored[key]
        for dependency in sorted(_tokens_in(value)):
            if dependency in authored:
                replacement = resolve(dependency)
            elif dependency in global_tokens:
                replacement = str(global_tokens[dependency])
            elif dependency in _RUNTIME_PATH_PLACEHOLDERS:
                continue
            else:
                raise ConfigError(
                    f"Scoped token '{key}' in {section} references unknown token(s): {dependency}."
                )
            value = value.replace("{" + dependency + "}", replacement)
        resolving.pop()
        resolved[key] = value
        return value

    for key in authored:
        resolve(key)

    return resolved


def _walk_scoped_refs(obj: Any, tokens: dict[str, str], parent_key: str = "") -> None:
    if isinstance(obj, Namespace):
        for key in obj.keys():
            value = object.__getattribute__(obj, key)
            resolved = _resolve_token_value(value, tokens, key)
            if resolved is not value:
                object.__setattr__(obj, key, resolved)
            else:
                _walk_scoped_refs(value, tokens, key)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            resolved = _resolve_token_value(value, tokens, str(key))
            if resolved is not value:
                obj[key] = resolved
            else:
                _walk_scoped_refs(value, tokens, str(key))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            resolved = _resolve_token_value(item, tokens, parent_key)
            if resolved is not item:
                obj[index] = resolved
            else:
                _walk_scoped_refs(item, tokens, parent_key)


def _resolve_token_value(value: Any, tokens: dict[str, str], key: str) -> Any:
    raw = str(value) if isinstance(value, Path) else value
    if not isinstance(raw, str) or "{" not in raw:
        return value

    substituted = _substitute_known_tokens(raw, tokens)
    if substituted == raw:
        return value

    if _is_path_key(key):
        unresolved = _unresolved_path_tokens(substituted)
        if unresolved:
            names = ", ".join(sorted(unresolved))
            raise ConfigError(
                f"Path config key '{key}' contains unresolved token(s): {names}."
            )
        return Path(substituted).expanduser()

    return substituted


def _validate_no_unresolved_path_tokens(obj: Any, parent_key: str = "") -> None:
    if isinstance(obj, Namespace):
        for key in obj.keys():
            value = object.__getattribute__(obj, key)
            _validate_path_value(key, value)
            _validate_no_unresolved_path_tokens(value, key)
    elif isinstance(obj, list):
        for item in obj:
            _validate_no_unresolved_path_tokens(item, parent_key)


def _validate_path_value(key: str, value: Any) -> None:
    if not _is_path_key(key):
        return
    raw = str(value) if isinstance(value, Path) else value
    if isinstance(raw, str) and "{" in raw:
        unresolved = _unresolved_path_tokens(raw)
        if not unresolved:
            return
        names = ", ".join(sorted(unresolved))
        raise ConfigError(f"Path config key '{key}' contains unresolved token(s): {names}.")


def _mapping_items(obj: Any) -> dict[str, Any]:
    if isinstance(obj, Namespace):
        return {str(key): value for key, value in obj.items()}
    if isinstance(obj, dict):
        return {str(key): value for key, value in obj.items()}
    return {}


def _child(obj: Any, key: str) -> Any:
    if isinstance(obj, Namespace):
        return getattr(obj, key, None)
    if isinstance(obj, dict):
        return obj.get(key)
    return None


def _substitute_known_tokens(text: str, tokens: dict[str, str]) -> str:
    for key, value in tokens.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _tokens_in(text: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(text))


def _unresolved_path_tokens(text: str) -> set[str]:
    return _tokens_in(text) - _RUNTIME_PATH_PLACEHOLDERS

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
from typing import Any, Iterable

from rey_lib.config.config_namespace import Namespace
from rey_lib.errors.error_utils import ConfigError

# Keys whose string values are filesystem paths and are resolved to Path
# objects. Explicit, reviewed membership only — path behaviour is never inferred
# from how a key is spelled (no suffix/name/regex matching, no fallback). Add a
# key here, under review, to opt it in. Notably absent: bare 'path' (the
# 'paths:' block is resolved explicitly by _build_path_resolver).
_PATH_KEYS = frozenset({
    "app_path", "artifacts_path", "config_path",
    "converted_path", "env_file", "failed_path",
    "inbox_path", "jsonl_path", "jsonl_root", "output_root",
    "processing_path", "raw_output_path",
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

#: The named locations file access may reach when an installation states no
#: ``security.allowed_roots`` (or states none), as the Console has always applied.
_DEFAULT_ALLOWED_ROOTS = ("root", "apps", "configs", "data", "logs", "state")


class PathResolver:
    """Resolved named paths from the installation config ``paths:`` list.

    Attached to ``ctx.paths`` after loading a ``config.yaml``.
    Call ``ctx.paths.resolve("name")`` to get the physical ``Path``.

    It also owns which of those locations the installation's file access may
    reach (backlog 1066): ``security.allowed_roots`` names them and configures
    this object; :meth:`authorize` is the one check that a location sits
    beneath one of them.
    """

    def __init__(
        self, paths: dict[str, Path], allowed_roots: Iterable[Any] | None = None,
    ) -> None:
        self._paths = dict(paths)
        self._runtime_tokens: dict[str, str] = {}
        # The permitted root NAMES. None stated, or an empty list, is the
        # default set -- the rule the Console applied before this owned it.
        self._allowed = tuple(str(name) for name in (allowed_roots or ())) \
            or _DEFAULT_ALLOWED_ROOTS

    def resolve(self, name: str) -> Path:
        """Return the resolved ``Path`` for the given logical name."""
        if name not in self._paths:
            raise ConfigError(f"Unknown path name: {name!r}")
        return self._paths[name]

    def allowed_roots(self) -> list[Path]:
        """The locations file access may reach, resolved.

        Each permitted name resolves as a named path; a name this installation
        does not configure is taken as a literal location, as it always was.
        """
        roots: list[Path] = []
        for name in self._allowed:
            try:
                roots.append(self.resolve(name).expanduser().resolve())
            except ConfigError:
                roots.append(Path(name).expanduser().resolve())
        return roots

    def authorize(self, raw_path: Any) -> Path:
        """One location, resolved, if it sits beneath a permitted root.

        A relative location is taken beneath ``root``; the result is resolved
        and must sit beneath one of :meth:`allowed_roots`.

        Raises:
            ConfigError: when it does not, or a relative location is given and
                this installation configures no ``root``.
        """
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            if "root" not in self._paths:
                raise ConfigError("Required path is not configured: root")
            path = self._paths["root"].expanduser() / path
        path = path.resolve()

        for root in self.allowed_roots():
            try:
                path.relative_to(root.resolve())
                return path
            except ValueError:
                continue
        raise ConfigError(f"Path is outside allowed roots: {path}")

    def authorize_beneath(self, root: str, relative_path: str) -> Path:
        """One location relative to a configured root, contained and authorized.

        Moved from the Console's file_explorer._authorized_path (backlog 1056),
        every check unchanged: an absolute or anchored relative location is
        refused, the candidate is resolved beneath the root and must still sit
        beneath it, and then the installation's allowed-root gate applies -- the
        configured root is necessary but not sufficient.

        Raises:
            ConfigError: when any of those refuses the location.
        """
        base = Path(root).expanduser().resolve()

        candidate = Path(relative_path)
        if candidate.is_absolute() or candidate.anchor:
            raise ConfigError("Explorer locations must be relative to the exploration root.")

        resolved = (base / candidate).resolve() if relative_path else base
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ConfigError(f"Path is outside the exploration root: {relative_path}") from exc

        # The configured root is necessary but not sufficient: the installation's
        # existing allowed-root boundary is enforced on every expansion as well.
        return self.authorize(str(resolved))

    def with_path(self, name: str, path: Path) -> "PathResolver":
        """The same resolver with one more named path, its permitted roots kept.

        Nothing else carries over, exactly as the rebuild it replaces did.
        """
        return PathResolver({**self._paths, str(name): path}, self._allowed)

    def __repr__(self) -> str:
        return f"PathResolver({list(self._paths)})"

def _build_path_resolver(
    raw_paths: Any,
    tokens: dict[str, str] | None = None,
    allowed_roots: Iterable[Any] | None = None,
) -> PathResolver:
    """Process a ``paths:`` list into a ``PathResolver``.

    Each entry must have ``name`` and ``path`` keys.  Values may reference
    earlier-resolved names with ``{name}`` placeholders. ``allowed_roots`` names
    the permitted locations (``security.allowed_roots``); None is the default.
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

    resolver = PathResolver(resolved_paths, allowed_roots)
    resolver._runtime_tokens = runtime_tokens
    return resolver


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_config_paths(
    tree: Namespace,
    declared_paths: Any,
    runtime_tokens: dict[str, str] | None = None,
) -> PathResolver:
    """Resolve a configuration tree's path references in place (backlog 963).

    The public access point to the resolution the context build applies: the
    declared ``paths`` list becomes a :class:`PathResolver`, and every logical
    ``{name}`` reference in ``tree`` is substituted from it, exactly as
    ``ContextBuilder`` does for a context. Nothing is added: run-time
    placeholders survive as they do there.

    Args:
        tree: The configuration to resolve, mutated in place.
        declared_paths: The ``paths:`` list of ``{name, path}`` entries.
        runtime_tokens: Values available to path templates, as the context
            build supplies them; None supplies none.

    Returns:
        The resolver built from ``declared_paths``.

    Raises:
        ConfigError: As the context build raises, for a path key left with an
            unresolved token.
    """
    resolver = _build_path_resolver(declared_paths, runtime_tokens)
    _apply_path_resolver(tree, resolver)
    return resolver


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


def _tokens_in(text: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(text))


def _unresolved_path_tokens(text: str) -> set[str]:
    return _tokens_in(text) - _RUNTIME_PATH_PLACEHOLDERS

"""The reference documents an AI contract declares, loaded for the package AI records.

The LLM package itself is no longer built here (#1305): the AI builds and records
it at the run it is asked for -- the task's contract beside the record it was
sent -- under the parent the calling application supplied
(``rey_lib.ai.evidence``). This module keeps the one piece every package shares:
resolving and loading what a contract declares as its references.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "load_contract_references",
]



def _resolve_reference_path(ctx: Any, raw_path: str) -> Path:
    """Resolve a contract-declared reference path through the existing resolver.

    Reuses ctx.paths (the installation PathResolver): each ``{name}`` token is
    replaced with its resolved path. No new resolver or path system is created.
    """
    import re

    resolver = getattr(ctx, "paths", None)

    def _sub(match: "re.Match[str]") -> str:
        if resolver is not None:
            try:
                return str(resolver.resolve(match.group(1)))
            except Exception:
                return match.group(0)
        return match.group(0)

    return Path(re.sub(r"\{([^}]+)\}", _sub, str(raw_path))).expanduser()


def load_contract_references(ctx: Any, declared: Any) -> list[dict[str, Any]] | None:
    """Resolve and load the reference documents a contract declares.

    ``declared`` is a contract's ``references`` list (or None/empty). Reuses the
    existing path-token resolver (ctx.paths) and the approved text loader
    (rey_lib.files.read_text_file), so every LLM package-construction path attaches
    the same reference contents. Returns None when nothing is declared. A required
    reference that cannot be resolved or loaded raises before the LLM request; a
    non-required one is omitted with a warning.
    """
    from rey_lib.files import read_text_file
    from rey_lib.logs.app_logger import AppLogger

    if not declared:
        return None

    loaded: list[dict[str, Any]] = []
    for ref in declared:
        if not isinstance(ref, dict):
            continue
        name = str(ref.get("name") or "")
        role = str(ref.get("role") or "")
        raw_path = str(ref.get("path") or "")
        required = bool(ref.get("required", True))
        try:
            content = read_text_file(_resolve_reference_path(ctx, raw_path))
        except Exception as exc:
            if required:
                raise ConfigError(
                    f"Required contract reference '{name}' could not be loaded "
                    f"from '{raw_path}': {exc}"
                ) from exc
            AppLogger(__name__).warning(
                "Skipping optional contract reference '%s' (%s): %s", name, raw_path, exc
            )
            continue
        entry: dict[str, Any] = {"name": name}
        if role:
            entry["role"] = role
        entry["content"] = content
        loaded.append(entry)
    return loaded

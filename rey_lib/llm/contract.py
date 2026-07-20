"""
Contract loading and versioning for LLM workflow.

A contract is a markdown file with a YAML frontmatter block that declares
name, version, and effective_date. The body is the instruction text sent
to the LLM as its system prompt.

Contracts are immutable once results exist against them. Changing a contract
requires bumping the version. The content hash provides a tamper-evident
check — if a file is edited without bumping the version the hash will differ
from the stored value and the discrepancy can be detected at audit time.

Public API
----------
load(path)
    Parse a contract file and return a Contract instance.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from rey_lib.config.config_utils import parse_yaml
from rey_lib.errors.error_utils import ConfigError

__all__ = ["Contract", "load"]

# Matches a YAML frontmatter block at the top of the file.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)


@dataclass(frozen=True)
class Contract:
    """A parsed, versioned LLM instruction contract.

    Attributes
    ----------
    name : str
        Contract identifier, matches the filename stem by convention.
    version : str
        Semantic version string (e.g. '1.0.0').
    effective_date : date
        Date this version became active.
    body : str
        Instruction text — sent to the LLM as the system prompt.
    hash : str
        SHA-256 hex digest of the full file content (frontmatter + body).
        Used as a tamper-evident fingerprint in stored results.
    path : Path
        Absolute path the contract was loaded from.
    raw_frontmatter : dict[str, Any]
        Full parsed frontmatter as a Python dict. Consumers (e.g. analysis.py)
        read domain-specific fields from here without this module knowing them.
    """

    name:            str
    version:         str
    effective_date:  date
    body:            str
    hash:            str
    path:            Path
    raw_frontmatter: dict[str, Any] = field(default_factory=dict, compare=False)


def load(path: Path) -> Contract:
    """Parse a contract markdown file and return a Contract instance.

    The file must begin with a YAML frontmatter block containing at least
    ``name``, ``version``, and ``effective_date``. Everything after the
    closing ``---`` is the instruction body.  Nested YAML structures (lists,
    dicts) are fully parsed and available via ``Contract.raw_frontmatter``.

    Parameters
    ----------
    path : Path
        Absolute path to the contract markdown file.

    Returns
    -------
    Contract
        Parsed and validated contract.

    Raises
    ------
    ConfigError
        If the file is missing, has no frontmatter, or is missing required
        frontmatter fields.
    """
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"Contract file not found: {path}")

    raw          = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        # Native YAML contract: the whole document is the contract. Parse it as
        # one mapping and keep the complete raw text as the instruction body.
        try:
            parsed = parse_yaml(raw)
        except ConfigError as exc:
            raise ConfigError(
                f"Contract '{path.name}' has invalid YAML: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigError(
                f"Contract '{path.name}' YAML root must be a mapping."
            )
        fields: dict[str, Any] = parsed
        body                   = raw
        field_source           = "document root"
    else:
        # Markdown contract: existing frontmatter-plus-body format, unchanged.
        m = _FRONTMATTER_RE.match(raw)
        if not m:
            raise ConfigError(
                f"Contract '{path.name}' has no frontmatter block. "
                "Add a '---' delimited block with name, version, and effective_date."
            )
        try:
            fields = parse_yaml(m.group(1)) or {}
        except ConfigError as exc:
            raise ConfigError(
                f"Contract '{path.name}' has invalid YAML frontmatter: {exc}"
            ) from exc
        body         = raw[m.end():].strip()
        field_source = "frontmatter"

    for required in ("name", "version", "effective_date"):
        if required not in fields:
            raise ConfigError(
                f"Contract '{path.name}' {field_source} is missing required field '{required}'."
            )

    raw_date = fields["effective_date"]
    try:
        effective_date = (
            raw_date if isinstance(raw_date, date)
            else date.fromisoformat(str(raw_date).strip())
        )
    except ValueError as exc:
        raise ConfigError(
            f"Contract '{path.name}': effective_date '{raw_date}' "
            "is not a valid ISO date (YYYY-MM-DD)."
        ) from exc

    return Contract(
        name            = str(fields["name"]).strip(),
        version         = str(fields["version"]).strip(),
        effective_date  = effective_date,
        body            = body,
        hash            = content_hash,
        path            = path,
        raw_frontmatter = fields,
    )

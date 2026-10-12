"""
Contract loading and versioning for LLM workflow.

A contract is a markdown file with a YAML frontmatter block that declares
name, version, and effective_date. The body is the instruction text sent
to the LLM as its system prompt.

Contracts are immutable once results exist against them. Changing a contract
requires bumping the version. The content hash provides a tamper-evident
check — if a file is edited without bumping the version the hash will differ
from the stored value and the discrepancy can be detected at audit time.

Contracts are stored in control.analysis_contract (backlog 903) as their
complete raw text plus its format; nothing reads a contract file at runtime.

Public API
----------
parse_contract(text, fmt)
    Parse a contract's raw text and return a Contract instance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from rey_lib.config.config_utils import parse_yaml
from rey_lib.encryption import sha256_text
from rey_lib.errors.error_utils import ConfigError

__all__ = ["CONTRACT_FORMATS", "Contract", "parse_contract"]

# Matches a YAML frontmatter block at the top of the file.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)


#: The serializations a contract is stored in: YAML (the whole document is the
#: contract) or Markdown (a frontmatter block, then the body). Not derivable
#: from the text alone -- both may begin with ``---`` -- so it is stored with it.
CONTRACT_FORMATS: tuple[str, ...] = ("yaml", "markdown")



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
    path : Optional[Path]
        Where the contract came from, when it came from a file; None for a
        stored or inline contract.
    raw_frontmatter : dict[str, Any]
        Full parsed frontmatter as a Python dict. Consumers (e.g. analysis.py)
        read domain-specific fields from here without this module knowing them.
    text : str
        The complete raw serialization the contract was parsed from.
    """

    name:            str
    version:         str
    effective_date:  date
    body:            str
    hash:            str
    path:            Optional[Path]
    raw_frontmatter: dict[str, Any] = field(default_factory=dict, compare=False)
    text:            str            = field(default="", compare=False)


def parse_contract(text: str, fmt: str, *, path: Optional[Path] = None) -> Contract:
    """Parse a contract's raw text and return a Contract instance.

    The one contract parser (backlog 903). ``yaml``: the whole document is the
    contract -- it is parsed as one mapping and the complete text is the
    instruction body. ``markdown``: a ``---`` delimited frontmatter block, then
    the body. Either way it must declare ``name``, ``version`` and
    ``effective_date``, and the hash is taken over the full text.

    Parameters
    ----------
    text : str
        The complete raw contract text.
    fmt : str
        Its serialization, one of :data:`CONTRACT_FORMATS`.
    path : Optional[Path]
        Where the text came from, when it came from a file; recorded only.

    Returns
    -------
    Contract
        Parsed and validated contract.

    Raises
    ------
    ConfigError
        If the format is unknown, there is no frontmatter, or a required field
        is missing or invalid.
    """
    label = path.name if path is not None else f"{fmt} contract text"
    if fmt not in CONTRACT_FORMATS:
        raise ConfigError(
            f"Contract '{label}': format '{fmt}' is not one of {list(CONTRACT_FORMATS)}."
        )

    content_hash = sha256_text(text)

    if fmt == "yaml":
        # Native YAML contract: the whole document is the contract. Parse it as
        # one mapping and keep the complete raw text as the instruction body.
        try:
            parsed = parse_yaml(text)
        except ConfigError as exc:
            raise ConfigError(
                f"Contract '{label}' has invalid YAML: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigError(
                f"Contract '{label}' YAML root must be a mapping."
            )
        fields: dict[str, Any] = parsed
        body                   = text
        field_source           = "document root"
    else:
        # Markdown contract: frontmatter-plus-body format.
        m = _FRONTMATTER_RE.match(text)
        if not m:
            raise ConfigError(
                f"Contract '{label}' has no frontmatter block. "
                "Add a '---' delimited block with name, version, and effective_date."
            )
        try:
            fields = parse_yaml(m.group(1)) or {}
        except ConfigError as exc:
            raise ConfigError(
                f"Contract '{label}' has invalid YAML frontmatter: {exc}"
            ) from exc
        body         = text[m.end():].strip()
        field_source = "frontmatter"

    for required in ("name", "version", "effective_date"):
        if required not in fields:
            raise ConfigError(
                f"Contract '{label}' {field_source} is missing required field '{required}'."
            )

    raw_date = fields["effective_date"]
    try:
        effective_date = (
            raw_date if isinstance(raw_date, date)
            else date.fromisoformat(str(raw_date).strip())
        )
    except ValueError as exc:
        raise ConfigError(
            f"Contract '{label}': effective_date '{raw_date}' "
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
        text            = text,
    )

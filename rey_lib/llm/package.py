"""
Canonical LLM package builder
(SGC_Rey_Lib_Canonical_LLM_Package_And_Contract_Evidence).

One provider-neutral, JSON-serializable representation of everything prepared for
an LLM invocation:

    {
      "analysis":          {"name": ..., ...},
      "contract":          {"path": ..., "hash": ..., "content": ...},
      "inputs":            [{"source_path": ..., "content": ..., ...}, ...],
      "execution_context": {...},
    }

The contract is instructions and execution evidence, kept structurally separate
from ``inputs`` — it is never one of the ordinary source inputs. ``inputs`` is
always an ordered collection; the generic builder supports one or more, while
current rey_analyzer analyses supply exactly one.

This module owns the package *shape* only. It does not invoke a provider, own
analyzer orchestration, read YAML configuration, or implement low-level file
reading: file content and hashes come through ``rey_lib.files``. ``build_package``
is a pure, deterministic assembly with no I/O; ``read_input`` is the one helper
that touches the filesystem, and it does so through the shared file utilities.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rey_lib.files import file_sha256, read_text_file

__all__ = [
    "LlmPackageContract",
    "LlmPackageInput",
    "build_package",
    "read_input",
]


@dataclass(frozen=True)
class LlmPackageContract:
    """Identity and content evidence for the resolved LLM contract.

    ``path`` and ``hash`` are the already-resolved contract evidence supplied by
    the caller; this builder never performs a second contract lookup. ``content``
    is the contract text preserved in the package.

    Attributes
    ----------
    path : str
        Resolved contract file path.
    hash : str
        Content hash of the resolved contract, as already computed by the caller.
    content : str
        Contract text. May be empty when the caller intentionally omits content.
    """

    path: str
    hash: str
    content: str = ""


@dataclass(frozen=True)
class LlmPackageInput:
    """One logical source input supplied to an analysis.

    Attributes
    ----------
    source_path : str
        Path the input was read from, for lineage.
    content : Any
        The input content placed in the package (typically text).
    artifact_id : str
        Optional upstream artifact identity, when known.
    input_hash : str
        Optional content hash of the input.
    media_type : str
        Optional media type of the source.
    name : str
        Optional semantic name/role for the input.
    """

    source_path: str
    content: Any
    artifact_id: str = ""
    input_hash: str = ""
    media_type: str = ""
    name: str = ""


def read_input(
    source_path: Path | str,
    *,
    name: str = "",
    artifact_id: str = "",
    media_type: str = "",
) -> LlmPackageInput:
    """Read one source file into a package input through the shared file utilities.

    File access and hashing go through ``rey_lib.files`` — this introduces no new
    low-level file reading. The read is recorded by the shared file boundary, so
    the input also appears as standard file evidence.

    Parameters
    ----------
    source_path : Path | str
        The file to read.
    name : str
        Optional semantic name/role for the input.
    artifact_id : str
        Optional upstream artifact identity.
    media_type : str
        Optional media type of the source.

    Returns
    -------
    LlmPackageInput
        The input descriptor with content and hash populated.
    """
    path = str(source_path)
    return LlmPackageInput(
        source_path=path,
        content=read_text_file(source_path),
        input_hash=file_sha256(source_path),
        name=name,
        artifact_id=artifact_id,
        media_type=media_type,
    )


def _analysis_section(analysis: Mapping[str, Any] | str) -> dict[str, Any]:
    """Return the analysis section, requiring at least a name."""
    if isinstance(analysis, str):
        return {"name": analysis}
    section = {str(key): value for key, value in dict(analysis).items()}
    if not str(section.get("name") or ""):
        raise ValueError("Canonical package analysis requires a name.")
    return section


def _contract_section(contract: LlmPackageContract | Mapping[str, Any]) -> dict[str, Any]:
    """Return the contract section, keeping it separate from inputs."""
    if isinstance(contract, LlmPackageContract):
        return {"path": contract.path, "hash": contract.hash, "content": contract.content}
    data = dict(contract)
    return {
        "path": str(data.get("path") or ""),
        "hash": str(data.get("hash") or ""),
        "content": data.get("content", ""),
    }


def _input_section(item: LlmPackageInput | Mapping[str, Any]) -> dict[str, Any]:
    """Return one input entry with its minimum and any present evidence fields."""
    if isinstance(item, LlmPackageInput):
        source_path, content = item.source_path, item.content
        optional = {
            "artifact_id": item.artifact_id,
            "input_hash": item.input_hash,
            "media_type": item.media_type,
            "name": item.name,
        }
    else:
        data = dict(item)
        source_path = str(data.get("source_path") or "")
        content = data.get("content", "")
        optional = {
            key: data.get(key, "")
            for key in ("artifact_id", "input_hash", "media_type", "name")
        }
    entry: dict[str, Any] = {"source_path": source_path, "content": content}
    # Optional evidence fields appear only when populated, so the shape stays
    # minimal for the common case while remaining inspectable when known.
    entry.update({key: value for key, value in optional.items() if value})
    return entry


def build_package(
    *,
    analysis: Mapping[str, Any] | str,
    contract: LlmPackageContract | Mapping[str, Any],
    inputs: Sequence[LlmPackageInput | Mapping[str, Any]],
    execution_context: Mapping[str, Any] | None = None,
    references: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble one canonical LLM package.

    Pure and deterministic: equivalent ordered inputs and metadata always produce
    an equal package, and no provider is invoked. The contract is kept as its own
    top-level section and is never inserted into ``inputs``. ``inputs`` is always
    an ordered collection — the generic builder accepts one or more; a caller that
    supplies one produces exactly one entry.

    Parameters
    ----------
    analysis : Mapping[str, Any] | str
        Analysis identity/metadata; a bare string is treated as the name.
    contract : LlmPackageContract | Mapping[str, Any]
        Resolved contract identity and content, structurally separate from inputs.
    inputs : Sequence[LlmPackageInput | Mapping[str, Any]]
        Ordered source inputs. Order is preserved.
    execution_context : Mapping[str, Any] | None
        Run/execution metadata needed to interpret the request.

    Returns
    -------
    dict[str, Any]
        The canonical package with analysis, contract, inputs, and
        execution_context top-level sections.
    """
    package: dict[str, Any] = {
        "analysis": _analysis_section(analysis),
        "contract": _contract_section(contract),
    }
    if references:
        package["references"] = [dict(reference) for reference in references]
    package["inputs"] = [_input_section(item) for item in inputs]
    package["execution_context"] = dict(execution_context or {})
    return package

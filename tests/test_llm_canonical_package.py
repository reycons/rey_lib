"""Tests for the canonical LLM package builder
(SGC_Rey_Lib_Canonical_LLM_Package_And_Contract_Evidence).

The builder owns one provider-neutral package shape: analysis, contract, inputs,
and execution_context, with the contract kept structurally separate from inputs
and inputs always an ordered collection.
"""

from __future__ import annotations

from pathlib import Path

from rey_lib.files import file_sha256
from rey_lib.llm.package import (
    LlmPackageContract,
    LlmPackageInput,
    build_package,
    read_input,
)


def _contract() -> LlmPackageContract:
    return LlmPackageContract(path="/contracts/x.md", hash="c0ffee", content="RULES")


# TEST-001
def test_build_canonical_package_with_one_input() -> None:
    """One input: all four sections exist, one entry, contract stays separate."""
    package = build_package(
        analysis={"name": "file_profile_to_loader_config", "run_id": "r1"},
        contract=_contract(),
        inputs=[LlmPackageInput(source_path="/in/profile.json", content={"a": 1})],
        execution_context={"run_id": "r1", "app": "rey_analyzer"},
    )
    assert set(package) == {"analysis", "contract", "inputs", "execution_context"}
    assert package["analysis"]["name"] == "file_profile_to_loader_config"
    assert package["contract"] == {"path": "/contracts/x.md", "hash": "c0ffee", "content": "RULES"}
    assert isinstance(package["inputs"], list)
    assert len(package["inputs"]) == 1
    # The contract is never one of the inputs.
    assert all("contract" not in entry for entry in package["inputs"])
    assert package["inputs"][0]["source_path"] == "/in/profile.json"


# TEST-002
def test_build_canonical_package_with_multiple_ordered_inputs() -> None:
    """The generic builder supports one_or_more inputs and preserves order.

    This is a rey_lib capability test; no analyzer execution path is involved.
    """
    inputs = [
        LlmPackageInput(source_path="/in/a.json", content="A", name="first"),
        LlmPackageInput(source_path="/in/b.json", content="B", name="second"),
        LlmPackageInput(source_path="/in/c.json", content="C", name="third"),
    ]
    package = build_package(
        analysis="multi", contract=_contract(), inputs=inputs,
    )
    assert [entry["name"] for entry in package["inputs"]] == ["first", "second", "third"]
    assert [entry["source_path"] for entry in package["inputs"]] == [
        "/in/a.json", "/in/b.json", "/in/c.json",
    ]
    # Still exactly one contract, still separate.
    assert package["contract"]["hash"] == "c0ffee"


def test_inputs_is_always_a_collection_even_when_empty() -> None:
    """inputs is a list invariant, never a scalar or None."""
    package = build_package(analysis="a", contract=_contract(), inputs=[])
    assert package["inputs"] == []


def test_package_construction_is_deterministic() -> None:
    """Equivalent ordered inputs and metadata produce an equal package."""
    kwargs = dict(
        analysis={"name": "a", "run_id": "r"},
        contract=_contract(),
        inputs=[LlmPackageInput(source_path="/in/a", content="A", input_hash="h")],
        execution_context={"run_id": "r"},
    )
    assert build_package(**kwargs) == build_package(**kwargs)


def test_optional_evidence_fields_appear_only_when_populated() -> None:
    """A bare input carries only the minimum fields; evidence fields when known."""
    bare = build_package(
        analysis="a", contract=_contract(),
        inputs=[LlmPackageInput(source_path="/in/a", content="A")],
    )["inputs"][0]
    assert set(bare) == {"source_path", "content"}

    evidenced = build_package(
        analysis="a", contract=_contract(),
        inputs=[LlmPackageInput(
            source_path="/in/a", content="A", input_hash="h",
            artifact_id="art-1", media_type="application/json", name="profile",
        )],
    )["inputs"][0]
    assert evidenced["input_hash"] == "h"
    assert evidenced["artifact_id"] == "art-1"
    assert evidenced["media_type"] == "application/json"
    assert evidenced["name"] == "profile"


def test_read_input_uses_shared_file_utilities(tmp_path: Path) -> None:
    """read_input reads content and hash through rey_lib.files, not a new layer."""
    source = tmp_path / "profile.json"
    source.write_text('{"columns": ["a", "b"]}', encoding="utf-8")

    entry = read_input(source, name="profile", media_type="application/json")
    assert entry.source_path == str(source)
    assert entry.content == '{"columns": ["a", "b"]}'
    # Hash is the shared file utility's hash, not a re-implementation.
    assert entry.input_hash == file_sha256(source)
    assert entry.name == "profile"

    # It composes into a package as one input.
    package = build_package(analysis="a", contract=_contract(), inputs=[entry])
    assert package["inputs"][0]["input_hash"] == file_sha256(source)


def test_builder_does_not_invoke_a_provider() -> None:
    """The builder is pure assembly: it imports no provider/runner/orchestration."""
    import ast

    import rey_lib.llm.package as pkg

    tree = ast.parse(Path(pkg.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    # File utilities only — never the provider, runner, adapters, or analysis layer.
    assert "rey_lib.files" in imported
    for forbidden in ("rey_lib.llm.runner", "rey_lib.llm.adapters",
                      "rey_lib.llm.providers", "rey_lib.llm.analysis",
                      "rey_lib.llm.llm_utils"):
        assert forbidden not in imported, forbidden


# --- Legacy provider wire package is preserved (TEST-008) ---------------------
# SGC reconciliation (c): the log-analysis LLM_PACKAGE is the existing provider
# wire package, not the canonical representation, and is preserved unchanged.

def test_legacy_llm_package_wire_shape_is_unchanged() -> None:
    """The log-analysis package keeps its four-field wire shape."""
    from rey_lib.logs.llm_package import _build_analysis_package

    package = _build_analysis_package(
        analysis_name="log_interpreter",
        source_record_type="RESULTS_SUMMARY",
        instructions={"rules": ["explain failures"]},
        source={"record_type": "RESULTS_SUMMARY", "summary": "..."},
    )
    assert set(package) == {"analysis_name", "source_record_type", "instructions", "source"}
    # Not the canonical shape.
    assert "contract" not in package
    assert "inputs" not in package
    assert "execution_context" not in package


def test_legacy_and_canonical_packages_are_distinct_shapes() -> None:
    """The two representations are intentionally different structures."""
    from rey_lib.logs.llm_package import _build_analysis_package

    legacy = _build_analysis_package(
        "a", "RESULTS_SUMMARY", {"r": 1}, {"record_type": "RESULTS_SUMMARY"},
    )
    canonical = build_package(
        analysis="a", contract=LlmPackageContract(path="/c", hash="h", content="R"),
        inputs=[LlmPackageInput(source_path="/s", content="X")],
    )
    assert set(legacy) != set(canonical)


def test_log_package_module_has_no_canonical_downconvert_adapter() -> None:
    """No ceremonial build-canonical-then-restate-legacy adapter is introduced."""
    from pathlib import Path

    import rey_lib.logs.llm_package as legacy_module

    source = Path(legacy_module.__file__).read_text(encoding="utf-8")
    # The log path does not import or route through the canonical builder.
    assert "rey_lib.llm.package" not in source
    assert "build_package" not in source


# --- Shared record recognition (REQ-006 / TEST-006) --------------------------

def test_llm_contract_and_context_project_into_results_by_type(tmp_path: Path) -> None:
    """LLM_CONTRACT and LLM_CONTEXT are recognized as result evidence by type.

    Recognized without a record_group so they are correlatable to their analysis
    (SGC_Rey_Lib_Canonical_LLM_Package_And_Contract_Evidence, REQ-006).
    """
    import json

    from rey_lib.logs.evidence_projection import read_run_log_sections

    records = [
        {"record_type": "RUN_START", "record_group": "execution",
         "run_id": "r1", "run_timestamp": "20260716_000000"},
        {"record_type": "LLM_CONTRACT", "run_id": "r1",
         "analysis_name": "a", "contract_path": "/c.md", "contract_hash": "h"},
        {"record_type": "LLM_CONTEXT", "run_id": "r1",
         "analysis_name": "a", "payload": {"x": 1}},
        {"record_type": "RUN_COMPLETE", "record_group": "execution",
         "run_id": "r1", "run_timestamp": "20260716_000000",
         "status": "success", "timestamp": "2026-07-16T00:00:01+00:00"},
    ]
    log = tmp_path / "run.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    result_types = [
        r["record_type"]
        for r in read_run_log_sections(log)["sections"]["results"]["records"]
    ]
    assert "LLM_CONTRACT" in result_types
    assert "LLM_CONTEXT" in result_types

"""Focused tests for the Loader mutation-context boundary.

Copied from the legacy file_operator tests and pointed at the Loader-owned
copy, rey_lib.load.mutation_context (row 589, step 4).
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

import rey_lib.load
from rey_lib.load.mutation_context import (
    MutationContextError,
    build_mutation_context,
    log_governed_source_file_mutation,
)


def test_file_id_is_required_before_the_shared_writer_can_be_called() -> None:
    with patch(
        "rey_lib.load.mutation_context._shared_log_source_file_mutation"
    ) as writer:
        with pytest.raises(MutationContextError, match="governed file id"):
            context = build_mutation_context(
                {"record_type": "source_file_classification"}
            )
            log_governed_source_file_mutation(
                object(), context, action="move", status="success"
            )

    writer.assert_not_called()


def test_existing_classification_is_forwarded_exactly() -> None:
    classification = {
        "TyPe": "Unfamiliar",
        "source_field": None,
        "values": {"MixedCase": "Kept", "optional": None},
        "future": {"enabled": False, "items": [3, 1]},
    }
    context = build_mutation_context(
        {"file_id": 4001, "classification": classification}
    )

    assert context.file_id == 4001
    assert context.classification == classification
    assert context.classification is not classification

    with patch(
        "rey_lib.load.mutation_context._shared_log_source_file_mutation",
        return_value=7,
    ) as writer:
        assert log_governed_source_file_mutation(
            object(), context, action="create", status="success"
        ) == 7

    assert writer.call_args.kwargs["file_id"] == 4001
    assert writer.call_args.kwargs["classification"] == classification


def test_absent_classification_remains_absent() -> None:
    context = build_mutation_context({"file_id": 1001})

    assert context.classification is None


def test_present_non_mapping_classification_is_rejected() -> None:
    with pytest.raises(MutationContextError, match="must be a mapping"):
        build_mutation_context(
            {"file_id": 1001, "classification": "not-governed-shape"}
        )


def test_only_mutation_context_module_calls_the_shared_writer_directly() -> None:
    package = Path(rey_lib.load.__file__).resolve().parent
    violations: list[str] = []
    for path in package.rglob("*.py"):
        if path.name == "mutation_context.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "rey_lib.files":
                if any(
                    alias.name == "log_source_file_mutation" for alias in node.names
                ):
                    violations.append(str(path.relative_to(package)))
            if isinstance(node, ast.Attribute) and node.attr == "log_source_file_mutation":
                violations.append(str(path.relative_to(package)))

    assert violations == []

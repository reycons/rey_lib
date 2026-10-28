"""FileManifest selects the governed files remaining for one operation (#1110 S3).

Moved from tests/load/test_manifest_source.py with ``get_for_operation``: the
selection is FileManifest's, and it answers one row set per governed file.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.manifest import FileManifest
from rey_lib.load.manifest_source import ManifestSource
from tests.load.test_manifest_source import CountingReader, _row


class _SetReader:
    """The manifest retrieval contract answered with a set, recording each call."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = list(rows)
        self.calls: list[dict[str, Any]] = []

    def get_file_manifests(self, **filters: Any) -> Sequence[Mapping[str, Any]]:
        self.calls.append(filters)
        return self.rows


class TestOneReadForTheOperationsWholeSet:
    """get_for_operation: one call, one row set per file, in the routine's order."""

    @staticmethod
    def _three_files() -> list[dict[str, Any]]:
        return [
            _row(file_manifest_id=10, file_mutation_id=25, data_profile_field_id=1),
            _row(file_manifest_id=10, file_mutation_id=25, data_profile_field_id=2),
            _row(file_manifest_id=11, file_mutation_id=31),
            _row(file_manifest_id=12, file_mutation_id=40),
        ]

    def test_the_whole_set_is_one_call(self) -> None:
        reader = _SetReader(self._three_files())

        FileManifest.get_for_operation(reader, 3, "file_sanitization")

        assert reader.calls == [{
            "installation_id": 3, "operation": "file_sanitization", "source_name": None,
        }]

    def test_one_object_per_file_in_the_routines_order(self) -> None:
        sources = FileManifest.get_for_operation(
            _SetReader(self._three_files()), 3, "file_sanitization",
        )

        assert [(s[0]["file_manifest_id"], s[0]["file_mutation_id"]) for s in sources] == [
            (10, 25), (11, 31), (12, 40),
        ]

    def test_each_object_is_what_create_builds_from_its_rows(self) -> None:
        rows = self._three_files()
        sources = FileManifest.get_for_operation(_SetReader(rows), 3, "file_sanitization")

        one = ManifestSource.create(CountingReader(rows[:2]), file_mutation_id=25)

        assert sources[0] == one._rows

    def test_the_source_is_passed_through(self) -> None:
        reader = _SetReader([])

        FileManifest.get_for_operation(reader, 3, "file_classification", source_name="fidelity")

        assert reader.calls[0]["source_name"] == "fidelity"

    def test_nothing_remaining_is_no_objects(self) -> None:
        assert FileManifest.get_for_operation(_SetReader([]), 3, "data_profile") == []

    def test_a_reader_of_the_retired_contract_is_refused_by_name(self) -> None:
        with pytest.raises(ConfigError, match="get_file_manifests"):
            FileManifest.get_for_operation(object(), 3, "data_profile")

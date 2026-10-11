"""Where one mutation wrote, answered by the file manifest (backlog 733).

Asserted: one mutation is read through the manifest's own read; its destination
is interpreted only by MutationDestination -- a table is a
DatabaseObjectIdentity, a file or an absent destination is refused explicitly;
and DatabaseObjectIdentity reads back exactly what it wrote.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.manifest import FileManifest, MutationDestination


class _Control:
    """Answers list_file_mutations from fixed rows, recording each call."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def list_file_mutations(
        self, file_manifest_id: Optional[int] = None,
        file_mutation_id: Optional[int] = None, required: bool = True,
    ) -> list[dict[str, Any]]:
        self.calls.append({"file_manifest_id": file_manifest_id,
                           "file_mutation_id": file_mutation_id, "required": required})
        return [row for row in self.rows if row["file_mutation_id"] == file_mutation_id]


_TABLE = DatabaseObjectIdentity("warehouse", "", "trading", "trades")


def _manifest(destination: Any) -> tuple[FileManifest, _Control]:
    control = _Control([{"file_mutation_id": 12, "destination": destination}])
    return FileManifest(control), control


class TestOneMutation:

    def test_it_is_read_through_the_manifests_own_read(self) -> None:
        manifest, control = _manifest(None)

        assert manifest.mutation(12)["file_mutation_id"] == 12
        assert control.calls == [
            {"file_manifest_id": None, "file_mutation_id": 12, "required": False},
        ]

    def test_an_unknown_mutation_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="No file mutation 99"):
            _manifest(None)[0].mutation(99)


class TestItsDestination:

    def test_a_table_is_its_database_identity(self) -> None:
        destination = _manifest(_TABLE.to_dict())[0].mutation_destination(12)

        assert destination.is_database()
        assert destination.database_identity() == _TABLE

    def test_a_file_is_refused_explicitly_as_not_a_database_destination(self) -> None:
        destination = _manifest({"path": "/out/trades.csv", "file_type": "csv"})[0]\
            .mutation_destination(12)

        assert not destination.is_database()
        with pytest.raises(ConfigError, match="wrote a file .*not a queryable database"):
            destination.database_identity()

    def test_no_destination_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="has no destination"):
            _manifest(None)[0].mutation_destination(12).database_identity()

    def test_an_incomplete_identity_is_refused_by_the_identity(self) -> None:
        destination = MutationDestination(12, {"name": "trades", "schema": "trading"})

        with pytest.raises(ConfigError, match="connection"):
            destination.database_identity()


class TestTheIdentityReadsBackWhatItWrote:

    def test_round_trip(self) -> None:
        assert DatabaseObjectIdentity.from_dict(_TABLE.to_dict()) == _TABLE

    def test_a_catalog_may_be_empty_and_others_may_not(self) -> None:
        with pytest.raises(ConfigError, match="schema, name"):
            DatabaseObjectIdentity.from_dict({"connection": "w"})

"""What the relational destination sends, and what it never sends.

No database is opened. These assert the boundary the schema declares: staging
is filled, one procedure is called, and nothing touches storage -- which is the
whole reason the writer can hold no privilege on it.
"""

from __future__ import annotations

from typing import Any

from rey_lib.repository_map.code_index import CodeIndexWriter, IndexedRepository
from rey_lib.repository_map.code_index_database import DatabaseCodeIndexWriter


class _Adapter:
    """An adapter that records instead of connecting."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.inserts: list[tuple[str, str, int, list[str]]] = []

    def execute_sql(
        self,
        _conn: Any,
        sql_text: str,
        _named_params: dict[str, Any],
        _result_mode: str,
    ) -> None:
        self.statements.append(sql_text)

    def bulk_insert(
        self,
        _conn: Any,
        schema: str,
        table: str,
        rows: list[dict[str, Any]],
        columns: list[str],
    ) -> int:
        self.inserts.append((schema, table, len(rows), columns))
        return len(rows)


def _indexed() -> list[IndexedRepository]:
    return [
        IndexedRepository(
            repository="rey_loader",
            header={
                "repository_key": "rey_loader",
                "revision": "abc123",
                "branch": "main",
                "working_tree_status": "clean",
                "content_hash": "c" * 64,
                "generator_version": "1.0.0",
                "rules_hash": "r" * 64,
            },
            files=[
                {
                    "relative_path": "db.py",
                    "language": "Python",
                    "content_hash": "f" * 64,
                    "is_generated": False,
                    "is_vendor": False,
                    "is_test": False,
                    "symbols": [
                        {
                            "symbol_kind": "function",
                            "name": "connect",
                            "qualified_name": "connect",
                            "owner": "",
                            "is_public": True,
                            "start_line": 10,
                            "start_column": 0,
                        }
                    ],
                }
            ],
        )
    ]


class TestTheWriteBoundary:
    """Staging and one call, and nothing else."""

    def test_it_satisfies_the_writer_contract(self) -> None:
        assert isinstance(DatabaseCodeIndexWriter(_Adapter(), None), CodeIndexWriter)

    def test_storage_is_never_written_to_directly(self) -> None:
        """The writer holds no privilege on storage; it must not try to use one."""
        adapter = _Adapter()
        DatabaseCodeIndexWriter(adapter, None).replace_index(_indexed())

        staged = {table for _schema, table, _count, _columns in adapter.inserts}
        assert staged <= {"repository_stage", "file_stage", "symbol_stage"}

        # Every statement either empties staging or calls a procedure. A
        # storage table named anywhere would mean the writer needs a privilege
        # the schema does not give it.
        for statement in adapter.statements:
            if statement.startswith("CALL"):
                continue
            assert statement.startswith("DELETE FROM code.")
            assert statement.endswith("_stage")

    def test_the_replacement_is_one_call(self) -> None:
        """Atomicity is the schema's, so exactly one promotion is issued."""
        adapter = _Adapter()
        DatabaseCodeIndexWriter(adapter, None).replace_index(_indexed())

        calls = [s for s in adapter.statements if s.startswith("CALL")]
        assert calls == ["CALL code.p_index_replace()"]

    def test_staging_is_emptied_before_it_is_filled(self) -> None:
        """A previous run that staged and failed to promote must not survive.

        Otherwise the next promotion would publish a mixture of two scans.
        """
        adapter = _Adapter()
        DatabaseCodeIndexWriter(adapter, None).replace_index(_indexed())

        deletes = [s for s in adapter.statements if s.startswith("DELETE")]
        assert deletes == [
            "DELETE FROM code.symbol_stage",
            "DELETE FROM code.file_stage",
            "DELETE FROM code.repository_stage",
        ]

    def test_a_symbol_carries_the_keys_that_resolve_it(self) -> None:
        """Staging holds natural keys; storage identity is minted by the schema."""
        adapter = _Adapter()
        DatabaseCodeIndexWriter(adapter, None).replace_index(_indexed())

        columns = {table: cols for _s, table, _c, cols in adapter.inserts}
        assert columns["symbol_stage"][:2] == ["repository_key", "relative_path"]
        for staged in columns.values():
            assert not any(name.endswith("_id") for name in staged)

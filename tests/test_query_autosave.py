"""Workbench retention uses existing owners and the mapped Control procedure.

The store below is isolated fixture state. These tests never write the live DB.
"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.control.control import Control
from rey_lib.db.database_query import DatabaseQuery
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.load import Source
from rey_lib.load import source as source_module


class SavedQueries:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.failure: DatabaseError | None = None

    def get_saved_query(self, saved_query_id: int) -> dict[str, Any] | None:
        row = self.rows.get(saved_query_id)
        return dict(row) if row is not None else None

    def save_query(self, saved_query_id: int | None, name: str,
                   sql_statement: str, sql_dialect: str) -> dict[str, Any]:
        self.calls.append((saved_query_id, name, sql_statement, sql_dialect))
        if self.failure is not None:
            raise self.failure
        identity = saved_query_id or len(self.rows) + 1
        row = {**self.rows.get(identity, {"saved_query_id": identity, "name": name}),
               "sql_statement": sql_statement, "sql_dialect": sql_dialect}
        self.rows[identity] = row
        return dict(row)


def test_edits_do_not_save_until_the_owner_save_operation_is_called() -> None:
    store = SavedQueries()
    query = DatabaseQuery("SELECT 1")

    assert query.save(store) == 1
    query.update(statement="SELECT 2")
    assert len(store.calls) == 1
    assert store.rows[1]["sql_statement"] == "SELECT 1"
    assert query.projection()["navigation"]["modified"] is True
    query.save(store)

    assert len(store.rows) == 1
    assert store.calls[-1] == (1, "Adhoc query", "SELECT 2", "sql")
    assert query.projection()["saved_query_id"] == 1
    assert query.projection()["navigation"] == {
        "modified": False, "can_close": True, "label": "Adhoc query"}
    assert "result" not in query.projection()


def test_blank_draft_waits_for_text_and_clearing_keeps_the_last_saved_text() -> None:
    store = SavedQueries()
    query = DatabaseQuery("")
    assert query.save(store) is None
    assert store.calls == []
    query.update(statement="SELECT 1")
    assert store.calls == []
    query.save(store)
    query.update(statement="  ")
    assert store.rows[1]["sql_statement"] == "SELECT 1"
    assert query.projection()["statement"] == "  "
    assert query.projection()["navigation"]["modified"] is True


def test_internal_queries_never_gain_persistence_from_construction_or_update() -> None:
    query = DatabaseQuery("SELECT :value", {"value": 10})
    query.update(statement="SELECT 2")
    assert "saved_query_id" not in query.projection()
    assert query.projection()["navigation"]["modified"] is True


def test_repeated_edits_and_simultaneous_save_requests_do_not_create_copies() -> None:
    store = SavedQueries()
    query = DatabaseQuery("SELECT 1")
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: query.save(store), range(8))) == [1] * 8
    query.update(statement="SELECT 1", current_page=2)
    assert len(store.calls) == 1
    assert len(store.rows) == 1
    other = DatabaseQuery("SELECT 1")
    assert other.save(store) == 2


def test_execution_connection_is_not_persisted_and_dialect_is_metadata() -> None:
    store = SavedQueries()
    query = DatabaseQuery("SELECT 1", connection=SimpleNamespace(name="first", provider="duckdb"))
    query.save(store)
    query.update(connection=SimpleNamespace(name="second", provider="postgres"))
    assert store.calls == [(None, "Adhoc query", "SELECT 1", "duckdb")]
    assert set(store.rows[1]) == {"saved_query_id", "name", "sql_statement", "sql_dialect"}
    assert query.connection.name == "second"


def test_save_failure_keeps_the_edit_and_saved_identity_and_can_retry() -> None:
    store = SavedQueries()
    query = DatabaseQuery("SELECT 1")
    query.save(store)
    store.failure = DatabaseError("Save refused")
    query.update(statement="SELECT 2")
    with pytest.raises(DatabaseError, match="Save refused"):
        query.save(store)
    assert query.statement == "SELECT 2"
    assert query.projection()["saved_query_id"] == 1
    assert query.projection()["navigation"]["modified"] is True
    assert query.projection()["save_error"] == "Save refused"
    assert store.rows[1]["sql_statement"] == "SELECT 1"
    store.failure = None
    query.save(store)
    assert len(store.rows) == 1
    assert query.projection()["save_error"] is None
    assert query.projection()["navigation"]["modified"] is False


def test_failed_first_save_is_not_projected_as_a_saved_initial_statement() -> None:
    store = SavedQueries()
    store.failure = DatabaseError("Save refused")
    query = DatabaseQuery("SELECT 1")
    with pytest.raises(DatabaseError):
        query.save(store)
    assert query.projection()["saved_query_id"] is None
    assert query.projection()["navigation"]["modified"] is True


def test_clearing_a_never_saved_draft_leaves_no_pending_write_or_stale_error() -> None:
    store = SavedQueries()
    store.failure = DatabaseError("Save refused")
    query = DatabaseQuery("SELECT 1")
    with pytest.raises(DatabaseError):
        query.save(store)
    query.update(statement="")
    query.save(store)
    assert query.projection()["saved_query_id"] is None
    assert query.projection()["save_error"] is None
    assert query.projection()["navigation"]["modified"] is False


def test_reverting_to_the_persisted_statement_clears_a_prior_save_failure() -> None:
    store = SavedQueries()
    query = DatabaseQuery("SELECT 1")
    query.save(store)
    store.failure = DatabaseError("Save refused")
    query.update(statement="SELECT 2")
    with pytest.raises(DatabaseError):
        query.save(store)
    query.update(statement="SELECT 1")
    query.save(store)
    assert query.projection()["save_error"] is None
    assert query.projection()["navigation"]["modified"] is False


def test_source_retains_its_own_saved_identity_and_raw_template(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SavedQueries()
    monkeypatch.setattr(source_module, "_control_of", lambda ctx, purpose: store)
    monkeypatch.setattr(source_module, "shared_connection",
                        lambda ctx, name: SimpleNamespace(name=name, provider="duckdb"))
    source = Source({"statement": "SELECT * FROM '{file_path}'", "source-connection": "data",
                     "file": "/example/orders.csv"}, selected="database")
    source.observe_governed_context({"transform_query_id": 10})
    monkeypatch.setattr(source, "save_query", lambda ctx: pytest.fail("No transform-template write"))
    ctx = SimpleNamespace()
    assert source.save(ctx) == 1
    assert store.rows[1]["sql_statement"] == "SELECT * FROM '{file_path}'"
    source.update("statement", "SELECT count(*) FROM '{file_path}'")
    assert len(store.calls) == 1
    assert source.query_projection()["navigation"]["modified"] is True
    source.save(ctx)
    first, second = source.query(ctx), source.query(ctx)
    assert first is not second
    assert "saved_query_id" not in first.projection()
    assert source.query_projection()["saved_query_id"] == 1
    assert source.query_projection()["navigation"]["modified"] is False
    assert len(store.rows) == 1
    assert source.value("statement") == "SELECT count(*) FROM '{file_path}'"


def test_source_selection_and_edits_never_save_automatically(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SavedQueries()
    monkeypatch.setattr(source_module, "_control_of", lambda ctx, purpose: store)
    source = Source({"statement": "SELECT 1"}, selected="file")
    source.update("statement", "SELECT 2")
    assert store.calls == []
    assert source.save(SimpleNamespace()) is None
    assert store.calls == []
    source.select("database")
    assert store.calls == []
    source.save(SimpleNamespace())
    assert store.rows[1]["sql_statement"] == "SELECT 2"


def test_source_failed_save_keeps_its_edited_template_and_projects_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SavedQueries()
    monkeypatch.setattr(source_module, "_control_of", lambda ctx, purpose: store)
    source = Source({"statement": "SELECT 1"}, selected="database")
    source.save(SimpleNamespace())
    store.failure = DatabaseError("Save refused")
    source.update("statement", "SELECT 2")
    with pytest.raises(DatabaseError):
        source.save(SimpleNamespace())
    assert source.value("statement") == "SELECT 2"
    assert source.query_projection()["save_error"] == "Save refused"
    assert source.query_projection()["navigation"]["modified"] is True
    assert store.rows[1]["sql_statement"] == "SELECT 1"


def test_control_uses_its_existing_required_procedure_map_call_and_stored_result() -> None:
    control = Control.__new__(Control)
    calls = []
    stored = {"saved_query_id": 42, "sql_statement": "SELECT 1", "sql_dialect": "sql"}

    def call(action: str, values: dict, required: bool = False) -> dict:
        calls.append((action, values, required))
        return dict(stored)

    control._call = call
    assert control.save_query(None, "Query", "SELECT 1", "sql") == stored
    assert calls == [("save_query", {"saved_query_id": None, "name": "Query",
                                   "sql_statement": "SELECT 1", "sql_dialect": "sql"}, True)]


@pytest.mark.parametrize("row", [None, {},
    {"saved_query_id": 1, "sql_statement": "wrong", "sql_dialect": "sql"},
    {"saved_query_id": 2, "sql_statement": "SELECT 1", "sql_dialect": "sql"},
])
def test_control_refuses_a_missing_or_mismatched_stored_row(row: Any) -> None:
    control = Control.__new__(Control)
    control._call = lambda *args, **kwargs: row
    with pytest.raises(ConfigError, match="stored row"):
        control.save_query(1, "Query", "SELECT 1", "sql")


def test_saved_definition_opens_clean_and_edits_keep_its_identity_and_classification() -> None:
    store = SavedQueries()
    store.rows[42] = {
        "saved_query_id": 42, "saved_query_collection_id": 18,
        "name": "Customer query", "description": "Customers with orders",
        "sql_statement": "SELECT * FROM customers", "sql_dialect": "postgres",
        "installation_id": 7, "collection_name": "Reporting",
        "owner_login": "query_owner", "is_public": False,
    }
    query = DatabaseQuery.from_saved(store, 42)
    assert query.save(store) == 42  # an unchanged explicit Save is a no-op
    assert store.calls == []
    assert query.connection is None and query.params == {}
    shown = query.projection()
    assert shown["statement"] == store.rows[42]["sql_statement"]
    assert "result" not in shown
    assert shown["settings"] == {
        "name": "Customer query", "description": "Customers with orders",
        "sql_dialect": "postgres", "saved_query_collection_id": 18,
        "collection_name": "Reporting", "owner_login": "query_owner", "is_public": False,
    }
    assert shown["navigation"] == {"modified": False, "can_close": True, "label": "Customer query"}
    query.update(connection=SimpleNamespace(name="another", provider="duckdb"))
    assert store.calls == []
    query.update(statement="SELECT id FROM customers")
    assert store.calls == []
    assert query.projection()["navigation"]["modified"] is True
    query.save(store)
    assert store.calls == [(42, "Customer query", "SELECT id FROM customers", "postgres")]
    assert list(store.rows) == [42]
    assert store.rows[42]["saved_query_collection_id"] == 18
    assert query.projection()["settings"]["collection_name"] == "Reporting"


@pytest.mark.parametrize("identity", [0, -1, "42", True])
def test_saved_query_refuses_an_invalid_identity_before_reading(identity: Any) -> None:
    with pytest.raises(DatabaseError, match="positive saved_query_id"):
        DatabaseQuery.from_saved(object(), identity)


def test_inaccessible_saved_definition_is_not_replaced_with_a_blank_or_adhoc_query() -> None:
    store = SavedQueries()
    with pytest.raises(DatabaseError, match="unavailable"):
        DatabaseQuery.from_saved(store, 42)
    assert store.rows == {} and store.calls == []


def test_control_reads_by_id_through_its_existing_required_row_map() -> None:
    control = Control.__new__(Control)
    calls = []
    row = {"saved_query_id": 42}
    control._call_rows = lambda action, values, required: (
        calls.append((action, values, required)) or [row])
    assert control.get_saved_query(42) == row
    assert calls == [("get_saved_query", {"saved_query_id": 42}, True)]
    control._call_rows = lambda *a, **kw: []
    assert control.get_saved_query(42) is None

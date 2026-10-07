"""One schema, read into the database objects that describe it (backlog 684).

``DatabaseSchema.read`` is proven against a fake DBAdapter rather than a live
database, so what is asserted is the mapping: what the provider answered
becomes what the object carries. No connection is opened and no data is read --
every answer below is written by the test. Moved from the Console with the
reading itself; the Console keeps only its bridge.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db.database_objects import DatabaseSchema
from rey_lib.errors.error_utils import DatabaseError

SCHEMA_READ: dict[str, Any] = {
    "tables": [{
        "name": "run_log",
        "columns": [
            {"name": "run_id", "type": "BIGINT", "nullable": False, "default": None},
            {"name": "status", "type": "TEXT", "nullable": True, "default": "'pending'"},
        ],
        "primary_key": {"name": "run_log_pkey", "constrained_columns": ["run_id"]},
        "foreign_keys": [{
            "name": "run_log_batch_fkey", "constrained_columns": ["batch_id"],
            "referred_schema": "control", "referred_table": "batch",
            "referred_columns": ["batch_id"],
        }],
        "indexes": [{"name": "run_log_status_idx", "column_names": ["status"], "unique": False}],
        "unique_constraints": [{"name": "run_log_status_key", "column_names": ["status"]}],
    }],
    "views": [{"name": "run_vw", "definition": "SELECT 1"}],
}


#: The object types ``postgres_utils.get_object_ddl`` dispatches on. Plural, and
#: anything else raises there -- which is how an entire release of empty DDL
#: went unnoticed behind an adapter that answered whatever it was asked.
PROVIDER_VOCABULARY = frozenset({
    "schemas", "types", "tables", "views", "sequences",
    "functions", "procedures", "triggers", "indexes", "constraints",
})

ROUTINES = {
    "procedures": [{"name": "load_runs", "signature": "integer"}],
    "functions": [
        {"name": "rate", "signature": "text, date"},
        {"name": "rate", "signature": "integer"},
    ],
}


class _Adapter:
    """A DBAdapter that answers from the test's own script.

    It refuses vocabulary the real provider refuses, so a caller asking with
    the wrong word fails here instead of silently receiving nothing.
    """

    def __init__(
        self,
        read: dict[str, Any] | None = None,
        fail: bool = False,
        routines: dict[str, Any] | None = None,
        no_routines: bool = False,
    ) -> None:
        self._read = SCHEMA_READ if read is None else read
        self._fail = fail
        self._routines = ROUTINES if routines is None else routines
        self._no_routines = no_routines
        self.asked: list[tuple[str, str]] = []
        self.routine_requests: list[tuple[str, str, str]] = []

    def get_schema_metadata(self, conn: Any, schema: str) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError("the provider refused")
        return self._read

    # The connector primitives a view's own DDL is read through (backlog 695),
    # answered as PostgreSQL.
    def provider(self, _conn: Any) -> str:
        return "postgres"

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return f'"{name}"'

    def fetch_rows(self, _conn: Any, _sql_text: str, params: dict[str, Any]) -> list[dict]:
        self.asked.append(("view", str(params.get("view"))))
        return [{"definition": " SELECT run_id FROM control.run_log"}]

    def get_object_ddl(self, conn: Any, obj: dict[str, Any]) -> str:
        object_type = str(obj["object_type"])
        if object_type not in PROVIDER_VOCABULARY:
            raise RuntimeError(f"unsupported export object type '{object_type}'")
        self.asked.append((object_type, str(obj["name"])))
        return f"CREATE {object_type[:-1].upper()} {obj['schema']}.{obj['name']} ();"

    def get_columns(self, conn: Any, schema: str, name: str) -> list[dict[str, Any]]:
        return [{"name": "run_id", "type": "BIGINT", "nullable": False, "default": None}]

    def get_column_ddl(self, conn: Any, schema: str, table: str, column: str) -> str:
        return f'"{column}" BIGINT NOT NULL'

    def list_procedures(self, conn: Any, schema: str) -> list[dict[str, Any]]:
        if self._no_routines:
            raise RuntimeError("this provider answers about no routines")
        return list(self._routines.get("procedures") or [])

    def list_functions(self, conn: Any, schema: str) -> list[dict[str, Any]]:
        if self._no_routines:
            raise RuntimeError("this provider answers about no routines")
        return list(self._routines.get("functions") or [])

    def get_procedure_definition(self, conn: Any, identity: dict[str, Any]) -> str:
        return self._routine_source("procedure", identity)

    def get_function_definition(self, conn: Any, identity: dict[str, Any]) -> str:
        return self._routine_source("function", identity)

    def _routine_source(self, kind: str, identity: dict[str, Any]) -> str:
        if identity.get("object_type") != kind:
            raise RuntimeError("asked for the wrong routine kind")
        name = str(identity["name"])
        signature = str(identity.get("signature", ""))
        self.routine_requests.append((kind, name, signature))
        return f"CREATE {kind.upper()} {identity['schema']}.{name}({signature}) ..."


def _loaded(adapter: _Adapter, _monkeypatch: pytest.MonkeyPatch) -> Any:
    """The schema as read on a connection, through the given adapter."""
    return DatabaseSchema.read("handle", "rey_apps", "rey", "control", adapter)


def test_a_schema_answers_with_its_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """One table and one view, each constructed whole."""
    schema = _loaded(_Adapter(), monkeypatch)

    assert schema.connection == "rey_apps"
    assert schema.catalog == "rey"
    assert schema.schema == "control"
    assert [table.identity.name for table in schema.tables] == ["run_log"]
    assert [view.identity.name for view in schema.views] == ["run_vw"]


def test_a_table_carries_what_it_is_made_of(monkeypatch: pytest.MonkeyPatch) -> None:
    """Columns in reported order, and every structural answer mapped."""
    table = _loaded(_Adapter(), monkeypatch).tables[0]

    assert [(column.name, column.ordinal) for column in table.columns] == [
        ("run_id", 1), ("status", 2),
    ]
    assert table.columns[0].nullable is False
    assert table.columns[1].default == "'pending'"
    assert table.primary_key is not None
    assert table.primary_key.columns == ("run_id",)
    assert table.foreign_keys[0].referenced_table == "batch"
    assert table.foreign_keys[0].referenced_columns == ("batch_id",)
    assert table.indexes[0].columns == ("status",)
    assert table.constraints[0].kind == "unique"
    assert table.ddl == "CREATE TABLE control.run_log ();"


def test_a_check_constraint_is_built_beside_the_unique_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backlog 571: a check the read carries becomes a constraint of kind check."""
    read = {
        "tables": [{
            **SCHEMA_READ["tables"][0],
            "check_constraints": [{"name": "run_log_status_ck", "sqltext": "status <> ''"}],
        }],
        "views": [],
    }
    adapter = _Adapter(read=read)
    table = _loaded(adapter, monkeypatch).tables[0]

    assert [(one.name, one.kind) for one in table.constraints] == [
        ("run_log_status_key", "unique"), ("run_log_status_ck", "check"),
    ]
    assert table.constraints[1].ddl == "CREATE CONSTRAINT control.run_log_status_ck ();"
    assert ("constraints", "run_log_status_ck") in adapter.asked


def test_a_table_without_a_key_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty constraint answer is no key, not a key with no columns."""
    read = {"tables": [{"name": "t", "columns": [], "primary_key": {}}], "views": []}
    table = _loaded(_Adapter(read), monkeypatch).tables[0]

    assert table.primary_key is None


def test_a_view_is_asked_for_its_own_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bulk read describes a view by its definition; its columns are a second question."""
    view = _loaded(_Adapter(), monkeypatch).views[0]

    assert [column.name for column in view.columns] == ["run_id"]
    # The view's own DDL (backlog 695), not the provider's object DDL.
    assert view.ddl == (
        'CREATE OR REPLACE VIEW "control"."run_vw" AS\n SELECT run_id FROM control.run_log;'
    )


def test_the_statement_lists_the_columns_that_were_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """initial_select is written from the same read, one column per line."""
    schema = _loaded(_Adapter(), monkeypatch)

    assert schema.tables[0].initial_select == (
        "SELECT\n    run_id,\n    status\nFROM control.run_log;"
    )
    assert schema.views[0].initial_select == "SELECT\n    run_id\nFROM control.run_vw;"


def test_a_relation_with_no_columns_opens_on_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No projection to write is a working statement, not a broken one."""
    read = {"tables": [{"name": "t", "columns": []}], "views": []}

    assert _loaded(_Adapter(read), monkeypatch).tables[0].initial_select == (
        "SELECT * FROM control.t;"
    )


def test_ddl_is_asked_in_the_vocabulary_the_provider_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact tokens, not near-misses.

    The provider dispatches on plural object types and raises on anything
    else. A singular word here read as an empty definition for every object in
    the estate, and nothing noticed.
    """
    adapter = _Adapter()
    _loaded(adapter, monkeypatch)

    asked = dict.fromkeys(kind for kind, _ in adapter.asked if kind != "view")
    assert set(asked) <= PROVIDER_VOCABULARY
    assert ("tables", "run_log") in adapter.asked
    # A view writes its own DDL now: it is never asked of the provider.
    assert ("views", "run_vw") not in adapter.asked
    assert ("view", "run_vw") in adapter.asked
    assert ("constraints", "run_log_pkey") in adapter.asked
    assert ("constraints", "run_log_batch_fkey") in adapter.asked
    assert ("indexes", "run_log_status_idx") in adapter.asked


def test_a_supplied_definition_arrives_non_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the provider answers, the object carries it.

    The assertion the previous suite lacked: it proved the call happened, never
    that anything came back.
    """
    schema = _loaded(_Adapter(), monkeypatch)
    table = schema.tables[0]

    assert table.ddl == "CREATE TABLE control.run_log ();"
    assert table.columns[0].ddl == '"run_id" BIGINT NOT NULL'
    assert table.primary_key is not None and table.primary_key.ddl
    assert table.foreign_keys[0].ddl
    assert table.indexes[0].ddl
    assert table.constraints[0].ddl
    assert schema.views[0].ddl
    assert schema.procedures[0].ddl
    assert all(function.ddl for function in schema.functions)


def test_routines_are_carried_as_their_own_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Procedures and functions, beside tables and views."""
    schema = _loaded(_Adapter(), monkeypatch)

    assert [p.identity.name for p in schema.procedures] == ["load_runs"]
    assert [f.identity.name for f in schema.functions] == ["rate", "rate"]
    assert schema.procedures[0].identity.schema == "control"
    assert schema.procedures[0].identity.connection == "rey_apps"


def test_two_overloads_are_two_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """One name, two argument lists, two objects that do not collapse."""
    functions = _loaded(_Adapter(), monkeypatch).functions

    assert len(functions) == 2
    assert {f.identity.signature for f in functions} == {"text, date", "integer"}
    assert functions[0].identity != functions[1].identity
    assert functions[0].ddl != functions[1].ddl


def test_a_routine_taking_nothing_is_still_addressable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty signature is an identity, not a missing one."""
    routines = {"procedures": [{"name": "sweep", "signature": ""}], "functions": []}
    procedure = _loaded(_Adapter(routines=routines), monkeypatch).procedures[0]

    assert procedure.identity.signature == ""
    assert procedure.identity.name == "sweep"
    assert procedure.ddl


def test_the_signature_travels_with_the_definition_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader refuses without it, so it is not dropped on the way."""
    adapter = _Adapter()
    _loaded(adapter, monkeypatch)

    assert adapter.routine_requests == [
        ("procedure", "load_runs", "integer"),
        ("function", "rate", "text, date"),
        ("function", "rate", "integer"),
    ]


def test_a_provider_answering_about_no_routines_loses_no_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty collections, not a failed read.

    DuckDB answers about catalogs, schemas, tables and views only, and a schema
    it describes is still a schema.
    """
    schema = _loaded(_Adapter(no_routines=True), monkeypatch)

    assert schema.procedures == ()
    assert schema.functions == ()
    assert [table.identity.name for table in schema.tables] == ["run_log"]


def test_a_failed_read_raises_rather_than_half_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No partial schema escapes a failed read."""
    with pytest.raises(DatabaseError, match="could not be read"):
        _loaded(_Adapter(fail=True), monkeypatch)


def test_a_second_read_replaces_what_the_first_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload is a new snapshot, not a retained one.

    Proven by contents rather than by counting reads: the second answer drops a
    table and adds a column, and what comes back says so.
    """
    first = _loaded(_Adapter(), monkeypatch)
    changed = {
        "tables": [{
            "name": "run_log",
            "columns": [
                {"name": "run_id", "type": "BIGINT", "nullable": False, "default": None},
                {"name": "status", "type": "TEXT", "nullable": True, "default": None},
                {"name": "finished_at", "type": "TIMESTAMP", "nullable": True, "default": None},
            ],
        }],
        "views": [],
    }
    second = _loaded(_Adapter(changed), monkeypatch)

    assert [column.name for column in first.tables[0].columns] == ["run_id", "status"]
    assert [column.name for column in second.tables[0].columns] == [
        "run_id", "status", "finished_at",
    ]
    assert second.views == ()
    assert second.tables[0].initial_select.endswith("finished_at\nFROM control.run_log;")


def test_a_provider_invocation_is_carried_to_the_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the provider rendered arrives on the routine, unchanged."""
    routines = {
        "procedures": [
            {"name": "sweep", "signature": "", "invocation": "CALL control.sweep();"},
        ],
        "functions": [],
    }
    procedure = _loaded(_Adapter(routines=routines), monkeypatch).procedures[0]

    assert procedure.invocation == "CALL control.sweep();"


def test_the_reader_rewrites_nothing_about_the_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carried, not parsed: the reader does not know what a call looks like."""
    odd = 'select /*x*/ "Control"."F Rate"(NULL::numeric(10,2)) ;'
    routines = {
        "procedures": [],
        "functions": [{"name": "rate", "signature": "numeric", "invocation": odd}],
    }
    function = _loaded(_Adapter(routines=routines), monkeypatch).functions[0]

    assert function.invocation == odd


def test_a_provider_rendering_none_leaves_the_routine_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The absent key is an ordinary answer, and nothing fills it in."""
    schema = _loaded(_Adapter(), monkeypatch)

    assert all(routine.invocation == "" for routine in schema.functions)
    assert schema.procedures[0].invocation == ""
    # And the routine is still a whole object: only the call is missing.
    assert schema.procedures[0].ddl
    assert schema.procedures[0].identity.name == "load_runs"


#: Deliberately not in name order, and not in reverse order either -- the
#: provider's inspector walks relations however it likes, which is the order
#: the Explorer was listing 58 tables in.
SHUFFLED: dict[str, Any] = {
    "tables": [
        {"name": "symbol"},
        {"name": "edge", "columns": [
            {"name": "from_id", "type": "BIGINT", "nullable": False, "default": None},
            {"name": "to_reference", "type": "TEXT", "nullable": True, "default": None},
            {"name": "edge_kind", "type": "TEXT", "nullable": False, "default": None},
        ]},
        {"name": "repository"},
        {"name": "concept_stage"},
    ],
    "views": [
        {"name": "symbol_vw", "definition": "SELECT 1"},
        {"name": "code_calls_flat_vw", "definition": "SELECT 1"},
        {"name": "edge_vw", "definition": "SELECT 1"},
    ],
}


def test_relations_are_listed_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE POINT OF THE SORT.

    Asserted as the guarantee rather than against one expected list: what
    matters is that the order no longer depends on how the provider's inspector
    happened to walk the schema.
    """
    schema = _loaded(_Adapter(SHUFFLED), monkeypatch)

    tables = [table.identity.name for table in schema.tables]
    views = [view.identity.name for view in schema.views]

    assert tables == ["concept_stage", "edge", "repository", "symbol"]
    assert views == ["code_calls_flat_vw", "edge_vw", "symbol_vw"]
    assert tables == sorted(tables)
    assert views == sorted(views)


def test_mixed_case_orders_as_a_reader_expects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case-insensitive, because that is what alphabetical means to a person.

    A plain sort puts every capital first -- 'Banana, apple, cherry' -- which
    is codepoint order wearing an alphabet's clothes.
    """
    read = {
        "tables": [{"name": "cherry"}, {"name": "Banana"}, {"name": "apple"}],
        "views": [],
    }
    schema = _loaded(_Adapter(read), monkeypatch)

    assert [table.identity.name for table in schema.tables] == [
        "apple", "Banana", "cherry",
    ]


def test_two_names_differing_only_in_case_keep_a_settled_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A case-insensitive key alone would leave these two to the read order.

    They are equal under casefold, so without the tiebreak the provider decides
    -- and the same schema could list them either way on two reads.
    """
    first = _loaded(_Adapter({
        "tables": [{"name": "Trade"}, {"name": "trade"}], "views": [],
    }), monkeypatch)
    second = _loaded(_Adapter({
        "tables": [{"name": "trade"}, {"name": "Trade"}], "views": [],
    }), monkeypatch)

    assert [table.identity.name for table in first.tables] == \
        [table.identity.name for table in second.tables]


def test_a_sorted_table_keeps_its_columns_in_ordinal_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE REGRESSION THAT MATTERS MOST.

    Relations are reordered; what a relation is MADE OF is not. Column position
    is ordinal position and means something, and 'edge' is the case that would
    expose a sort reaching too far -- it moves from second in the read to second
    in the answer while its columns must not move at all.
    """
    schema = _loaded(_Adapter(SHUFFLED), monkeypatch)
    edge = next(table for table in schema.tables if table.identity.name == "edge")

    assert [column.name for column in edge.columns] == [
        "from_id", "to_reference", "edge_kind",
    ]
    assert [column.ordinal for column in edge.columns] == [1, 2, 3]


def test_routines_are_not_reordered_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """They arrive sorted from the adapter, and this adds no second rule.

    ROUTINES lists 'rate(text, date)' before 'rate(integer)' -- which is NOT
    signature order -- and it survives, because re-sorting routines here would
    be a rule free to disagree with DBAdapter._list_routines.
    """
    schema = _loaded(_Adapter(), monkeypatch)

    assert [function.identity.signature for function in schema.functions] == [
        "text, date", "integer",
    ]

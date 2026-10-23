"""DatabaseObjectListing: every inventory entry has exactly one canonical owner (backlog 723).

The provider's inventory is read once; each entry is claimed by exactly one
database-object type, which builds the object. An entry no type claims is
refused naming its kind -- never dropped -- and one several claim is refused
naming them. Each type's own ``list`` uses the same claim and build.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db import database_objects
from rey_lib.db.database_objects import (
    DatabaseConstraint,
    DatabaseForeignKey,
    DatabaseIndex,
    DatabaseObjectIdentity,
    DatabaseObjectListing,
    DatabaseObjectReference,
    DatabasePrimaryKey,
    DatabaseProcedure,
    DatabaseSchema,
    DatabaseTable,
)
from rey_lib.db import postgres_utils
from rey_lib.errors.error_utils import DatabaseError

_INVENTORY: list[dict[str, Any]] = [
    {"database": "rey", "object_type": "schema", "schema": "control", "name": "control"},
    {"database": "rey", "object_type": "table", "schema": "control", "name": "run",
     "dependencies": [{"object_type": "schema", "schema": "control", "name": "control"}]},
    {"database": "rey", "object_type": "constraint", "kind": "primary_key",
     "schema": "control", "name": "run_pk", "table": "run",
     "dependencies": ["tables:control.run"]},
    {"database": "rey", "object_type": "constraint", "kind": "foreign_key",
     "schema": "control", "name": "run_fk", "table": "run"},
    {"database": "rey", "object_type": "constraint", "kind": "check",
     "schema": "control", "name": "run_ck", "table": "run"},
    {"database": "rey", "object_type": "index", "schema": "control", "name": "run_ix",
     "table": "run"},
    {"database": "rey", "object_type": "procedure", "schema": "control", "name": "load",
     "signature": "integer"},
]


class _Adapter:
    """The inventory primitive and what a schema's DDL needs, counting inventory reads."""

    def __init__(self, inventory: Any) -> None:
        self.inventory = inventory
        self.reads = 0

    def provider(self, _conn: Any) -> str:
        return "postgres"

    def quote_identifier(self, _conn: Any, name: str) -> str:
        return postgres_utils.quote_identifier(name)

    def list_database_objects(self, _conn: Any, database: str | None = None) -> Any:
        self.reads += 1
        return self.inventory


def test_each_entry_becomes_the_object_of_its_one_owner() -> None:
    adapter = _Adapter(_INVENTORY)

    listing = DatabaseObjectListing.read(object(), "rey", None, adapter)

    assert [type(obj).__name__ for obj in listing.objects] == [
        "DatabaseSchema", "DatabaseTable", "DatabasePrimaryKey", "DatabaseForeignKey",
        "DatabaseConstraint", "DatabaseIndex", "DatabaseProcedure",
    ]
    assert (listing.provider, listing.database) == ("postgres", "rey")
    assert adapter.reads == 1  # read once, whatever the number of types


def test_the_constraint_partition_is_the_constraint_types_own() -> None:
    """No owner map: each type claims its own kinds, the same partition its DDL uses."""
    listing = DatabaseObjectListing.read(object(), "rey", None, _Adapter(_INVENTORY))
    constraints = [
        obj for obj in listing.objects
        if isinstance(obj, (DatabasePrimaryKey, DatabaseForeignKey, DatabaseConstraint))
    ]

    assert [(type(obj).__name__, obj.name) for obj in constraints] == [
        ("DatabasePrimaryKey", "run_pk"),
        ("DatabaseForeignKey", "run_fk"),
        ("DatabaseConstraint", "run_ck"),
    ]
    assert not hasattr(database_objects, "_CONSTRAINT_OWNER")


def test_an_unowned_kind_is_refused_naming_it_never_dropped() -> None:
    adapter = _Adapter([*_INVENTORY, {"object_type": "rule", "schema": "control", "name": "r"}])

    with pytest.raises(DatabaseError, match="No database object owns the DDL of a 'rule'"):
        DatabaseObjectListing.read(object(), "rey", None, adapter)


def test_an_entry_several_types_claim_is_refused_naming_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_objects, "_LISTED", (DatabaseTable, DatabaseTable))

    with pytest.raises(DatabaseError, match="claimed by DatabaseTable, DatabaseTable"):
        DatabaseObjectListing.read(object(), "rey", None, _Adapter(_INVENTORY[1:2]))


def test_a_malformed_inventory_is_refused() -> None:
    with pytest.raises(DatabaseError, match=r"must return list\[dict\]"):
        DatabaseObjectListing.read(object(), "rey", None, _Adapter("not a list"))
    with pytest.raises(DatabaseError, match="empty name"):
        DatabaseObjectListing.read(object(), "rey", None, _Adapter([{"object_type": "table"}]))


def test_references_keep_a_child_s_table_and_a_routine_s_signature() -> None:
    listing = DatabaseObjectListing.read(object(), "rey", None, _Adapter(_INVENTORY))
    by_name = {obj.reference.name: obj for obj in listing.objects}

    assert by_name["run_ix"].reference == DatabaseObjectReference(
        kind="indexes", schema="control", name="run_ix", table="run",
    )
    assert by_name["load"].reference == DatabaseObjectReference(
        kind="procedures", schema="control", name="load", signature="integer",
    )
    assert by_name["run"].depends_on == (
        DatabaseObjectReference(kind="schemas", schema="control", name="control"),
    )
    assert by_name["run_pk"].depends_on == (
        DatabaseObjectReference(kind="tables", schema="control", name="run"),
    )


def test_each_type_s_list_uses_the_same_claim_and_build() -> None:
    adapter = _Adapter(_INVENTORY)

    tables = DatabaseTable.list(object(), "rey", None, adapter)
    assert [obj.identity.name for obj in tables] == ["run"]
    keys = DatabasePrimaryKey.list(object(), "rey", None, adapter)
    assert [obj.name for obj in keys] == ["run_pk"]
    constraints = DatabaseConstraint.list(object(), "rey", None, adapter)
    assert [obj.name for obj in constraints] == ["run_ck"]
    (procedure,) = DatabaseProcedure.list(object(), "rey", None, adapter)
    assert procedure.identity.signature == "integer"


def test_the_database_is_the_inventory_s_then_the_caller_s_then_the_connection_s() -> None:
    nameless = [{"object_type": "schema", "schema": "s", "name": "s"}]

    asked = DatabaseObjectListing.read(object(), "rey", "asked", _Adapter(nameless))
    assert asked.database == "asked"

    class _Conn:
        database = "attached"

    attached = DatabaseObjectListing.read(_Conn(), "rey", None, _Adapter(nameless))
    assert attached.database == "attached"


def test_listed_objects_carry_their_whole_identity() -> None:
    """#1141: the governed connection the caller holds, and the catalog the listing resolves."""
    listing = DatabaseObjectListing.read(object(), "control_db", None, _Adapter(_INVENTORY))
    table = next(obj for obj in listing.objects if isinstance(obj, DatabaseTable))
    key = next(obj for obj in listing.objects if isinstance(obj, DatabasePrimaryKey))

    assert (table.identity.connection, table.identity.catalog) == ("control_db", "rey")
    assert (key.table.connection, key.table.catalog) == ("control_db", "rey")
    (procedure,) = DatabaseProcedure.list(object(), "control_db", None, _Adapter(_INVENTORY))
    assert (procedure.identity.connection, procedure.identity.catalog) == ("control_db", "rey")


def test_no_catalog_is_made_up_for_an_identity() -> None:
    """Where nothing names a catalog, an identity has none -- not the listing's fallback."""
    nameless = [{"object_type": "table", "schema": "s", "name": "t"}]

    listing = DatabaseObjectListing.read(object(), "control_db", None, _Adapter(nameless))

    (table,) = listing.objects
    assert table.identity.catalog == ""
    assert listing.database == "database"


class TestReadDdlIsOneContract:
    """Asked of the type with an address, or of a listed object for its own (M3)."""

    def test_a_listed_object_addresses_its_own_ddl(self) -> None:
        adapter = _Adapter(_INVENTORY)
        (schema,) = [
            obj for obj in DatabaseObjectListing.read(object(), "rey", None, adapter).objects
            if isinstance(obj, DatabaseSchema)
        ]

        assert schema.read_ddl(object(), adapter) == DatabaseSchema.read_ddl(
            object(), "control", adapter,
        ) == 'CREATE SCHEMA IF NOT EXISTS "control";'

    def test_the_type_is_still_asked_with_an_address(self) -> None:
        table = DatabaseObjectIdentity(connection="", catalog="", schema="s", name="t")

        class _Index(_Adapter):
            def index_column_catalog(self, *_a: Any, **_k: Any) -> dict:
                return {"unique": False, "primary": False, "columns": ["a"]}

            def provider(self, _conn: Any) -> str:
                return "mysql"

            def quote_identifier(self, _conn: Any, name: str) -> str:
                return f"`{name}`"

        assert DatabaseIndex.read_ddl(object(), table, "ix", _Index([])) == (
            "CREATE INDEX `ix` ON `s`.`t` (`a`);"
        )

    def test_a_child_cannot_exist_without_its_table(self) -> None:
        """#1141: the table is half of a child's identity, so it is always present."""
        with pytest.raises(TypeError, match="table"):
            DatabaseIndex(name="ix")  # type: ignore[call-arg]


def test_keys_and_other_constraints_carry_their_own_canonical_kinds() -> None:
    """One provider word, three canonical kinds (backlog 1102)."""
    listing = DatabaseObjectListing.read(object(), "rey", None, _Adapter(_INVENTORY))

    kinds = {type(obj).__name__: obj.reference.kind for obj in listing.objects}

    assert kinds["DatabasePrimaryKey"] == "primary_keys"
    assert kinds["DatabaseForeignKey"] == "foreign_keys"
    assert kinds["DatabaseConstraint"] == "constraints"


def test_every_kind_names_exactly_one_listed_type() -> None:
    """The kind is the type's own, and no two types share one (backlog 1102)."""
    listed = database_objects._LISTED

    assert len({owner.KIND for owner in listed}) == len(listed)
    for owner in listed:
        assert DatabaseObjectListing.object_type(owner.KIND) is owner


def test_a_kind_no_type_states_is_refused() -> None:
    with pytest.raises(DatabaseError, match="exactly one"):
        DatabaseObjectListing.object_type("query")

"""The database objects a schema read answers with, and what they serialize to.

These prove the objects alone: nothing here opens a connection, reads a schema
or renders SQL. What is asserted is the exact serialized shape, because a
projection can be JSON-safe while quietly dropping an identity or flattening a
nested object, and `json.dumps` alone would not notice.
"""

from __future__ import annotations

import json

from rey_lib.db.database_objects import (
    DatabaseColumn,
    DatabaseFunction,
    DatabaseConstraint,
    DatabaseForeignKey,
    DatabaseIndex,
    DatabaseObjectIdentity,
    DatabasePrimaryKey,
    DatabaseProcedure,
    DatabaseRoutineIdentity,
    DatabaseSchema,
    DatabaseTable,
    DatabaseView,
)

IDENTITY = DatabaseObjectIdentity(
    connection="rey_apps", catalog="rey", schema="control", name="run_log",
)


def _table() -> DatabaseTable:
    """One table carrying one of everything it can carry."""
    return DatabaseTable(
        identity=IDENTITY,
        columns=(
            DatabaseColumn(name="run_id", type="bigint", nullable=False,
                           default=None, ordinal=1, ddl='"run_id" bigint NOT NULL'),
            DatabaseColumn(name="status", type="text", nullable=True,
                           default="'pending'", ordinal=2,
                           ddl='"status" text DEFAULT \'pending\' NULL'),
        ),
        primary_key=DatabasePrimaryKey(
            name="run_log_pkey", columns=("run_id",),
            ddl="ALTER TABLE control.run_log ADD CONSTRAINT run_log_pkey PRIMARY KEY (run_id);",
        ),
        foreign_keys=(
            DatabaseForeignKey(
                name="run_log_batch_fkey", columns=("batch_id",),
                referenced_schema="control", referenced_table="batch",
                referenced_columns=("batch_id",),
                ddl="ALTER TABLE control.run_log ADD CONSTRAINT run_log_batch_fkey ...;",
            ),
        ),
        indexes=(DatabaseIndex(
            name="run_log_status_idx", columns=("status",), unique=False,
            ddl="CREATE INDEX run_log_status_idx ON control.run_log (status);",
        ),),
        constraints=(DatabaseConstraint(
            name="run_log_status_key", columns=("status",), kind="unique",
            ddl="ALTER TABLE control.run_log ADD CONSTRAINT run_log_status_key UNIQUE (status);",
        ),),
        ddl="CREATE TABLE control.run_log ();",
        initial_select="SELECT\n    run_id,\n    status\nFROM control.run_log;",
    )


def test_a_table_serializes_every_field_it_carries() -> None:
    """The whole object, nested and complete."""
    assert _table().to_dict() == {
        "identity": {
            "connection": "rey_apps", "catalog": "rey",
            "schema": "control", "name": "run_log",
        },
        "columns": [
            {"name": "run_id", "type": "bigint", "nullable": False,
             "default": None, "ordinal": 1, "ddl": '"run_id" bigint NOT NULL'},
            {"name": "status", "type": "text", "nullable": True,
             "default": "'pending'", "ordinal": 2,
             "ddl": '"status" text DEFAULT \'pending\' NULL'},
        ],
        "primary_key": {
            "name": "run_log_pkey", "columns": ["run_id"],
            "ddl": "ALTER TABLE control.run_log ADD CONSTRAINT run_log_pkey PRIMARY KEY (run_id);",
        },
        "foreign_keys": [{
            "name": "run_log_batch_fkey", "columns": ["batch_id"],
            "referenced_schema": "control", "referenced_table": "batch",
            "referenced_columns": ["batch_id"],
            "ddl": "ALTER TABLE control.run_log ADD CONSTRAINT run_log_batch_fkey ...;",
        }],
        "indexes": [{
            "name": "run_log_status_idx", "columns": ["status"], "unique": False,
            "ddl": "CREATE INDEX run_log_status_idx ON control.run_log (status);",
        }],
        "constraints": [{
            "name": "run_log_status_key", "columns": ["status"], "kind": "unique",
            "ddl": "ALTER TABLE control.run_log ADD CONSTRAINT run_log_status_key UNIQUE (status);",
        }],
        "ddl": "CREATE TABLE control.run_log ();",
        "initial_select": "SELECT\n    run_id,\n    status\nFROM control.run_log;",
    }


def test_identity_is_nested_rather_than_flattened() -> None:
    """A table has an identity; it is not one. The shape must say so."""
    serialized = _table().to_dict()

    assert serialized["identity"]["name"] == "run_log"
    for field in ("connection", "catalog", "schema", "name"):
        assert field not in serialized


def test_an_empty_table_keeps_its_collections() -> None:
    """Empty is a fact, and it is carried rather than dropped."""
    assert DatabaseTable(identity=IDENTITY).to_dict() == {
        "identity": IDENTITY.to_dict(),
        "columns": [],
        "primary_key": None,
        "foreign_keys": [],
        "indexes": [],
        "constraints": [],
        "ddl": "",
        "initial_select": "",
    }


def test_a_view_serializes_columns_a_definition_and_its_statement() -> None:
    """A view is columns and a definition, and carries nothing a view lacks."""
    view = DatabaseView(
        identity=DatabaseObjectIdentity(
            connection="rey_apps", catalog="rey", schema="control", name="run_vw",
        ),
        columns=(DatabaseColumn(name="run_id", type="bigint", nullable=False,
                                default=None, ordinal=1),),
        ddl="CREATE VIEW control.run_vw AS SELECT 1;",
        initial_select="SELECT\n    run_id\nFROM control.run_vw;",
    )

    assert view.to_dict() == {
        "identity": {"connection": "rey_apps", "catalog": "rey",
                     "schema": "control", "name": "run_vw"},
        "columns": [{"name": "run_id", "type": "bigint", "nullable": False,
                     "default": None, "ordinal": 1, "ddl": ""}],
        "ddl": "CREATE VIEW control.run_vw AS SELECT 1;",
        "initial_select": "SELECT\n    run_id\nFROM control.run_vw;",
    }


def test_a_schema_holds_its_objects_and_serializes_them_whole() -> None:
    """The unit a read answers with."""
    schema = DatabaseSchema(
        connection="rey_apps", catalog="rey", schema="control",
        tables=(_table(),), views=(),
    )
    serialized = schema.to_dict()

    assert serialized["connection"] == "rey_apps"
    assert serialized["catalog"] == "rey"
    assert serialized["schema"] == "control"
    assert serialized["views"] == []
    assert serialized["procedures"] == []
    assert serialized["functions"] == []
    assert serialized["tables"] == [_table().to_dict()]


def test_what_is_serialized_survives_json() -> None:
    """Whatever crosses a boundary later crosses as this, unchanged."""
    schema = DatabaseSchema(
        connection="rey_apps", catalog="rey", schema="control",
        tables=(_table(),),
        views=(DatabaseView(identity=IDENTITY),),
    )

    assert json.loads(json.dumps(schema.to_dict())) == schema.to_dict()


ROUTINE = DatabaseRoutineIdentity(
    connection="rey_apps", catalog="rey", schema="control",
    name="rate", signature="text, date",
)


def test_a_procedure_serializes_its_identity_and_its_source() -> None:
    """Identity nested, signature included, and the SQL that made it."""
    assert DatabaseProcedure(
        identity=ROUTINE, ddl="CREATE PROCEDURE control.rate(text, date) ...",
    ).to_dict() == {
        "identity": {
            "connection": "rey_apps", "catalog": "rey", "schema": "control",
            "name": "rate", "signature": "text, date",
        },
        "ddl": "CREATE PROCEDURE control.rate(text, date) ...",
    }


def test_a_function_serializes_the_same_way() -> None:
    assert DatabaseFunction(identity=ROUTINE).to_dict() == {
        "identity": ROUTINE.to_dict(),
        "ddl": "",
    }


def test_two_overloads_are_two_identities() -> None:
    """The signature is what tells them apart, so it is part of identity."""
    other = DatabaseRoutineIdentity(
        connection="rey_apps", catalog="rey", schema="control",
        name="rate", signature="integer",
    )

    assert ROUTINE != other
    assert ROUTINE.name == other.name
    assert {ROUTINE, other} == {ROUTINE, other}
    assert len({ROUTINE, other}) == 2


def test_a_schema_carries_all_four_collections() -> None:
    """Tables, views, procedures and functions, each present even when empty."""
    serialized = DatabaseSchema(
        connection="rey_apps", catalog="rey", schema="control",
        procedures=(DatabaseProcedure(identity=ROUTINE),),
    ).to_dict()

    assert serialized["tables"] == []
    assert serialized["views"] == []
    assert serialized["functions"] == []
    assert serialized["procedures"] == [DatabaseProcedure(identity=ROUTINE).to_dict()]

"""Database objects describe themselves (#1440): members, keys, parameters, lookup.

What the schema map re-derived from serialized dictionaries is each object's own
answer. No database is opened: the objects are values.
"""

from __future__ import annotations

from rey_lib.db.database_objects import (
    DatabaseColumn,
    DatabaseConstraint,
    DatabaseForeignKey,
    DatabaseFunction,
    DatabaseIndex,
    DatabaseObject,
    DatabaseObjectIdentity,
    DatabaseParameter,
    DatabasePrimaryKey,
    DatabaseProcedure,
    DatabaseRoutine,
    DatabaseRoutineIdentity,
    DatabaseSchema,
    DatabaseSequence,
    DatabaseTable,
    DatabaseTrigger,
    DatabaseView,
)
from rey_lib.inferred_architecture.object_map import ObjectMap

_TABLE = DatabaseObjectIdentity(connection="rey_apps", catalog="rey_apps", schema="control", name="run")


def _column(name: str, ordinal: int) -> DatabaseColumn:
    return DatabaseColumn(name=name, type="bigint", nullable=False, default=None, ordinal=ordinal)


def _parameter(name: str, ordinal: int, mode: str | None) -> DatabaseParameter:
    return DatabaseParameter(name=name, ordinal=ordinal, type="text", mode=mode, has_default=False)


def _routine_identity(name: str) -> DatabaseRoutineIdentity:
    return DatabaseRoutineIdentity(connection="rey_apps", catalog="rey_apps", schema="control",
                                   name=name, signature="")


def _table() -> DatabaseTable:
    return DatabaseTable(
        identity=_TABLE,
        columns=(_column("run_id", 1), _column("batch_id", 2), _column("run_key", 3),
                 _column("label", 4)),
        primary_key=DatabasePrimaryKey(name="run_pk", table=_TABLE, columns=("run_id",)),
        foreign_keys=(DatabaseForeignKey(name="run_batch_fk", table=_TABLE, columns=("batch_id",),
                                         referenced_schema="control", referenced_table="batch",
                                         referenced_columns=("batch_id",)),),
        indexes=(DatabaseIndex(name="run_key_ux", table=_TABLE, columns=("run_key",), unique=True),
                 DatabaseIndex(name="run_label_ix", table=_TABLE, columns=("label",), unique=False)),
        constraints=(DatabaseConstraint(name="run_label_uq", table=_TABLE, columns=("label",),
                                        kind=DatabaseConstraint.UNIQUE),
                     DatabaseConstraint(name="run_id_ck", table=_TABLE, columns=("run_id",),
                                        kind="check")),
    )


class TestWhatAnObjectContains:
    """MEMBERS names the field holding what an object contains -- never whether it shows."""

    def test_a_table_and_a_view_contain_their_columns(self) -> None:
        view = DatabaseView(identity=_TABLE, columns=(_column("run_id", 1),))
        assert DatabaseTable.MEMBERS == "columns" and DatabaseView.MEMBERS == "columns"
        assert getattr(_table(), _table().MEMBERS) == _table().columns
        assert getattr(view, view.MEMBERS) == view.columns

    def test_a_routine_contains_its_parameters(self) -> None:
        procedure = DatabaseProcedure(identity=_routine_identity("p_run"),
                                      parameters=(_parameter("in_run_id", 1, "IN"),))
        assert DatabaseFunction.MEMBERS == "parameters" and DatabaseProcedure.MEMBERS == "parameters"
        assert getattr(procedure, procedure.MEMBERS) == procedure.parameters

    def test_an_object_with_no_members_declares_none(self) -> None:
        for kind in (DatabaseObject, DatabaseSequence, DatabaseIndex, DatabaseTrigger, DatabaseSchema):
            assert kind.MEMBERS is None, kind.__name__


class TestARoutinesParameters:
    """DatabaseRoutine divides parameters by what they mean to a caller, IN/OUT/INOUT."""

    def test_functions_and_procedures_are_routines(self) -> None:
        assert issubclass(DatabaseFunction, DatabaseRoutine)
        assert issubclass(DatabaseProcedure, DatabaseRoutine)
        assert issubclass(DatabaseRoutine, DatabaseObject)
        assert not issubclass(DatabaseTable, DatabaseRoutine)

    def test_inputs_and_outputs_follow_the_mode_and_inout_is_both(self) -> None:
        parameters = (_parameter("a", 1, "IN"), _parameter("b", 2, "OUT"), _parameter("c", 3, "INOUT"),
                      _parameter("d", 4, "VARIADIC"), _parameter("e", 5, None), _parameter("f", 6, "TABLE"))
        for kind in (DatabaseFunction, DatabaseProcedure):
            routine = kind(identity=_routine_identity("r"), parameters=parameters)
            assert [one.name for one in routine.inputs] == ["a", "c", "d", "e"], kind.__name__
            assert [one.name for one in routine.outputs] == ["b", "c", "f"], kind.__name__

    def test_unreported_parameters_divide_into_nothing(self) -> None:
        routine = DatabaseFunction(identity=_routine_identity("f"))
        assert routine.parameters is None
        assert routine.inputs == () and routine.outputs == ()

    def test_positional_construction_is_unchanged(self) -> None:
        """No field was added above a routine's own: its first field is still its identity."""
        identity = _routine_identity("p")
        assert DatabaseProcedure(identity, "ddl").ddl == "ddl"
        assert DatabaseFunction(identity, "ddl").identity == identity


class TestATablesKeys:
    """The table owns which of its columns are keys (moved from ObjectMap._column_keys)."""

    def test_primary_foreign_and_unique_columns(self) -> None:
        assert _table().column_keys() == {
            "primary_key": frozenset({"run_id"}),
            "foreign_key": frozenset({"batch_id"}),
            # A unique index and a unique constraint; neither a plain index nor a check.
            "unique": frozenset({"run_key", "label"}),
        }

    def test_a_table_with_no_keys_answers_empty(self) -> None:
        assert DatabaseTable(identity=_TABLE).column_keys() == {
            "primary_key": frozenset(), "foreign_key": frozenset(), "unique": frozenset()}

    def test_the_same_answer_the_map_derives_from_the_tables_dictionary(self) -> None:
        """Parity: the moved behaviour answers as ObjectMap's copy does, before the map switches."""
        table = _table()
        derived = ObjectMap._column_keys(table.to_dict())
        assert {key: set(held) for key, held in table.column_keys().items()} == derived


class TestASchemasTables:
    """The schema owns the lookup of its tables by name (replacing ObjectMap._schema_row)."""

    def test_a_table_by_name_or_none(self) -> None:
        other = DatabaseTable(identity=DatabaseObjectIdentity(connection="rey_apps", catalog="rey_apps",
                                                              schema="control", name="batch"))
        schema = DatabaseSchema(connection="rey_apps", catalog="rey_apps", schema="control",
                                tables=(_table(), other))
        assert schema.table("batch") is other
        assert schema.table("run") is schema.tables[0]
        assert schema.table("missing") is None

"""A connection, as canonical node records -- moved with the feeder (backlog 1057).

The feeder reads and projects. It holds nothing, signs nothing, and names no
action of its own: a table arrives already declaring what clicking it means, and
that declaration is carried exactly as the object made it.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree import DATABASE_SOURCE, rey_lib_sources
from rey_lib.tree.database import DatabaseDataObject


def test_rey_lib_implements_the_database_source() -> None:
    """Backlog 1057: the database source crossed into rey_lib.tree."""
    assert isinstance(rey_lib_sources()[DATABASE_SOURCE], DatabaseDataObject)

CONNECTION = {
    "node_key": "7:10",
    "label": "Rey Apps",
    "tree_object_key": "database",
    "tree_configuration": {"connection": "rey_apps"},
}

SNAPSHOT = {
    "catalogs": [{"name": "rey_apps"}],
    "schemas": [
        {"name": "tree", "catalog": "rey_apps"},
        {"name": "control", "catalog": "rey_apps"},
        {"name": "elsewhere", "catalog": "another"},
    ],
}

SCHEMA_OBJECTS = {
    "tables": [
        {
            "identity": {"name": "tree_node"},
            "connection": "rey_apps",
            "schema_name": "tree",
            "name": "tree_node",
            "statement": "SELECT * FROM tree.tree_node",
            "ddl": 'CREATE TABLE "tree"."tree_node" (...);',
        },
        {"identity": {"name": "no_statement"}},
    ],
    "views": [{
        "identity": {"name": "tree_vw"},
        "action": "open_query",
        "reference": "tree.v1.signed-view",
    }],
    "procedures": [{
        "identity": {"name": "create_run_manifest", "signature": "a, b"},
        "connection": "rey_apps",
        "schema_name": "control",
        "name": "create_run_manifest",
        "ddl": "CREATE PROCEDURE control.create_run_manifest(...) AS $$ $$;",
    }],
    "functions": [],
}

#: The same schema holding no routines: the kinds are the canonical object
#: types, and a kind the schema read holds none of is not offered (backlog 720).
TABLES_AND_VIEWS = {**SCHEMA_OBJECTS, "procedures": []}


def scoped(**configuration: Any) -> dict[str, Any]:
    """One record standing at a level, as the projection would hold it."""
    return {
        "node_key": "1:1:1",
        "tree_object_key": "database",
        "tree_configuration": {"connection": "rey_apps", **configuration},
    }


def test_a_connection_answers_with_its_catalogs() -> None:
    made = DatabaseDataObject().children(object(), CONNECTION, SNAPSHOT)

    assert [record["label"] for record in made] == ["rey_apps"]
    assert made[0]["tree_object_key"] == "database"
    assert made[0]["tree_configuration"] == {"connection": "rey_apps", "catalog": "rey_apps"}


def test_a_catalog_answers_with_only_its_own_schemas() -> None:
    """The connection's answer said which schemas each catalog holds."""
    made = DatabaseDataObject().children(object(), scoped(catalog="rey_apps"), SNAPSHOT)

    assert [record["label"] for record in made] == ["tree", "control"]
    assert made[0]["tree_configuration"]["schema"] == "tree"


def test_a_schema_offers_only_the_kinds_it_holds() -> None:
    """The kinds are the canonical object types; which exist is the read's (720)."""
    made = DatabaseDataObject().children(
        object(), scoped(catalog="rey_apps", schema="tree"), TABLES_AND_VIEWS,
    )

    assert [record["label"] for record in made] == ["Tables", "Views"]
    assert made[0]["tree_configuration"]["collection"] == "tables"


class TestAHeadingAppearsOnlyWhereTheSchemaHoldsOne:
    """A kind is offered where the schema holds one -- and nowhere else (backlog 720).

    The schema's snapshot is what a heading's children are read from, so a
    heading over a kind the snapshot holds none of is a folder that opens on
    nothing -- on Postgres, a Procedures and a Functions under every schema.
    No provider capability takes part.
    """

    @staticmethod
    def _headings(state: dict[str, Any]) -> list[dict[str, Any]]:
        return DatabaseDataObject().children(
            object(), scoped(catalog="rey_apps", schema="tree"), state,
        )

    def test_a_schema_with_tables_and_no_routines_shows_no_routine_headings(self) -> None:
        state = {
            "tables": SCHEMA_OBJECTS["tables"],
            "views": [],
            "procedures": [],
            "functions": [],
        }

        assert [record["label"] for record in self._headings(state)] == ["Tables"]

    def test_a_schema_holding_routines_shows_their_headings(self) -> None:
        state = {
            "tables": [],
            "views": [],
            "procedures": SCHEMA_OBJECTS["procedures"],
            "functions": [{"identity": {"name": "f", "signature": ""}, "name": "f"}],
        }

        assert [record["label"] for record in self._headings(state)] == [
            "Procedures", "Functions",
        ]

    def test_the_state_carries_no_capability_model(self) -> None:
        """A procedure the read holds is offered: nothing beside the read decides."""
        assert [record["label"] for record in self._headings(SCHEMA_OBJECTS)] == [
            "Tables", "Views", "Procedures",
        ]

    def test_every_heading_shown_opens_on_at_least_one_object(self) -> None:
        state = SCHEMA_OBJECTS

        for heading in self._headings(state):
            assert DatabaseDataObject().children(object(), heading, state), heading["label"]


def test_an_object_carries_the_declaration_it_already_held() -> None:
    """Nothing is signed here, and no action is named here."""
    made = DatabaseDataObject().children(
        object(),
        scoped(catalog="rey_apps", schema="tree", collection="tables"),
        SCHEMA_OBJECTS,
    )

    assert made[0]["label"] == "tree_node"
    assert made[0]["connection"] == "rey_apps"
    assert made[0]["schema_name"] == "tree"
    assert made[0]["name"] == "tree_node"
    assert made[0]["statement"]
    assert "reference" not in made[0]
    assert "text_click" not in made[0]


def test_an_object_given_no_action_is_given_none_here() -> None:
    """A relation with no statement was declared nothing, and stays so."""
    made = DatabaseDataObject().children(
        object(),
        scoped(catalog="rey_apps", schema="tree", collection="tables"),
        SCHEMA_OBJECTS,
    )

    assert made[1]["label"] == "no_statement"
    # A relation with no statement is addressed by nothing, so nothing can be
    # built for it and the mapping has nothing to name.
    assert "statement" not in made[1]


def test_a_routine_reads_as_its_own_signature() -> None:
    made = DatabaseDataObject().children(
        object(),
        scoped(catalog="rey_apps", schema="tree", collection="procedures"),
        SCHEMA_OBJECTS,
    )

    assert made[0]["label"] == "create_run_manifest(a, b)"


def test_records_leave_without_identity() -> None:
    """Which key a created record carries is not this object's answer."""
    for record in DatabaseDataObject().children(object(), CONNECTION, SNAPSHOT):
        assert "node_key" not in record
        assert "parent_node_key" not in record


def test_state_is_named_per_connection_and_per_schema() -> None:
    """One snapshot serves a whole connection; one read serves one schema."""
    feeder = DatabaseDataObject()

    assert feeder.state_key(CONNECTION) == "database:rey_apps"
    assert feeder.state_key(scoped(catalog="rey_apps")) == "database:rey_apps"
    assert feeder.state_key(scoped(catalog="rey_apps", schema="tree")) == (
        "database:rey_apps:rey_apps:tree"
    )


def test_the_feeder_reads_but_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls read twice: surviving between calls is the projection's job."""
    reads: list[str] = []
    monkeypatch.setattr(
        "rey_lib.tree.database.connection_snapshot",
        lambda _ctx, connection: reads.append(connection) or SNAPSHOT,
    )
    feeder = DatabaseDataObject()

    feeder.state(object(), CONNECTION)
    feeder.state(object(), CONNECTION)

    assert reads == ["rey_apps", "rey_apps"]


def test_a_schema_s_state_is_its_read_and_nothing_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No connection snapshot is merged in for capabilities (backlog 720)."""
    monkeypatch.setattr(
        "rey_lib.tree.database.connection_snapshot",
        lambda *_a: pytest.fail("a schema's level reads no connection snapshot"),
    )
    monkeypatch.setattr(
        "rey_lib.tree.database.load_database_schema", lambda *_a: "read",
    )
    monkeypatch.setattr(
        "rey_lib.tree.database.schema_projection",
        lambda read: {"projected": read},
    )

    state = DatabaseDataObject().state(object(), scoped(catalog="rey_apps", schema="tree"))

    assert state == {"projected": "read"}


def test_a_feeder_does_not_decide_selectability() -> None:
    """It was decided here, and disagreed with every other heading in the tree.

    Which kinds take the selection is one answer, on the node type, proved
    where that lives.
    """
    made = DatabaseDataObject().children(object(), CONNECTION, SNAPSHOT)

    assert all("selectable" not in record for record in made)


def test_a_record_keeps_the_definition_its_object_already_held() -> None:
    """The DDL is read when the object is built, so the row carries it.

    Dropping it here would mean an action had to fetch a value the row was
    holding a moment earlier -- and that fetch is what this replaced.
    """
    made = DatabaseDataObject().children(
        object(),
        scoped(catalog="rey_apps", schema="tree", collection="tables"),
        SCHEMA_OBJECTS,
    )

    assert made[0]["ddl"] == 'CREATE TABLE "tree"."tree_node" (...);'


def test_a_routine_is_addressed_the_way_a_relation_is() -> None:
    """The same three fields, under the same names, and its own definition.

    A routine keeps its identity nested because a signature is part of it. What
    the tree reads is ordinary fields, so the ordinary ones are lifted -- which
    is what lets one action declaration serve a routine and a table alike.
    """
    made = DatabaseDataObject().children(
        object(),
        scoped(catalog="rey_apps", schema="control", collection="procedures"),
        SCHEMA_OBJECTS,
    )

    assert made[0]["connection"] == "rey_apps"
    assert made[0]["schema_name"] == "control"
    assert made[0]["name"] == "create_run_manifest"
    assert made[0]["ddl"].startswith("CREATE PROCEDURE")
    # And still no statement: opening a routine is a call, and none is declared.
    assert "statement" not in made[0]


class TestARootMayNarrowWhichCollectionsAreOffered:
    """`allowed_collections` restricts an existing answer.

    The schema read still decides which kinds EXIST. A root declaring
    this narrows that set to the ones it asked for -- which is what lets one
    surface show tables alone while the Explorer, declaring nothing, goes on
    showing everything it always did.
    """

    @staticmethod
    def _kinds(**configuration: Any) -> list[str]:
        return [
            record["label"] for record in DatabaseDataObject().children(
                object(),
                scoped(catalog="rey_apps", schema="tree", **configuration),
                TABLES_AND_VIEWS,
            )
        ]

    def test_absent_is_exactly_what_it_has_always_been(self) -> None:
        """THE CASE THAT MATTERS MOST. Every root that exists today carries no
        such key, so every tree in the estate must take the untouched path.
        """
        assert self._kinds() == ["Tables", "Views"]

    def test_a_restriction_narrows_to_what_it_names(self) -> None:
        assert self._kinds(allowed_collections=["tables"]) == ["Tables"]

    def test_it_cannot_admit_a_kind_the_schema_holds_none_of(self) -> None:
        """A RESTRICTION, NEVER A GRANT. `procedures` is a real collection and
        this schema holds none, so naming it admits nothing.
        """
        assert self._kinds(allowed_collections=["tables", "procedures"]) == ["Tables"]

    def test_naming_every_kind_is_the_same_as_naming_none(self) -> None:
        assert self._kinds(
            allowed_collections=["tables", "views", "procedures", "functions"],
        ) == ["Tables", "Views"]

    def test_an_empty_restriction_offers_nothing(self) -> None:
        """Distinct from absent: `[]` is a root saying "none of them", and a
        surface that meant "all of them" says nothing at all.
        """
        assert self._kinds(allowed_collections=[]) == []

    def test_a_kind_that_is_not_a_collection_is_refused(self) -> None:
        """Refused rather than skipped: a typo that silently removed a level
        would render a tree that looks finished and is missing part of the
        database.
        """
        with pytest.raises(ConfigError, match="tabels"):
            self._kinds(allowed_collections=["tabels"])

    def test_the_restriction_travels_to_the_levels_beneath_it(self) -> None:
        """Declared once on a root and inherited, like every other key."""
        made = DatabaseDataObject().children(
            object(), scoped(allowed_collections=["tables"]), SNAPSHOT,
        )

        assert made[0]["tree_configuration"]["allowed_collections"] == ["tables"]


class TestARootMayPresentObjectsDirectlyBeneathTheirSchema:
    """`objects_under_schema` is an alternate PRESENTATION of the same tree.

    Not a second tree and not a second path: the same object, the same records
    and the same addressing, with two levels that would each hold exactly one
    thing simply not drawn.

        default              objects_under_schema = true
        catalog              schema
          schema               object
            collection
              object

    `allowed_collections` says WHICH kinds are allowed; this says WHETHER those
    objects appear directly beneath each schema.
    """

    FLAT = {"allowed_collections": ["tables"], "objects_under_schema": True}

    def test_a_connection_answers_with_schemas_rather_than_catalogs(self) -> None:
        """The catalog level is REMOVED, not summarized."""
        made = DatabaseDataObject().children(object(), scoped(**self.FLAT), SNAPSHOT)

        assert [record["label"] for record in made] == ["tree", "control", "elsewhere"]

    def test_each_schema_still_carries_the_catalog_it_belongs_to(self) -> None:
        """THE CLAIM THIS SHAPE RESTS ON: what is drawn changed, what is
        ADDRESSED did not. With no catalog node there is nothing to inherit
        from, so the catalog comes from the schema entry.
        """
        made = DatabaseDataObject().children(object(), scoped(**self.FLAT), SNAPSHOT)
        carried = {record["label"]: record["tree_configuration"]["catalog"] for record in made}

        assert carried == {"tree": "rey_apps", "control": "rey_apps", "elsewhere": "another"}

    def test_a_schema_answers_with_objects_rather_than_collection_headings(self) -> None:
        made = DatabaseDataObject().children(
            object(), scoped(catalog="rey_apps", schema="tree", **self.FLAT), SCHEMA_OBJECTS,
        )

        assert [record["label"] for record in made] == ["tree_node", "no_statement"]

    def test_an_object_is_addressed_exactly_as_the_drawn_shape_addresses_it(self) -> None:
        """Identity preservation, asserted directly: the record reached without
        a collection heading is the record reached through one.
        """
        through_heading = DatabaseDataObject().children(
            object(),
            scoped(catalog="rey_apps", schema="tree", collection="tables"),
            SCHEMA_OBJECTS,
        )
        beneath_schema = DatabaseDataObject().children(
            object(), scoped(catalog="rey_apps", schema="tree", **self.FLAT), SCHEMA_OBJECTS,
        )

        assert [one["tree_configuration"] for one in beneath_schema] \
            == [one["tree_configuration"] for one in through_heading]

    def test_it_cannot_admit_a_kind_the_schema_holds_none_of(self) -> None:
        """Nothing is drawn -- and it is not an error, exactly as a heading is
        simply absent.
        """
        made = DatabaseDataObject().children(
            object(),
            scoped(
                catalog="rey_apps", schema="tree",
                allowed_collections=["procedures"], objects_under_schema=True,
            ),
            TABLES_AND_VIEWS,
        )

        assert made == []

    def test_several_allowed_collections_are_refused(self) -> None:
        """Over two kinds a table and a view of the same name would sit side by
        side with nothing telling them apart. Refused rather than resolved:
        choosing one would be this object deciding what configuration did not.
        """
        with pytest.raises(ConfigError, match="exactly one"):
            DatabaseDataObject().children(
                object(),
                scoped(allowed_collections=["tables", "views"], objects_under_schema=True),
                SNAPSHOT,
            )

    def test_naming_no_allowed_collection_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="exactly one"):
            DatabaseDataObject().children(
                object(), scoped(objects_under_schema=True), SNAPSHOT,
            )

    def test_the_shape_travels_to_the_levels_beneath_it(self) -> None:
        """Declared once on a root and inherited, like every other key -- which
        is what lets the schema level know it too.
        """
        made = DatabaseDataObject().children(object(), scoped(**self.FLAT), SNAPSHOT)

        assert made[0]["tree_configuration"]["objects_under_schema"] is True


def test_a_catalog_parent_produces_the_configuration_it_always_did() -> None:
    """The regression guard for the catalog-from-entry change.

    `_schemas` now takes the catalog from the schema entry rather than
    inheriting it. Under a catalog node the two agree -- the filter matched on
    it -- so what this path produces must be unchanged, byte for byte.
    """
    made = DatabaseDataObject().children(object(), scoped(catalog="rey_apps"), SNAPSHOT)

    assert [record["tree_configuration"] for record in made] == [
        {"connection": "rey_apps", "catalog": "rey_apps", "schema": "tree"},
        {"connection": "rey_apps", "catalog": "rey_apps", "schema": "control"},
    ]


class TestAnObjectOpensIntoWhatItIsMadeOf:
    """Backlog 572: structural collections beneath an object, from its schema's read."""

    TABLE = {
        "identity": {"name": "tree_node"},
        "columns": [
            {"name": "label", "type": "TEXT", "ordinal": 2},
            {"name": "tree_node_id", "type": "BIGINT", "ordinal": 1},
        ],
        "primary_key": {"name": "tree_node_pkey", "columns": ["tree_node_id"]},
        "foreign_keys": [{
            "name": "tree_node_root_fkey", "columns": ["tree_root_id"],
            "referenced_schema": "tree", "referenced_table": "tree_root",
            "referenced_columns": ["tree_root_id"],
        }],
        "indexes": [
            {"name": "tree_node_label_idx", "columns": ["label"], "unique": False},
            {"name": "Tree_Node_Parent_Idx", "columns": ["parent_id"], "unique": True},
        ],
        "constraints": [
            {"name": "tree_node_label_key", "columns": ["label"], "kind": "unique"},
            {"name": "tree_node_label_ck", "columns": [], "kind": "check"},
        ],
    }
    BARE = {"identity": {"name": "bare"}, "columns": [], "primary_key": None,
            "foreign_keys": [], "indexes": [], "constraints": []}
    VIEW = {"identity": {"name": "tree_vw"},
            "columns": [{"name": "node_key", "type": "TEXT", "ordinal": 1}]}
    STATE = {"tables": [TABLE, BARE], "views": [VIEW], "procedures": [], "functions": []}

    @pytest.fixture(autouse=True)
    def _no_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The dependency readings, answered without the index."""
        monkeypatch.setattr(DatabaseDataObject, "_indexed", lambda *_a: True)
        monkeypatch.setattr(DatabaseDataObject, "_related_rows", lambda *_a: [])

    def _at(self, object_type: str, name: str, **more: Any) -> dict[str, Any]:
        return scoped(catalog="rey_apps", schema="tree", object=name,
                      object_type=object_type, signature="", **more)

    def _labels(self, record: dict[str, Any]) -> list[str]:
        made = DatabaseDataObject().children(object(), record, self.STATE)
        return [one["label"] for one in made]

    def test_a_table_lists_its_structure_then_its_dependency_level_unchanged(self) -> None:
        feeder = DatabaseDataObject()
        record = self._at("table", "tree_node")
        made = feeder.children(object(), record, self.STATE)

        assert [one["label"] for one in made[:3]] == ["Columns", "Indexes", "Constraints"]
        assert made[3:] == feeder._relationships(object(), record["tree_configuration"])
        assert all(one["node_type"] == "structure_group" for one in made[:3])

    def test_a_view_lists_only_its_columns(self) -> None:
        assert self._labels(self._at("view", "tree_vw"))[:1] == ["Columns"]
        assert "Indexes" not in self._labels(self._at("view", "tree_vw"))

    def test_an_object_with_no_structure_shows_no_empty_heading(self) -> None:
        labels = self._labels(self._at("table", "bare"))
        assert not {"Columns", "Indexes", "Constraints"} & set(labels)

    def test_columns_keep_their_ordinal_position(self) -> None:
        labels = self._labels(self._at("table", "tree_node", structure="columns"))
        assert labels == ["tree_node_id  BIGINT", "label  TEXT"]

    def test_indexes_are_listed_by_name_ignoring_case(self) -> None:
        labels = self._labels(self._at("table", "tree_node", structure="indexes"))
        # A raw sort would put the capitalised name first.
        assert labels == ["tree_node_label_idx (label)", "Tree_Node_Parent_Idx (parent_id) unique"]

    def test_constraints_open_into_their_groups_in_stated_order(self) -> None:
        labels = self._labels(self._at("table", "tree_node", structure="constraints"))
        assert labels == ["Primary Keys", "Foreign Keys", "Unique", "Checks"]

    def test_each_constraint_group_lists_its_entries(self) -> None:
        def group(name: str) -> list[str]:
            return self._labels(self._at("table", "tree_node", structure=name))

        assert group("primary_key") == ["tree_node_pkey (tree_node_id)"]
        assert group("foreign_keys") == [
            "tree_node_root_fkey (tree_root_id) → tree.tree_root(tree_root_id)"
        ]
        assert group("unique") == ["tree_node_label_key (label)"]
        assert group("checks") == ["tree_node_label_ck"]

    def test_an_entry_is_shown_and_nothing_more(self) -> None:
        made = DatabaseDataObject().children(
            object(), self._at("table", "tree_node", structure="columns"), self.STATE,
        )
        for one in made:
            assert one["has_children"] is False
            assert one["selectable"] is False
            assert one["tree_object_key"] is None
            assert one["tree_configuration"] == {}

    def test_object_and_structure_levels_name_their_schemas_state(self) -> None:
        feeder = DatabaseDataObject()
        expected = "database:rey_apps:rey_apps:tree"
        assert feeder.state_key(self._at("table", "tree_node")) == expected
        assert feeder.state_key(self._at("table", "tree_node", structure="columns")) == expected

    def test_a_dependency_level_still_names_no_state(self) -> None:
        record = self._at("table", "tree_node", relationship="depends")
        assert DatabaseDataObject().state_key(record) is None


class TestTheCodeIndexIsAskedThroughItsOwner:
    """What the index knows is IndexedDatabaseObject's (backlog 696, 729).

    The feeder builds the object's identity -- signature always carried -- and
    reads the owner's records by field. It runs no code-index SQL and holds no
    DBAdapter.
    """

    AT = {"catalog": "rey_apps", "schema": "control", "object": "end_step",
          "object_type": "procedure", "signature": "integer, text"}

    @pytest.fixture
    def asked(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        """The owner's three reads, recording the identity and what was asked."""
        from rey_lib.repository_map.indexed_database_object import (
            IndexedCodeReach,
            IndexedDatabaseObject,
            IndexedRelation,
        )

        calls: list[tuple] = []
        handle = object()
        monkeypatch.setattr(
            "rey_lib.tree.database.shared_connection",
            lambda _ctx, _name: type("_Shared", (), {"handle": lambda _self: handle})(),
        )
        monkeypatch.setattr(IndexedDatabaseObject, "is_indexed",
                            lambda obj, conn: calls.append(("indexed", obj, conn)) or True)
        monkeypatch.setattr(
            IndexedDatabaseObject, "related",
            lambda obj, conn, *, reverse: calls.append(("related", obj, reverse)) or (
                IndexedRelation(schema="control", name="batch_step", object_type="table",
                                signature="", definition="", reference_kind="writes"),
            ),
        )
        monkeypatch.setattr(
            IndexedDatabaseObject, "reached_by_code",
            lambda obj, conn: calls.append(("code", obj, conn)) or (
                IndexedCodeReach(installation="local", map_name="control",
                                 repository_key="rey_lib", relative_path="rey_lib/control.py",
                                 qualified_name="Control.end", binding_name="end_step"),
                IndexedCodeReach(installation="local", map_name="loader",
                                 repository_key="rey_loader", relative_path="main.py",
                                 qualified_name="run", binding_name="end_step"),
            ),
        )
        return calls

    def test_each_read_asks_with_the_whole_identity(self, asked: list[tuple]) -> None:
        feeder = DatabaseDataObject()

        assert feeder._indexed(object(), self.AT) is True
        feeder._related_rows(object(), self.AT, True)
        feeder._bridge_rows(object(), self.AT)

        identities = {(o.schema, o.name, o.object_type, o.signature) for _k, o, _x in asked}
        assert identities == {("control", "end_step", "procedure", "integer, text")}
        assert [kind for kind, _o, _x in asked] == ["indexed", "related", "code"]

    def test_a_related_object_is_drawn_from_the_relation_s_fields(
        self, asked: list[tuple], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Found in the database: its record. Not found: the stale-index status."""
        monkeypatch.setattr("rey_lib.tree.database.load_database_schema",
                            lambda *_a: "read")
        monkeypatch.setattr("rey_lib.tree.database.schema_projection",
                            lambda _read: {"tables": [{"identity": {"name": "batch_step"},
                                                       "name": "batch_step"}]})

        (made,) = DatabaseDataObject().children(
            object(), scoped(**self.AT, relationship="depends", results="database"),
        )
        assert made["label"].endswith("  (writes)")

        monkeypatch.setattr("rey_lib.tree.database.schema_projection",
                            lambda _read: {"tables": []})
        (stale,) = DatabaseDataObject().children(
            object(), scoped(**self.AT, relationship="depends", results="database"),
        )
        assert stale["label"] == "control.batch_step is in the index and not in the database"

    def test_the_code_levels_are_drawn_from_the_reach_s_fields(
        self, asked: list[tuple],
    ) -> None:
        def level(**more: Any) -> list[dict[str, Any]]:
            return DatabaseDataObject().children(
                object(), scoped(**self.AT, relationship="used", results="code", **more),
            )

        assert [r["label"] for r in level()] == ["Mappings"]
        assert [r["label"] for r in level(bridge="mappings")] == ["local"]
        assert [r["label"] for r in level(bridge="mappings", bridge_installation="local")] == [
            "control", "loader",
        ]
        (code,) = level(bridge="mappings", bridge_installation="local", bridge_map="control")
        assert code["label"] == "Control.end  (end_step)"
        assert code["relative_path"] == "rey_lib/rey_lib/control.py"


def test_the_feeder_runs_no_code_index_sql_and_holds_no_dbadapter() -> None:
    """Console -> IndexedDatabaseObject -> DBAdapter: never Console -> DBAdapter (729)."""
    from pathlib import Path

    from rey_lib.tree import database as database_data_object

    source = Path(database_data_object.__file__).read_text(encoding="utf-8")

    assert "DBAdapter" not in source
    assert "code.db_" not in source
    assert "IndexedDatabaseObject" in source

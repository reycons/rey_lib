"""A drawn column's type is its catalog column's where the map holds its table (#1454).

The lineage fixture's index read states every column as ``text``; the schema read
states the catalog's types. A table in the map's schema is drawn with its catalog
column's type; a view -- no catalog object held here -- keeps the index's. The
row's ordinal is always the index's attribute number: lineage is ordered by it.
"""
from __future__ import annotations

from tests import test_object_map_column_lineage as lineage


def _expanded() -> dict:
    held, _ = lineage._map()
    # On books' screen books opens by name; its neighbours by their node: trades db:1, exposure db:3.
    held.focus(table="books", opened=["books", "db:1", "db:3"])
    return {n["id"]: n for n in held.projection()["nodes"]}


def test_a_held_tables_column_is_drawn_with_its_catalog_type() -> None:
    nodes = _expanded()
    # trades.amount: catalog numeric, index text.
    assert (nodes["db:13"]["data_type"], nodes["db:13"]["type_category"]) == ("numeric", "number")
    # books.book_id: catalog bigint, index text.
    assert nodes["db:21"]["data_type"] == "bigint"


def test_a_column_with_no_catalog_object_keeps_the_index_type() -> None:
    # exposure is a view: the map holds no catalog object for it.
    assert _expanded()["db:51"]["data_type"] == "text"


def test_the_ordinal_stays_the_index_attribute_number() -> None:
    nodes = _expanded()
    assert [nodes[f"db:{n}"]["ordinal"] for n in (11, 12, 13, 14)] == [1, 2, 3, 4]

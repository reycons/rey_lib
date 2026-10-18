"""A configured collection, as canonical node records -- moved with the feeder (backlog 1059).

The feeder reads and projects. It holds nothing, signs nothing, and names no
action: every member arrives already declaring what clicking it means, and those
declarations are carried exactly as configuration's projection made them.

What it does not do is know which collection it is. The record's configuration
names one, the same way a folder record names a path and a database record names
a connection, and a name this object does not implement is refused.

Its two projections are projections only: applications are the objects the
configuration load built, tools are ToolRegistry's interpretation of the loaded
``ctx.tools`` (backlog 1093).
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config.applications import build_applications
from rey_lib.errors.error_utils import ConfigError
from rey_lib.tools import ToolRegistry
from rey_lib.tree import collection
from rey_lib.tree.collection import CollectionDataObject

#: What ``configured_applications`` answers with: an id, a label, its own mark
#: and the identity it is addressed by. No action -- the node type names it.
APPLICATIONS = [
    {"id": "rey_console", "label": "Rey Console", "icon": "app.rey_console",
     "application_id": "rey_console"},
    {"id": "file_operator", "label": "File Operator", "icon": "",
     "application_id": "file_operator"},
]

TOOLS = [{"id": "rebuild_index", "label": "Rebuild Index"}]

FEEDER = Path(collection.__file__)


def at(collection_name: str, **configuration: Any) -> dict[str, Any]:
    """One record standing at a level, as the projection would hold it."""
    return {
        "node_key": "5:5:1",
        "tree_object_key": collection_name,
        "tree_configuration": {"collection": collection_name, **configuration},
    }


APPLICATION_ROOT = at("applications")
TOOL_ROOT = at("tools")


# -- what the configuration names -------------------------------------------


def test_the_configuration_says_which_collection_to_read() -> None:
    """The point of the object: it is told, it does not know."""
    feeder = CollectionDataObject()

    assert feeder.state_key(APPLICATION_ROOT) == "collection:applications"
    assert feeder.state_key(TOOL_ROOT) == "collection:tools"


@pytest.mark.parametrize("named", ["feeds", "workflows", "pipelines", "steps"])
def test_a_collection_nobody_implemented_is_refused(named: str) -> None:
    """Not a switchboard. A row naming anything at all would hide a fault."""
    with pytest.raises(ConfigError, match=named):
        CollectionDataObject().children(object(), at(named), [])


def test_a_record_naming_no_collection_is_refused() -> None:
    """An empty configuration is the old assumption, and is now a fault."""
    with pytest.raises(ConfigError):
        CollectionDataObject().children(
            object(), {"tree_configuration": {}}, APPLICATIONS,
        )


# -- the members -------------------------------------------------------------


def test_applications_are_listed_by_label_ignoring_case() -> None:
    """Backlog 569: configuration order is not a declared order."""
    unsorted = [
        {"id": "r", "label": "rey Loader"},
        {"id": "f", "label": "File Operator"},
    ]

    made = CollectionDataObject().children(object(), APPLICATION_ROOT, unsorted)

    assert [record["label"] for record in made] == ["File Operator", "rey Loader"]


def test_a_member_opens_into_nothing() -> None:
    """A triangle over nothing is worse than no triangle."""
    for record in CollectionDataObject().children(object(), APPLICATION_ROOT, APPLICATIONS):
        assert record["has_children"] is False
        assert record["tree_object_key"] is None
        assert record["tree_configuration"] == {}


def test_an_application_draws_its_own_mark_or_its_collection_s() -> None:
    made = CollectionDataObject().children(object(), APPLICATION_ROOT, APPLICATIONS)

    assert [record["icon"] for record in made] == ["application", "app.rey_console"]


def test_a_feeder_carries_the_identity_and_names_no_action() -> None:
    """What a node *is* is the feeder's; what its reference is for is not."""
    made = CollectionDataObject().children(object(), APPLICATION_ROOT, APPLICATIONS)

    assert made[0]["node_type"] == "application"
    assert made[0]["application_id"] == "file_operator"
    assert "reference" not in made[0]
    assert "text_click" not in made[0]
    assert "buttons" not in made[0]


def test_records_leave_without_identity() -> None:
    for record in CollectionDataObject().children(object(), APPLICATION_ROOT, APPLICATIONS):
        assert "node_key" not in record
        assert "parent_node_key" not in record


def test_the_feeder_reads_but_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls read twice: surviving between calls is the projection's job."""
    reads: list[int] = []
    monkeypatch.setitem(
        collection._COLLECTIONS,
        "applications",
        lambda _ctx: reads.append(1) or APPLICATIONS,
    )
    feeder = CollectionDataObject()

    feeder.state(object(), APPLICATION_ROOT)
    feeder.state(object(), APPLICATION_ROOT)

    assert len(reads) == 2


# -- tools -------------------------------------------------------------------


def test_tools_are_one_more_collection_and_not_one_more_object() -> None:
    """Same object, same two methods, told a different name to read."""
    made = CollectionDataObject().children(object(), TOOL_ROOT, TOOLS)

    assert [record["label"] for record in made] == ["Rebuild Index"]
    # It declared nothing, so it neither opens nor acts.
    assert made[0]["has_children"] is False
    assert made[0]["node_type"] == "tool"
    assert "application_id" not in made[0]


# -- the projections (moved from rey_console tests/test_applications_are_objects.py) --


def _published(declared: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """What the installed distributions publish for these declarations."""
    return {
        str(entry["name"]): {
            "name": str(entry["name"]),
            "entry_point": entry.get("entry_point", "main.py"),
            "cli": entry.get("cli") or {},
        }
        for entry in declared
    }


def _application(name: str) -> Any:
    declared = [{"name": name, "app_path": f"/apps/{name}", "entry_point": "main.py",
                 "cli": {}}]
    return build_applications(
        SimpleNamespace(apps=declared), registrations=_published(declared)
    )[0]


class TestTheTreeProjects:
    """It projects the objects, and normalizes nothing."""

    def test_applications_come_from_the_built_objects(self) -> None:
        rows = collection.configured_applications(SimpleNamespace(
            applications=(_application("alpha"), _application("beta")),
        ))

        assert [row["id"] for row in rows] == ["alpha", "beta"]

    def test_a_context_without_applications_is_a_fault(self) -> None:
        # Not an empty installation: applications are built by the config load,
        # so a context without them was not built by it.
        with pytest.raises(ConfigError):
            collection.configured_applications(SimpleNamespace())

    def test_tools_are_the_registry_s_enabled_tools(self) -> None:
        loaded = {"editors": [
            {"name": "vscode", "label": "VS Code"},
            {"name": "vim", "enabled": False},
            {"name": "nano"},
        ]}

        rows = collection.configured_tools(SimpleNamespace(tools=loaded))

        assert rows == [{"id": "vscode", "label": "VS Code"},
                        {"id": "nano", "label": "nano"}]


# -- delegation (moved from rey_console tests/test_reader_delegation.py) ------


@pytest.mark.parametrize(
    ("projection", "owner"),
    [
        # Applications are read off the objects the config load built, not
        # from a registry reader that normalizes a declaration again.
        ("configured_applications", "_ctx_applications"),
    ],
)
def test_the_projection_asks_the_owner(
    projection: str, owner: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[Any] = []
    monkeypatch.setattr(collection, owner, lambda given: asked.append(given) or [])

    getattr(collection, projection)(SimpleNamespace())

    assert len(asked) == 1, f"{projection} did not call {owner}"


def test_tools_are_asked_of_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loaded block goes to ToolRegistry exactly as the context holds it."""
    asked: list[Any] = []
    loaded = object()
    monkeypatch.setattr(
        ToolRegistry, "from_configuration",
        classmethod(lambda cls, tools: asked.append(tools) or cls([])),
    )

    collection.configured_tools(SimpleNamespace(tools=loaded))

    assert asked == [loaded]


# -- structural --------------------------------------------------------------


def test_the_projection_reads_and_interprets_no_configuration() -> None:
    """Configuration I/O loads; ToolRegistry interprets; this only projects.

    Read from the code, not the prose: the provenance paragraph names the
    Console module this came from.
    """
    tree = ast.parse(FEEDER.read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    called = {
        (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    assert not [name for name in imported if name.startswith("rey_console")]
    for absent in ("list_tools", "list_apps", "list_app_registry", "to_plain_data",
                   "parse_yaml", "load_tool_registry"):
        assert absent not in called, absent

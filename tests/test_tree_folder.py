"""The folder source, moved from the Console into rey_lib.tree (backlog 1056).

Two halves, moved with their tests unchanged:

- folder_children: a folder node's children are the canonical one-level
  listing, after both authorization gates -- containment beneath the configured
  root and the installation's allowed roots (PathResolver.authorize_beneath).
- FileDataObject: what this object owns is the shape -- it is handed a record,
  reads the place that record kept, and returns what it found. It discovers,
  authorizes and signs nothing.

Every filesystem here is a temporary directory built by the test. No real
installation directory is enumerated, and no log directory is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rey_lib.config.config_paths import PathResolver
from rey_lib.errors.error_utils import ConfigError
from rey_lib.tree import (
    DATABASE_SOURCE,
    FOLDER_SOURCE,
    JSONL_SOURCE,
    FileDataObject,
    folder_children,
    rey_lib_sources,
)


class _Ctx:
    """One installation: its resolved paths, which own its allowed roots (backlog 1066)."""

    def __init__(self, root: Path, allowed: list[str]) -> None:
        self.paths = PathResolver({
            "root": root,
            "configs": root / "configs",
            "elsewhere": root / "elsewhere",
        }, allowed)


@pytest.fixture()
def installation(tmp_path: Path) -> Path:
    """
    A small tree, built here rather than found anywhere.

    Written in an order no enumeration would return, so that what comes back
    proves the copied ordering rather than the order of creation.
    """
    configs = tmp_path / "configs"
    (configs / "install" / "shared").mkdir(parents=True)
    (configs / "Zebra").mkdir()
    (configs / "config.yaml").write_text("name: one\n", encoding="utf-8")
    (configs / "apple.yaml").write_text("name: two\n", encoding="utf-8")
    (configs / ".hidden.yaml").write_text("secret: yes\n", encoding="utf-8")
    (configs / "install" / "installation.yaml").write_text("paths: []\n", encoding="utf-8")
    (configs / "install" / "shared" / "tree.yaml").write_text("trees: {}\n", encoding="utf-8")

    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "reachable.yaml").write_text("a: b\n", encoding="utf-8")
    (tmp_path / "secret.yaml").write_text("password: no\n", encoding="utf-8")
    return tmp_path


def ctx_for(installation: Path, allowed: list[str] | None = None) -> Any:
    """The context, permitting the configured root unless told otherwise."""
    return _Ctx(installation, allowed if allowed is not None else ["root", "configs"])


def labels(entries: list[dict[str, str]]) -> list[str]:
    return [entry["label"] for entry in entries]


# -- folder_children ---------------------------------------------------------


def test_one_level_is_listed_in_the_copied_order(installation: Path) -> None:
    entries = folder_children(ctx_for(installation), str(installation / "configs"), "")

    # Directories before files, then case-insensitively by name. Neither rule
    # is applied here: this is what the copied enumeration returned.
    assert labels(entries) == ["install", "Zebra", "apple.yaml", "config.yaml"]
    assert [entry["kind"] for entry in entries] == ["folder", "folder", "file", "file"]


def test_nothing_below_that_level_is_listed(installation: Path) -> None:
    entries = folder_children(ctx_for(installation), str(installation / "configs"), "")

    assert "shared" not in labels(entries)
    assert "installation.yaml" not in labels(entries)


def test_a_deeper_level_is_listed_when_it_is_asked_for(installation: Path) -> None:
    entries = folder_children(ctx_for(installation), str(installation / "configs"), "install")

    assert labels(entries) == ["shared", "installation.yaml"]
    assert [entry["path"] for entry in entries] == [
        "install/shared", "install/installation.yaml",
    ]


def test_hidden_entries_stay_hidden(installation: Path) -> None:
    entries = folder_children(ctx_for(installation), str(installation / "configs"), "")

    assert ".hidden.yaml" not in labels(entries)


def test_every_location_returned_is_relative_to_its_root(installation: Path) -> None:
    entries = folder_children(ctx_for(installation), str(installation / "configs"), "install")

    for entry in entries:
        assert not entry["path"].startswith("/")
        assert str(installation) not in entry["path"]
        assert entry["root"] == str(installation / "configs")


def test_an_empty_directory_has_no_children(installation: Path) -> None:
    (installation / "configs" / "Zebra" / "inner").mkdir()
    entries = folder_children(ctx_for(installation), str(installation / "configs"), "Zebra/inner")

    # Not a placeholder standing in for one. A directory holding nothing has
    # no children, which is what the tree already knows how to show.
    assert entries == []


@pytest.mark.parametrize("escape", [
    "../secret.yaml",
    "install/../../secret.yaml",
    "../../../../etc",
])
def test_leaving_the_configured_root_is_refused(installation: Path, escape: str) -> None:
    with pytest.raises(ConfigError):
        folder_children(ctx_for(installation), str(installation / "configs"), escape)


def test_an_absolute_location_is_refused(installation: Path) -> None:
    with pytest.raises(ConfigError, match="relative to the exploration root"):
        folder_children(ctx_for(installation), str(installation / "configs"), str(installation))


def test_a_root_outside_the_allowed_boundary_is_refused(installation: Path) -> None:
    with pytest.raises(ConfigError, match="outside allowed roots"):
        folder_children(ctx_for(installation), str(installation.parent), "")


def test_containment_alone_is_not_enough(installation: Path) -> None:
    """
    The second gate refuses what the first one allowed.

    The location sits inside the configured root it was asked about, so it
    passes containment. When that root is not among the installation's allowed
    roots, the copied authorization refuses it on that ground alone.
    """
    permitted = folder_children(
        ctx_for(installation, ["root", "configs", "elsewhere"]),
        str(installation / "elsewhere"), "",
    )
    assert labels(permitted) == ["reachable.yaml"]

    with pytest.raises(ConfigError, match="outside allowed roots"):
        folder_children(ctx_for(installation, ["configs"]), str(installation / "elsewhere"), "")


def test_each_child_is_projected_relative_to_its_root(installation: Path) -> None:
    entries = folder_children(ctx_for(installation), str(installation / "configs"), "")

    for entry in entries:
        # Where the entry sits under its root, and nothing else. What opening
        # it means is the node type's answer, not the discovery's.
        assert set(entry) == {"kind", "label", "root", "path"}
        # The child's own location is relative; only the configured root it was
        # asked about is absolute, and that came from configuration.
        assert not Path(entry["path"]).is_absolute()


# -- FileDataObject ----------------------------------------------------------

#: What the file boundary answers with: where each entry sits under the root,
#: and nothing about what opening it would mean.
ENTRIES = [
    {"kind": "folder", "label": "install", "root": "/an/installation", "path": "install"},
    {"kind": "file", "label": "env.yaml", "root": "/an/installation",
     "path": "env.yaml"},
]

PLACE = {
    "node_key": "6:9",
    "label": "Config",
    "tree_object_key": "folder",
    "tree_configuration": {"root": "/an/installation", "relative_path": ""},
}


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record what the file boundary was asked, and answer with ENTRIES."""
    calls: list[tuple[str, str]] = []

    def _folder_children(_ctx: Any, root: str, relative_path: str) -> list[dict[str, str]]:
        calls.append((root, relative_path))
        return ENTRIES

    monkeypatch.setattr("rey_lib.tree.folder.folder_children", _folder_children)
    return calls


def test_the_place_is_read_through_the_file_boundary(asked) -> None:
    """The location comes from the record, and is asked about rather than asserted."""
    FileDataObject().children(object(), PLACE)

    assert asked == [("/an/installation", "")]


def test_a_directory_carries_its_source_forward(asked) -> None:
    """A directory found inside a folder opens by the rule its parent opened by."""
    directory = FileDataObject().children(object(), PLACE)[0]

    assert directory["label"] == "install"
    assert directory["has_children"] is True
    assert directory["tree_object_key"] == "folder"
    assert directory["tree_configuration"] == {
        "root": "/an/installation", "relative_path": "install",
    }
    # A directory is addressed the same way a file is; whether that identity
    # opens anything is the node type's answer, and folder is assigned nothing.
    assert directory["root"] == "/an/installation"
    assert directory["relative_path"] == "install"


def test_a_file_carries_the_authorization_and_names_no_action(asked) -> None:
    """Nothing is signed here, and nothing is named here.

    The reference the discovery issued is carried exactly as it came. Which
    action it authorizes is assembled where the node is built, from the node
    type, so naming one here would be the declaration that path removed.
    """
    file_record = FileDataObject().children(object(), PLACE)[1]

    assert file_record["has_children"] is False
    assert file_record["tree_object_key"] is None
    assert file_record["root"] == "/an/installation"
    assert file_record["relative_path"] == "env.yaml"
    assert "reference" not in file_record
    assert "text_click" not in file_record
    assert "buttons" not in file_record


def test_records_leave_without_identity(asked) -> None:
    """Which key a created record carries is not this object's answer."""
    for record in FileDataObject().children(object(), PLACE):
        assert "node_key" not in record
        assert "parent_node_key" not in record


def test_refresh_is_offered_at_the_configured_root_only(asked) -> None:
    """A directory found inside one is reached by asking its parent again."""
    for record in FileDataObject().children(object(), PLACE):
        assert record["refreshable"] is False


# -- the sources rey_lib implements (D3) -------------------------------------


def test_rey_lib_implements_the_folder_source() -> None:
    sources = rey_lib_sources()

    # The exact set: a source rey_lib starts implementing must be added here on
    # purpose, never appear unnoticed (folder #1056, database #1057, jsonl #1058).
    assert set(sources) == {FOLDER_SOURCE, DATABASE_SOURCE, JSONL_SOURCE}
    assert isinstance(sources[FOLDER_SOURCE], FileDataObject)

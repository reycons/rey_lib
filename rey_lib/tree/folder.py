"""A place on disk, as canonical node records -- the folder source (backlog 1056).

Moved from the Console (its file_data_object and file_explorer.folder_children)
with its behaviour unchanged. One feeder among several. It answers for a record
whose children are a directory's contents, and it knows nothing of trees,
projections, keys or the other sources -- it is handed a record, reads the place
that record kept, and returns what it found in the shape every feeder returns.

What it does not do is discover, authorize or name an action. Authorization is
the installation's PathResolver's, applied on every call:

    folder_children      re-resolves and re-authorizes the location, every call
      ctx.paths.authorize_beneath   containment, then the allowed-root gate
      visible_children   the canonical one-level listing
      _entry             where each entry sits under its root

So a location arriving from a browser is a request rather than a permission.
What a folder or a file can do is ``tree.node_type_action``'s answer, and the
authorization for doing it is built where the node is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.file_utils import visible_children
from rey_lib.tree.sources import FOLDER_SOURCE

__all__ = ["FileDataObject", "folder_children"]

#: What a directory and a file read as. Named, never drawn: the icon system
#: decides what either looks like.
_FOLDER_ICON = "folder"
_FILE_ICON = "file"

#: What each kind of entry *is*. The word the behaviour registry is keyed by:
#: what a folder or a file can do is the database's answer, looked up by this.
_FOLDER_TYPE = "folder"
_FILE_TYPE = "file"


def folder_children(
    ctx: Any,
    root: str,
    relative_path: str,
) -> list[dict[str, str]]:
    """
    What one directory contains, one level down.

    Parameters
    ----------
    ctx : Any
        Finalized installation context.
    root : str
        The configured root this location sits under, as configuration resolved
        it. Not a name and not a token: the loader already did that work.
    relative_path : str
        Where within that root, relative to it. Empty means the root itself.
        Whatever the browser sent, it is re-resolved and re-authorized here:
        structured identity is not authorization.

    Returns
    -------
    list[dict[str, str]]
        One entry per visible child, in the order the copied enumeration
        returned them. Empty when the directory holds nothing.

    Raises
    ------
    ConfigError
        When the location was refused or could not be read. The old object
        turned that into a row of its own; here it stays a failure, so the tree
        reports it where it already reports every other one.
    """
    directory = ctx.paths.authorize_beneath(root, relative_path)
    if not directory.is_dir():
        raise ConfigError("Folder is not available.")
    try:
        entries = visible_children(directory)
    except OSError as exc:
        raise ConfigError(f"Folder cannot be read: {exc}") from exc

    return [_entry(item, root, relative_path) for item in entries]


def _entry(item: Path, root: str, relative_path: str) -> dict[str, str]:
    """One discovered entry, as the browser addresses it.

    A file declares what clicking it means; a directory declares nothing,
    because opening a directory is expanding it and the tree does that for any
    expandable node with no declaration.

    Both locations stay relative to the configured root, as the old object kept
    them: no absolute location is transported to the browser, and the server
    re-resolves and re-authorizes from what it is given on each request.
    """
    where = f"{relative_path}/{item.name}" if relative_path else item.name
    if item.is_dir():
        return {"kind": "folder", "label": item.name, "root": root, "path": where}
    return {
        "kind": "file",
        "label": item.name,
        "root": root,
        "path": where,
    }


class FileDataObject:
    """The children of a record that stands at a place."""

    def state_key(self, _record: dict[str, Any]) -> str | None:
        """Nothing survives between calls for a folder.

        Every level is one directory read, re-authorized each time, so there is
        nothing worth holding and nothing to go stale.
        """
        return None

    def children(
        self,
        ctx: Any,
        record: dict[str, Any],
        _state: Any = None,
    ) -> list[dict[str, Any]]:
        """One directory level, as canonical records.

        The location is the record's own, kept when the record was made and
        never recovered from its key.

        Records come back **without identity**: which key a created record
        carries is the projection's answer, not this object's.

        Args:
            ctx: The installation context the location is resolved in.
            record: The parent, carrying ``{root, relative_path}``.

        Returns:
            One record per visible child, in the order the boundary returned
            them: directories before files, then by name.
        """
        configured = dict(record.get("tree_configuration") or {})
        root = str(configured.get("root") or configured.get("path") or "")
        relative = str(configured.get("relative_path") or "")
        return [
            self._record(entry, root)
            for entry in folder_children(ctx, root, relative)
        ]

    def _record(self, entry: dict[str, str], root: str) -> dict[str, Any]:
        """One discovered entry, as a node record."""
        directory = str(entry.get("kind") or "") == FOLDER_SOURCE
        return {
            "row_type": "node",
            "node_type": _FOLDER_TYPE if directory else _FILE_TYPE,
            "label": entry.get("label"),
            "icon": _FOLDER_ICON if directory else _FILE_ICON,
            "has_children": directory,
            "is_enabled": True,
            # A folder offers to be read again at its configured root only: a
            # directory found inside one is reached by asking its parent again.
            "refreshable": False,
            # Carried forward, so a directory opens by the rule its parent
            # opened by and nothing is worked out from a key.
            "tree_object_key": FOLDER_SOURCE if directory else None,
            "tree_configuration": {"root": root, "relative_path": entry.get("path")},
            # What this entry is addressed by. The mapping names these two
            # columns; nothing here says which action they are for.
            "root": root,
            "relative_path": entry.get("path"),
        }

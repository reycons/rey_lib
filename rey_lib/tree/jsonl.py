"""A configured JSONL file, as canonical node records.

One feeder among several, and the same shape as the others: handed a record, it
reads what that record's configuration names, projects what it finds, and
returns records without identity. It holds nothing -- the projection owns what
survives -- and it decides nothing about what a node means.

The four feeders differ only in what they are told to read:

    folder      configuration says which path
    database    configuration says which connection
    collection  configuration says which collection
    jsonl       configuration says which file, and how its records map to a row

**It does not know which file it is.** A file whose records it understood would
be a feeder per artifact, and the second one would be a switchboard. So the
mapping is declared instead, and a second JSONL hierarchy becomes a tree by
declaration rather than by another feeder.

**The declaration is the generic JSONL-to-tree projection contract, and only
that.** It says where the file is and which of its fields fill the canonical
row -- the edge, the identity carried to the next level, the label, the kind,
whether a record has records of its own. It may never name an artifact, a
domain, a repository, an architecture concept or an action. A key here that
only one file could answer is a domain leaking into a source contract, and the
next file would need a second one beside it.

**It selects nothing.** ``rey_lib.files.jsonl.read_jsonl_file`` owns path,
filters and fields; a level *is* a filter handed to it, and the records it
returns are the level. Reading a file and then re-deciding which rows belong
would put a second interpretation of JSONL matching in the Console, and the
reader exists so there is only one. The single loop here projects records
already selected.

It classifies nothing and counts nothing. What kind a record is arrives with it
and is carried; which action that kind is assigned is ``tree.node_type_action``'s
answer, as for every other feeder. Each record's own fields are carried onto
the row, which is what lets a mapping name one: a target binds to a column the
emitting row exposes, and for a feeder the row is the record.

The declaration's own scalars are carried too, beneath the record. A record
names a file relative to somewhere, and where that somewhere *is* is
configuration's answer, resolved before this object sees it -- so a mapping can
bind a root without this object knowing that any root exists.

Moved from the Console (backlog 1058) unchanged but for where the file is
authorized: ``ctx.paths.authorize_configured_file`` -- the installation's
PathResolver -- in place of the Console's file reference and resolver.
"""

from __future__ import annotations

from typing import Any

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.jsonl import read_jsonl_file
from rey_lib.tree.sources import JSONL_SOURCE

__all__ = ["JsonlDataObject"]

#: What a source must declare for a level to be addressable at all. A file
#: without these is not a hierarchy, and answering it with an empty level would
#: hide a configuration fault rather than report one.
_REQUIRED = ("file", "parent_field", "key_field", "label_field")

#: Where the place is kept. A root declares ``path`` as the configured token
#: it was written with, and the tree resolves it before this object is handed
#: anything; a record made here carries the resolved place forward as ``root``.
#: The same pair the folder feeder reads, for the same reason.
_PLACE = ("root", "path")

#: Where the value selecting this level is kept. Traversal state rather than
#: source schema: a root declares none -- which is what selects the records
#: whose parent field is null -- and every level below carries the one its
#: parent produced. Nothing authors it.
_PARENT = "parent"


class JsonlDataObject:
    """The children of a record that stands at a JSONL file."""

    def state_key(self, _record: dict[str, Any]) -> str | None:
        """Nothing survives between calls for a JSONL source.

        Every level is one filtered read, so a level holds nothing the next one
        needs.
        """
        return None

    def children(
        self,
        ctx: Any,
        record: dict[str, Any],
        _state: Any = None,
    ) -> list[dict[str, Any]]:
        """One level of the file, as canonical records.

        The level is a filter: the source's own constants, plus the parent
        field set to this record's key. A root carries no key, so that value is
        ``None`` and the reader answers with the records whose parent field is
        null -- which is what a root is, and is not the same as absent.

        The location is the record's own, kept when the record was made and
        never recovered from its key. It is re-resolved and re-authorized on
        the way in, so a configuration arriving from the browser is a request
        rather than a permission.

        Records come back **without identity**: which key a created record
        carries is the projection's answer, not this object's.

        Args:
            ctx: The installation context the location is resolved in.
            record: The parent, carrying the source declaration it stands on.

        Returns:
            One record per child, in the order the file held them.

        Raises:
            ConfigError: When the source declaration is incomplete.
        """
        declared = self._declaration(record)
        place = self._place(declared)
        path = ctx.paths.authorize_configured_file(place, str(declared["file"]))
        filters = {
            **(declared.get("filters") or {}),
            str(declared["parent_field"]): declared.get(_PARENT),
        }
        return [
            self._record(row.record, declared)
            for row in read_jsonl_file(path, filters=filters)
        ]

    def _declaration(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return the source declaration this record stands on, or refuse it.

        Args:
            record: The parent record.

        Returns:
            The declaration, with every required entry present.

        Raises:
            ConfigError: When one is missing. A source that cannot say which
                field addresses a level is a configuration fault, and an empty
                level would hide one.
        """
        declared = dict(record.get("tree_configuration") or {})
        missing = [name for name in _REQUIRED if not str(declared.get(name) or "")]
        if not self._place(declared):
            missing.insert(0, " or ".join(_PLACE))
        if missing:
            raise ConfigError(
                f"A JSONL source must declare {', '.join(_REQUIRED)}; "
                f"missing {', '.join(missing)}."
            )
        return declared

    def _place(self, declared: dict[str, Any]) -> str:
        """Where the file sits, as the tree already resolved it.

        A root declares the configured token and the tree expands it before
        this object sees anything; a record made here carries the resolved
        place forward. Nothing expands a token, joins a path or guesses at a
        root name -- doing any of that would be a second resolution beside the
        one the tree performs.
        """
        for name in _PLACE:
            place = str(declared.get(name) or "")
            if place:
                return place
        return ""

    def _record(
        self,
        row: dict[str, Any],
        declared: dict[str, Any],
    ) -> dict[str, Any]:
        """One read record, as a node record.

        The record's own fields come first and the node's follow, so a mapping
        may name any field the file exposes while the fields the tree reads
        stay this object's answer rather than the file's.
        """
        icon = self._declared(row, declared, "icon_field")
        has_children = bool(self._declared(row, declared, "has_children_field"))
        return {
            # What the source declared, as columns, so a mapping may bind to a
            # place the source stands beside -- the file a record names, rather
            # than the file the records are in. Resolved before this object was
            # handed anything, and the record's own fields win, because a
            # record is the row and a declaration only tells this where to read.
            **{
                key: value for key, value in declared.items()
                if isinstance(value, (str, int, float, bool))
            },
            **row,
            "row_type": "node",
            "node_type": str(self._declared(row, declared, "node_type_field") or ""),
            "label": str(row.get(str(declared["label_field"])) or ""),
            **({"icon": str(icon)} if icon else {}),
            # Carried, never inferred: whether a record has records of its own
            # is the file's answer and arrived with it.
            "has_children": has_children,
            "is_enabled": True,
            # A source offers to be read again at its declared root only: a
            # record found inside one is reached by asking its parent again.
            "refreshable": False,
            # Carried forward, so a child opens by the rule its parent opened
            # by and nothing is worked out from a key.
            "tree_object_key": JSONL_SOURCE if has_children else None,
            "tree_configuration": {
                **declared,
                # The resolved place, so a child is not handed a token the
                # tree only expands for a root.
                "root": self._place(declared),
                _PARENT: row.get(str(declared["key_field"])),
            },
        }

    def _declared(
        self,
        row: dict[str, Any],
        declared: dict[str, Any],
        name: str,
    ) -> Any:
        """Return one optionally declared field's value from a record."""
        field = str(declared.get(name) or "")
        return row.get(field) if field else None

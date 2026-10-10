"""The Transform: the canonical Loader-facing object for what is done to a load's records.

    Source -> Transform -> Target

**ONE OBJECT, EVERY ENTRY POINT**, as with ``Source``. The Console, a workflow
step, the CLI, a Runner-launched process and scheduled work construct this same
class and resolve through it. Canonical is the class and its contract, not an
instance: no live object crosses a process boundary.

**It owns CONFIGURATION, not execution.** It holds which kinds of transform a
load can have, what has been said about each, and which one is selected. What
applies the rules is still the existing resolved object -- ``ColumnTransform``
or ``IdentityTransform`` -- built by ``_build_transform`` when something
executes, which is also where secrets are resolved. Nothing here holds a
transform object, a context or a secret.

    identity     (nothing)                     -> IdentityTransform
    declaration  transform                     -> ColumnTransform
    yaml         transform-file                -> ColumnTransform
    http         http-adapter, http-transform  -> HTTPTransform (a ColumnTransform from
                                                  http-transform, then the HTTP operation)
    manifest     declaration, persistence      -> ColumnTransform

**MANIFEST AND YAML ARE CONFIGURATION ORIGINS, not implementations.** A stored
definition fills ``declaration`` and ``persistence`` -- ManifestSource is one way
they arrive, a hand configuration is another -- and every kind ends in the same
builder. ``persistence`` is ``TransformPersistence`` held as data: optional for
execution, and what a later write back to the database keys on.

**IT AUTHORS ITS OWN DECLARATION.** A column mapping is edited here -- one
field of one entry, a column added or moved -- and never by whatever draws it.
Every edit first COMPLETES the declaration against the source's columns, which
this instance is told (``observe_source_columns``) as a fact of its context:
never a field, never in the declarative form, never persisted.

**TWO VOCABULARIES.** ``TRANSFORM_FIELDS`` is everything this object holds.
``TRANSFORM_PARAMETERS`` is the subset the loader declares as parameters --
the names a CLI or a panel types. ``declaration`` and ``persistence`` are
fields of this object and not loader parameters.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from rey_lib.config.config_loader import parse_yaml
from rey_lib.data.column_transform import TransformPersistence, authorable_starters
from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.files.file_utils import read_text_file
from rey_lib.load import load_operation
from rey_lib.load.file_transform import build_file_transform, file_kind, file_kinds
from rey_lib.errors.error_utils import StateError
from rey_lib.logs import AppLogger
from rey_lib.web_utils.operation import HttpOperation

__all__ = [
    "COLUMN_FIELDS", "Transform", "TRANSFORM_FIELDS", "TRANSFORM_KINDS", "TRANSFORM_PARAMETERS",
]


_logger = AppLogger(__name__)


@dataclass(frozen=True)
class _Kind:
    """One kind of transform: the fields that are its own, and which it needs."""

    id: str
    fields: tuple[str, ...]
    required: tuple[str, ...]
    #: The field naming what this kind SENDS RECORDS THROUGH, for a kind whose
    #: execution reaches outside Rey; None for a local one. What makes a
    #: preview of it an outbound operation (backlogs 668, 813; 663 inv. 4).
    outbound_via: Optional[str] = None


#: The kinds a Transform can be, in the order a reader is offered them.
TRANSFORM_KINDS: tuple[_Kind, ...] = (
    _Kind("identity", (), ()),
    _Kind("declaration", ("transform",), ("transform",)),
    _Kind("yaml", ("transform-file",), ("transform-file",)),
    # Records sent through a governed HTTP operation (backlogs 671, 813): the
    # job named by `http-adapter` carries its own connection and behaviour.
    # The field names are the load command's flat parameter namespace;
    # executed_declaration normalises them. `http-transform` is the column
    # mapping applied BEFORE sending (backlog 679): authored as Declaration's
    # is, never required. `http-request` holds the values the user set for the
    # hydrated operation's request settings (backlog 841): authored in the
    # Request sub-tab, never required.
    _Kind(
        "http", ("http-adapter", "http-transform", "http-request"),
        ("http-adapter",), outbound_via="http-adapter",
    ),
    # `persistence` is never required to EXECUTE: a declaration defines the
    # transform, and the identities only say where it can be written back.
    _Kind("manifest", ("declaration", "persistence"), ("declaration",)),
)

#: Every field a Transform holds, across its kinds.
TRANSFORM_FIELDS: tuple[str, ...] = tuple(dict.fromkeys(
    name for kind in TRANSFORM_KINDS for name in kind.fields
))

#: The fields the loader declares as parameters -- the externally typed subset.
TRANSFORM_PARAMETERS: tuple[str, ...] = (
    "transform", "transform-file", "http-adapter", "http-transform", "http-request",
)

_BY_ID: dict[str, _Kind] = {kind.id: kind for kind in TRANSFORM_KINDS}

#: The kind that sends records through a governed HTTP operation.
_HTTP = "http"
#: The field an HTTP operation is chosen through.
_HTTP_ADAPTER = "http-adapter"
#: The field the user's values for the request settings are held in (backlog 841).
_HTTP_REQUEST = "http-request"
#: How a configured header is marked in the request shown.
_CONFIGURED = "configured"

# THE FILE-LEVEL KINDS -- what is done to a governed file rather than to its
# records -- are a REGISTRY OF THEIR OWN (``rey_lib.load.file_transform``),
# deliberately kept out of TRANSFORM_KINDS and :meth:`Transform.kinds`, which
# are the record-transform choices a load is offered. One resolver still serves
# both: :meth:`Transform.resolve` builds a ``FileTransform`` for a registered
# file-level kind.


def _kind_of(kind_id: str) -> Optional[_Kind]:
    """The record kind, or the registered file-level kind, called ``kind_id``."""
    if kind_id in _BY_ID:
        return _BY_ID[kind_id]
    registered = file_kind(kind_id)
    if registered is None:
        return None
    return _Kind(registered.id, registered.fields, registered.required)


def _is_file_kind(kind_id: str) -> bool:
    """Whether ``kind_id`` is a registered file-level kind."""
    return kind_id not in _BY_ID and file_kind(kind_id) is not None


def _file_fields() -> tuple[str, ...]:
    """Every field the registered file-level kinds hold."""
    return tuple(dict.fromkeys(
        name for kind in file_kinds() for name in kind.fields
    ))

#: The one field of one entry an edit may change, as ``edit_column`` names it.
COLUMN_FIELDS: tuple[str, ...] = ("source", "name", "datatype", "export", "type", "transform")

#: Where each authorable kind keeps its declaration.
_AUTHORED_IN: dict[str, str] = {
    "declaration": "transform", "http": "http-transform", "manifest": "declaration",
}

#: The persisted facts held per entry, beside the declaration and never in it.
_ALIGNED: tuple[str, ...] = ("column_ids", "column_ordinals")


def _held(value: Any) -> bool:
    """Whether a value says anything. None, empty text and an empty mapping do not."""
    if value is None:
        return False
    if isinstance(value, Mapping):
        return bool(value)
    return str(value).strip() != ""


class Transform:
    """What is done to a load's records: its configurations, and the one selected.

    Built directly, or from a declaration with :meth:`from_declaration`, which is
    how every entry point constructs one.
    """

    def __init__(
        self,
        values: Optional[Mapping[str, Any]] = None,
        selected: Optional[str] = None,
    ) -> None:
        """Hold what has been said about each kind, and which one is chosen.

        Args:
            values: Field values, across every kind. Switching kinds must not
                lose what was said under another, but only a Transform field
                may be given.
            selected: The chosen kind, or None to let the values decide.

        Raises:
            ValueError: If a value names a field no kind has, or ``selected``
                names no kind.
        """
        self._values: dict[str, Any] = {}
        self._selected: Optional[str] = None
        # CONTEXT, NOT CONFIGURATION: the columns the source was last observed
        # to carry. Never in `declaration()`, never persisted.
        self._source_columns: tuple[str, ...] = ()
        # Source -> Transform -> Target: THIS TRANSFORM REFERENCES ITS CURRENT
        # SOURCE AND TARGET (backlog 831) -- the objects themselves, handed over
        # by `from_source`, dropped by `release`. Held, never in `declaration()`,
        # never an execution argument, never persisted.
        self._source: Any = None
        self._target: Any = None
        # THE HTTP LIFECYCLE IS THIS OBJECT'S, AS MANIFEST'S IS THE SOURCE'S
        # (backlogs 829, 837, 841). Held, never in `declaration()`, never an
        # execution argument, never persisted:
        #   _http_operations  every enabled operation of the installation,
        #                     HYDRATED ONCE by `from_source`: what is chosen from
        #   _http_operation   THE ONE SELECTED, picked from those -- what the
        #                     request shown is read off, and what a preview or
        #                     run of THIS transform executes
        # Nothing after initialization reads Control or hydrates anything.
        self._http_operations: Optional[dict[str, HttpOperation]] = None
        self._http_operation: Optional[HttpOperation] = None
        for name, value in (values or {}).items():
            self.update(name, value)
        if selected is not None:
            # DECLARATIVE: the kind as declared, validated and nothing more. A
            # declaration rebuilt for execution holds no HTTP operation; a
            # preview or run reads its job when it resolves.
            self._selected = self._known_kind(selected)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_declaration(cls, declaration: Mapping[str, Any]) -> "Transform":
        """Build a Transform from its declarative form -- the inverse of :meth:`declaration`."""
        return cls(
            values=dict(declaration.get("values") or {}),
            selected=declaration.get("selected") or None,
        )

    @classmethod
    def from_source(
        cls,
        ctx: Any,
        source: Any,
        declaration: Optional[Mapping[str, Any]] = None,
        *,
        target: Any = None,
    ) -> "Transform":
        """Build a Transform that starts from what its Source carries (backlog 677).

        THE TRANSFORM READS ITS SOURCE TO CREATE ITSELF. Built from its
        declarative form exactly as :meth:`from_declaration`, then told the
        columns the Source carries through :meth:`observe_source` -- the context
        its initial mapping is completed against.

        **IT REFERENCES ITS SOURCE AND TARGET** (backlog 831): the objects
        themselves, so an edit to either is what this Transform sees next. The
        runtime is not kept. A declaration given is kept as it is: the columns
        are context, never a field, so a profiled declaration is never
        overwritten. A Source that cannot be read yet -- incomplete, or not
        resolvable -- starts the Transform with no columns.

        **THE TRANSFORM IS ESTABLISHED HERE**, and handed the one collaborator
        its HTTP kind needs: the runtime's Control, and the installation's name
        -- the objects, never the runtime (backlog 829). A Transform declared in
        kind ``http`` reads its jobs and hydrates its operation now, or is refused.

        Args:
            ctx: The runtime context, to resolve the Source.
            source: The canonical Source this Transform transforms.
            declaration: This Transform's declarative form, if it has one.
            target: The canonical Target this Transform feeds, if there is one.

        Returns:
            The Transform, its source columns observed where they could be read.

        Raises:
            ConfigError: If it is declared in kind ``http`` and its jobs cannot
                be read or its operation cannot be executed.
        """
        from rey_lib.config.bootstrap import open_shared_control  # noqa: PLC0415 -- the bootstrap reaches the load package, not the other way

        built = cls.from_declaration(declaration or {})
        built._source = source
        built._target = target
        # THE INITIAL HYDRATION (backlog 841): the installation's HTTP operations,
        # read once and hydrated, whatever kind is declared -- as Manifest's
        # read populates. Selection afterwards picks from them, in memory.
        control = open_shared_control(ctx).shared_control
        if control is not None:
            built._http_operations = _hydrated_operations(control, str(ctx.installation.name))
        if built.selected_kind() == _HTTP:
            built.select(_HTTP)
        # A SOURCE ESTABLISHED FROM A GOVERNED FILE gives this Transform its half
        # of the same one read: the selected saved setting (backlog 830).
        manifest = source.manifest() if source is not None else None
        if manifest is not None:
            manifest.populate_transform(built)
        built.observe_source(ctx)
        return built

    def declaration(self) -> dict[str, Any]:
        """The declarative form: what was said, and what was chosen.

        What travels, and what a write back would store: nothing in it is a
        live object, a context or a resolved secret.
        """
        return {"selected": self._selected, "values": dict(self._values)}

    # -- reading -------------------------------------------------------------

    @staticmethod
    def kinds() -> tuple[str, ...]:
        """Every kind a Transform can be, in offering order."""
        return tuple(kind.id for kind in TRANSFORM_KINDS)

    @staticmethod
    def form_fields() -> dict[str, list[str]]:
        """Each kind's fields that are drawn as controls beside its mapping grid.

        A kind's fields less the one its grid AUTHORS (``_AUTHORED_IN``): that
        field is written by the grid's operations -- ``edit_column`` and its
        siblings -- and is never a second, editable representation of the same
        state. It stays a field of the kind -- held, configurable and typed on
        the CLI -- and is only not drawn (backlog 679). ``http-request`` is
        likewise written by the Request sub-tab's ``edit_request_field``
        (backlog 841), and is not drawn.
        """
        return {
            kind.id: [
                name for name in kind.fields
                if name != _AUTHORED_IN.get(kind.id) and name != _HTTP_REQUEST
            ]
            for kind in TRANSFORM_KINDS
        }

    def selected_kind(self) -> str:
        """The kind in force.

        Chosen, else the first kind whose own fields carry values, else
        ``identity`` -- a load told nothing about its columns loads its rows as
        they came.
        """
        if self._selected is not None:
            return self._selected
        for kind in TRANSFORM_KINDS:
            if any(_held(self._values.get(name)) for name in kind.fields):
                return kind.id
        return TRANSFORM_KINDS[0].id

    def value(self, name: str) -> Any:
        """What has been said for one field, under any kind, or None."""
        return self._values.get(name)

    def configuration(self) -> dict[str, Any]:
        """The selected kind's fields and their values -- what is in force."""
        kind = _kind_of(self.selected_kind())
        return {name: self._values.get(name) for name in kind.fields}

    def outbound(self, max_preview_records: int) -> Optional[dict[str, Any]]:
        """Where the selected kind sends records, as generic metadata -- or None.

        A fact about the transform's EXECUTION EFFECT (663 invariant 4): a kind
        that sends records outside Rey says so, and a preview of it is an
        outbound operation a reader must ask for explicitly. Read from the
        configuration alone -- nothing is resolved or sent, and no context is
        needed.

        Args:
            max_preview_records: The most records a preview would send -- the
                previewing surface's own limit, stated back with the fact.

        Returns:
            None for a local kind; otherwise ``{"operation": <name>,
            "max_preview_records": <limit>}``.
        """
        kind = _kind_of(self.selected_kind())
        if kind is None or kind.outbound_via is None:
            return None
        return {
            "operation": str(self._values.get(kind.outbound_via) or ""),
            "max_preview_records": int(max_preview_records),
        }

    def validate(self) -> list[str]:
        """What the selected kind still needs, or nothing where it is complete.

        WHAT IS SHOWN IS CONFIGURED (backlog 681): the field a kind's grid
        authors is satisfied by the mapping :meth:`columns` shows, so an
        untouched pass-through grid is a complete transform.
        """
        kind = _kind_of(self.selected_kind())
        held = self.configuration()
        shown = _AUTHORED_IN.get(kind.id) if self.columns() else None
        return [
            name for name in kind.required
            if not _held(held.get(name)) and name != shown
        ]

    # -- changing ------------------------------------------------------------

    def select(self, kind: str) -> None:
        """Choose which kind is in force. The other kinds' values are kept.

        **ATOMIC WITH THE HTTP SELECTION (backlogs 829, 837).** Choosing
        ``http`` establishes it first -- the one read of the installation's
        jobs, through the Control this transform was handed -- and hydrates an
        operation it already names; only then is anything changed, so a
        refusal leaves the kind as it was. A selection already held is kept,
        unread. Choosing any other kind lets the jobs and the operation go.

        Raises:
            ConfigError: If no kind is called that, or ``http`` is chosen and
                its jobs cannot be read or its operation cannot be executed.
        """
        chosen = self._known_kind(kind)
        if chosen != _HTTP:
            # Only the selection goes: the hydrated operations are held for
            # this transform's lifetime.
            self._selected = chosen
            self._http_operation = None
            return
        operation = self._http_established()
        self._selected = chosen
        self._http_operation = operation

    def update(self, name: str, value: Any) -> None:
        """Say something about one field.

        The field an HTTP operation is chosen through, while this transform
        is in kind http, IS choosing one: it goes through
        :meth:`select_http_operation`, so the request shown is never another
        operation's.

        Raises:
            ValueError: If the field is not a Transform field. A source or a
                destination setting handed to a transform is a wiring fault.
        """
        if name not in TRANSFORM_FIELDS and name not in _file_fields():
            raise ConfigError(
                f"Transform: '{name}' is not a transform field. "
                f"Fields: {', '.join((*TRANSFORM_FIELDS, *_file_fields()))}."
            )
        if name == _HTTP_ADAPTER and self._in_http():
            self.select_http_operation(name, value)
            return
        self._values[name] = value

    # -- the HTTP operation --------------------------------------------------

    def select_http_operation(self, name: str, value: Any) -> None:
        """Choose this HTTP transform's operation, in memory. Nothing is read.

        The chosen job, one of the enabled jobs already read, is hydrated
        before anything changes, so a refusal leaves the operation as it was;
        then this transform holds its key -- the declared form -- and the one
        HttpOperation itself (backlog 837).

        Args:
            name: The field chosen through -- ``http-adapter``.
            value: The operation's key.

        Raises:
            StateError: If this transform is not in kind http.
            ConfigError: If the field is not ``http-adapter``, the operation is
                not an enabled one of the installation, or it cannot be executed.
        """
        if not self._in_http():
            _logger.debug("transform: refused -- an HTTP operation chosen outside kind http")
            raise StateError("Transform: no HTTP operation can be chosen -- it is not in kind http.")
        if name != _HTTP_ADAPTER:
            _logger.debug("transform: refused -- an HTTP operation chosen through field %s", name)
            raise ConfigError(
                f"Transform: an HTTP operation is chosen through "
                f"'{_HTTP_ADAPTER}', not '{name}'."
            )
        key = str(value or "")
        operation = self._held_operation(key)
        self._values[name] = key
        self._http_operation = operation

    def edit_request_field(self, part: str, name: str, value: Any) -> None:
        """Set the value of one of the held operation's request settings (backlog 841).

        The setting is the hydrated one -- its part, name and kind are the
        governed job's; only the value is this transform's, held as ordinary
        declared state in ``http-request`` so a run sends it too.

        Args:
            part: The setting's request part, as hydrated (``body``, ``query``,
                ``header``).
            name: The setting's name, as hydrated.
            value: The value to send -- for a reference, the Source column read.

        Raises:
            StateError: If this transform holds no HTTP operation.
            ConfigError: If the held operation has no such request setting.
        """
        if self._http_operation is None:
            _logger.debug("transform: refused -- a request setting edited with no HTTP operation")
            raise StateError("Transform: no request setting can be set -- no HTTP operation is held.")
        setting = next((
            one for one in self._http_operation.request_fields
            if (one.part, one.name) == (str(part), str(name))
        ), None)
        if setting is None:
            _logger.debug("transform: refused -- %s field %s is not a request setting", part, name)
            raise ConfigError(
                f"Transform: the HTTP operation has no {part} setting '{name}'."
            )
        # ONLY WHAT WAS HYDRATED (backlog 841): one of the setting's value-map
        # inputs, or -- for a reference -- one of the Source columns held here.
        choices = list(self._http_operation.value_maps.get((setting.part, setting.name)) or ())
        allowed = choices or (list(self._source_columns) if setting.kind == "reference" else [])
        if str(value) not in allowed:
            _logger.debug("transform: refused -- %s for %s field %s is not a hydrated choice",
                          value, part, name)
            raise ConfigError(
                f"Transform: '{value}' is not a choice for {part} setting '{name}'. "
                f"Choices: {', '.join(allowed) or 'none'}."
            )
        values = _request_values(self._values.get(_HTTP_REQUEST))
        values.setdefault(str(part), {})[str(name)] = value
        self._values[_HTTP_REQUEST] = json.dumps(values)

    def choices(self) -> dict[str, list[dict[str, str]]]:
        """The values a field may be chosen from: the hydrated HTTP operations, or none."""
        if not self._in_http():
            return {}
        return {_HTTP_ADAPTER: [
            {"value": key, "label": key} for key in (self._http_operations or {})
        ]}

    def field_operations(self) -> dict[str, str]:
        """The operation a field's edit invokes, where it is not an ordinary update."""
        if not self._in_http():
            return {}
        return {_HTTP_ADAPTER: "select_http_operation"}

    def _in_http(self) -> bool:
        """Whether this transform is in kind http with hydrated operations to choose from."""
        return self._http_operations is not None and self._selected == _HTTP

    def _http_established(self) -> Optional[HttpOperation]:
        """The operation this transform would hold in kind http. Reads nothing.

        Picked from the operations hydrated at initialization; an operation
        already selected is kept. Nothing is read and nothing is hydrated.

        Raises:
            ConfigError: If no operations were hydrated -- the runtime had no
                control database at initialization -- or the operation this
                transform names is not one of them.
        """
        if self._http_operations is None:
            _logger.debug("transform: refused -- kind http without hydrated operations")
            raise ConfigError(
                "Transform (http): HTTP operations are read from the control database, "
                "which this application has not configured."
            )
        if self._selected == _HTTP and self._http_operation is not None:
            return self._http_operation
        named = self._values.get(_HTTP_ADAPTER)
        return self._held_operation(str(named)) if named else None

    def _held_operation(self, key: str) -> HttpOperation:
        """One of the hydrated operations, by its key; refused unless it is one."""
        held = self._http_operations or {}
        if key not in held:
            _logger.debug("transform: refused -- operation %s is not one of %d enabled",
                          key, len(held))
            raise ConfigError(
                f"HTTP operation '{key}' is not an enabled operation of this "
                f"installation. Operations: {', '.join(held) or 'none'}."
            )
        return held[key]

    def _known_kind(self, kind: str) -> str:
        """The kind, refused unless one is called that."""
        if _kind_of(kind) is None:
            raise ConfigError(
                f"Transform: no transform kind is called '{kind}'. Kinds: "
                f"{', '.join((*self.kinds(), *(one.id for one in file_kinds())))}."
            )
        return kind

    # -- authoring -----------------------------------------------------------

    def observe_source_columns(self, columns: Any) -> None:
        """Be told which columns the source carries, so a mapping can be completed.

        A fact of this instance's context -- whoever read the source says so --
        and not configuration: it changes what an edit completes against and
        what :meth:`columns` shows, and nothing about what is declared.
        """
        self._source_columns = tuple(str(one) for one in (columns or ()))

    def source_columns(self) -> list[str]:
        """The columns the source was last observed to carry."""
        return list(self._source_columns)

    # -- its Source and Target -------------------------------------------------

    def source(self) -> Any:
        """The Source this Transform references, or None."""
        return self._source

    def observe_source(self, ctx: Any) -> bool:
        """Read which columns its Source now carries, and be told them.

        Read through the one existing reading, ``source_columns``. No Source, or
        one that cannot be read yet -- incomplete, or not resolvable -- tells
        this Transform nothing.

        Args:
            ctx: The runtime context, to resolve the Source.

        Returns:
            Whether this Transform was told.
        """
        from rey_lib.load.execution import source_columns  # noqa: PLC0415 -- execution imports this module

        if self._source is None:
            _logger.debug("transform: there is no source to read for its columns")
            return False
        try:
            columns = source_columns(ctx, self._source)
        except (AppError, ValueError, OSError) as exc:
            _logger.debug(
                "transform: the source could not be read for its columns (%s)",
                type(exc).__name__,
            )
            return False
        self.observe_source_columns(columns)
        return True

    def preview(self, ctx: Any, limit: int) -> load_operation.LoadPreview:
        """The first records its Source carries through this Transform.

        The one preview path, ``preview_selected``, on the Source this Transform
        references and on itself. A Transform that is not complete is refused,
        never passed through.

        Args:
            ctx: The runtime context, for configured connections.
            limit: How many records at most.

        Returns:
            A fresh result; nothing about it is kept.

        Raises:
            StateError: If this Transform references no Source.
            ConfigError: If it is not complete, or either object cannot be
                resolved.
        """
        from rey_lib.load.execution import preview_selected  # noqa: PLC0415 -- execution imports this module

        if self._source is None:
            _logger.debug("transform: refused -- a preview with no source")
            raise StateError("Transform has no Source to preview.")
        return preview_selected(ctx, self._source, self, limit=limit)

    def release(self, held: Any) -> None:
        """Stop referencing ``held`` if it is this Transform's Source or Target.

        Anything else changes nothing. A Transform whose Source was released has
        no Source, so it cannot preview.
        """
        if held is not None and held is self._source:
            self._source = None
        if held is not None and held is self._target:
            self._target = None

    # -- its Source's saved settings -------------------------------------------
    #
    # A SAVED SETTING IS ONE OF THE GOVERNED FILE'S TRANSFORMS (backlog 830).
    # Choosing, creating or deleting one rehydrates this Transform AND its
    # Source from the Source's governed read, so this Transform -- the one
    # object referencing both -- performs it. The ManifestSource decides and
    # hydrates; nothing else sequences it.

    def select_saved_setting(self, ctx: Any, transform_id: Any) -> None:
        """Make one saved setting the active configuration, in memory.

        The Source's governed read selects the transform -- nothing is read --
        and rehydrates the Source and this Transform; this Transform then reads
        the columns its Source now carries.

        Args:
            ctx: The runtime context, to read the Source's columns.
            transform_id: The saved setting, by its transform id.

        Raises:
            StateError: If this Transform's Source was not opened on a governed file.
            ConfigError: If the id is not a whole number.
            DataStructureError: If it is not one of the file type's transforms.
        """
        manifest = self._governed_manifest()
        try:
            chosen = int(transform_id)
        except (ValueError, TypeError) as exc:
            _logger.debug("transform: refused -- a saved setting named by %r", transform_id)
            raise ConfigError(
                f"A saved setting is named by its transform id, a whole number; "
                f"got {transform_id!r}."
            ) from exc
        manifest.select_transform(chosen)
        manifest.populate(self._source, self)
        self.observe_source(ctx)

    def new_saved_setting(self, ctx: Any) -> None:
        """Create a saved setting from the selected one, select it, and rehydrate both objects.

        Args:
            ctx: The runtime context, whose control database is written.

        Raises:
            StateError: If this Transform's Source was not opened on a governed file.
            ConfigError: If the runtime has no control database.
        """
        manifest = self._governed_manifest()
        manifest.new_transform(self._saved_settings_control(ctx), self._source, self)

    def delete_saved_setting(self, ctx: Any) -> None:
        """Delete the selected saved setting, resolve the next, and rehydrate both objects.

        Args:
            ctx: The runtime context, whose control database is written.

        Raises:
            StateError: If this Transform's Source was not opened on a governed
                file, or nothing is selected.
            ConfigError: If the runtime has no control database.
        """
        manifest = self._governed_manifest()
        manifest.delete_transform(self._saved_settings_control(ctx), self._source, self)

    def _governed_manifest(self) -> Any:
        """The governed read of the Source this Transform references, or a refusal."""
        manifest = self._source.manifest() if self._source is not None else None
        if manifest is None:
            _logger.debug("transform: refused -- saved settings with no governed source")
            raise StateError(
                "Transform: its source was not opened on a governed file; "
                "it has no saved settings."
            )
        return manifest

    @staticmethod
    def _saved_settings_control(ctx: Any) -> Any:
        """The runtime's one Control, or a refusal."""
        # Imported here: the bootstrap reaches the load package, not the other way.
        from rey_lib.config.bootstrap import open_shared_control  # noqa: PLC0415

        control = open_shared_control(ctx).shared_control
        if control is None:
            _logger.debug("transform: refused -- saved settings with no control database")
            raise ConfigError("Transform: this runtime has no control database for saved settings.")
        return control

    def http_request(self) -> Optional[dict[str, Any]]:
        """The selected HTTP operation's request, for display, or None.

        Read off THE ONE SELECTED HttpOperation (backlog 837) -- the same one
        a preview or run of this transform executes -- and never in
        :meth:`declaration`. The values set for its request settings
        (``http-request``, backlog 841) are applied, so what is shown is what
        is sent.
        """
        if self._http_operation is None:
            return None
        return _request_of(load_operation.with_request_values(
            self._http_operation, _request_values(self._values.get(_HTTP_REQUEST)),
        ))

    # -- persistence ---------------------------------------------------------

    def saves(self) -> bool:
        """Whether this transform can save its mapping to a governed transform."""
        if self.selected_kind() not in _AUTHORED_IN:
            return False
        persistence = self._values.get("persistence")
        return _held(persistence) and dict(persistence).get("transform_id") is not None

    def save(self, ctx: Any) -> None:
        """Save the working column mapping to the governed transform it came from.

        THE TRANSFORM SAVES ITSELF: every entry of :meth:`columns` is sent as it
        stands, with the stored ``transform_column_id`` it is aligned to (None
        for one no stored row stands behind), through the runtime's own database
        access. The database owns how they become rows; the ids it answers with,
        in working order, become this transform's stored identities.

        Args:
            ctx: The runtime context, whose control database this is saved to.

        Raises:
            ValueError: If this transform holds no governed transform to save to.
            ConfigError: If the runtime has no control database.
        """
        if not self.saves():
            raise StateError(
                "Transform: only a transform populated from a governed file can save it."
            )
        declared, columns, aligned = self._authoring()
        ids = list((aligned or {}).get("column_ids") or [None] * len(columns))
        entries = [
            {**deepcopy(entry), "transform_column_id": ids[at] if at < len(ids) else None}
            for at, entry in enumerate(columns)
        ]
        # Imported here: the bootstrap reaches the load package, not the other way.
        from rey_lib.config.bootstrap import open_shared_control

        control = open_shared_control(ctx).shared_control
        if control is None:
            raise ConfigError("Transform: this runtime has no control database to save to.")
        saved = control.update_transform_columns(
            dict(self._values["persistence"])["transform_id"], entries,
        )
        self._write(declared, columns, {
            "column_ids": list(saved),
            "column_ordinals": list(range(1, len(saved) + 1)),
        })
        # ITS SOURCE'S GOVERNED READ IS STALE: the persisted columns changed
        # (backlog 830). The Source re-reads itself; a plain Source reads nothing.
        if self._source is not None:
            self._source.reread_governed(ctx)

    def in_force_declaration(self) -> Optional[dict[str, Any]]:
        """The selected kind's declaration as data, or None where there is none to read.

        None for ``identity``; for ``yaml``, whose file is read at resolution;
        and for declaration text that is not JSON -- a declaration a reader
        wrote as YAML by hand is left alone rather than reformatted.
        """
        field = _AUTHORED_IN.get(self.selected_kind())
        if field is None:
            return None
        return _json_mapping(self._values.get(field))

    def columns(self) -> list[dict[str, Any]]:
        """The mapping as it would be authored: every source column represented.

        READ-ONLY. It shows what a first edit would write -- a pass-through for
        each source column the declaration does not already name -- and writes
        nothing, so looking at a mapping authors none.
        """
        declared = self.in_force_declaration() or {}
        completed, _ = _completed(_entries(declared), None, self._source_columns)
        return completed

    def column_ids(self) -> list[Optional[int]]:
        """The stored ``transform_column_id`` of each entry of :meth:`columns`.

        None for an entry no stored row stands behind -- one spliced in or
        added -- and empty where no stored identities are held at all.
        """
        return self._stored("column_ids")

    def column_ordinals(self) -> list[Optional[int]]:
        """The stored ``column_ordinal`` of each entry of :meth:`columns`.

        The PERSISTED ordinal, not the entry's position: the two agree only
        until an entry is moved or added, and what is shown as stored must be
        what is stored. Aligned as :meth:`column_ids` is.
        """
        return self._stored("column_ordinals")

    def _stored(self, key: str) -> list[Any]:
        """One persisted per-entry fact, aligned to :meth:`columns`."""
        aligned = self._held_alignment()
        if aligned is None or key not in aligned:
            return []
        declared = self.in_force_declaration() or {}
        _, completed = _completed(_entries(declared), aligned, self._source_columns)
        return list(completed[key])

    def _held_alignment(self) -> Optional[dict[str, list[Any]]]:
        """The per-entry persisted facts the Manifest kind holds, or None.

        ``column_ids`` wherever a persistence is held, as it always has been;
        ``column_ordinals`` only where the persistence stored them -- none are
        manufactured for one that never had them.
        """
        if self.selected_kind() not in _AUTHORED_IN:
            return None
        persistence = self._values.get("persistence")
        if not _held(persistence):
            return None
        held = dict(persistence)
        return {
            key: list(held.get(key) or ())
            for key in _ALIGNED if key == "column_ids" or key in held
        }

    def edit_column(self, at: int, field: str, value: Any) -> None:
        """Change one field of one entry, and nothing else about it.

        Clearing ``source`` or ``datatype`` removes it -- no source is a
        legitimate shape, and absent datatype is derived. ``export`` is written
        only to say false, because absent means exported. ``type`` puts a COPY
        of that type's starter on the entry, and leaves the entry alone for a
        type the library publishes no starter for. ``transform`` sets the rule,
        or removes it for None.

        Raises:
            ValueError: If the selected kind is not authored here, the field is
                not one an entry has, or no entry is at that position.
        """
        if field not in COLUMN_FIELDS:
            raise ConfigError(
                f"Transform: '{field}' is not a column field. "
                f"Fields: {', '.join(COLUMN_FIELDS)}."
            )
        declared, columns, aligned = self._authoring()
        entry = columns[_position(at, columns)]

        if field == "source":
            _set_or_clear(entry, "source", value)
        elif field == "name":
            entry["name"] = "" if value is None else str(value)
        elif field == "datatype":
            _set_or_clear(entry, "datatype", value)
        elif field == "export":
            if value is False or str(value).lower() == "false":
                entry["export"] = False
            else:
                entry.pop("export", None)
        elif field == "type":
            if not _held(value):
                entry.pop("transform", None)
            else:
                starter = authorable_starters().get(str(value))
                if starter is not None:
                    entry["transform"] = starter
        else:  # transform
            if value is None or (isinstance(value, str) and not value.strip()):
                entry.pop("transform", None)
            elif isinstance(value, Mapping):
                entry["transform"] = deepcopy(dict(value))
            else:
                raise ConfigError("Transform: a column's transform is a mapping, or None.")

        self._write(declared, columns, aligned)

    def add_column(self, after: Optional[int] = None) -> None:
        """Add an entry after one, from the same source, or at the end.

        After an entry, the new one is a second output from that entry's source,
        named as it; with none given, or beside an entry with no source, it is an
        empty output the reader names.

        Raises:
            ValueError: If the selected kind is not authored here, or ``after``
                names no entry.
        """
        declared, columns, aligned = self._authoring()
        if after is None:
            landed, made = len(columns), {"name": ""}
        else:
            beside = columns[_position(after, columns)]
            source = str(beside.get("source") or "")
            made = {"source": source, "name": source} if source else {"name": ""}
            landed = int(after) + 1
        columns.insert(landed, made)
        for stored in (aligned or {}).values():
            stored.insert(landed, None)
        self._write(declared, columns, aligned)

    def move_column(self, at: int, to: int) -> None:
        """Move one entry to another position. Moving it nowhere authors nothing.

        Raises:
            ValueError: If the selected kind is not authored here, or either
                position names no entry.
        """
        declared, columns, aligned = self._authoring()
        start, end = _position(at, columns), _position(to, columns)
        if start == end:
            return
        columns.insert(end, columns.pop(start))
        for stored in (aligned or {}).values():
            stored.insert(end, stored.pop(start))
        self._write(declared, columns, aligned)

    def _authoring(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], Optional[dict[str, list[Any]]]]:
        """The declaration an edit lands in, completed, with its stored facts where held.

        Raises:
            ValueError: For a kind that is not authored here, or declaration
                text that is not JSON.
        """
        kind = self.selected_kind()
        field = _AUTHORED_IN.get(kind)
        if field is None:
            raise StateError(
                f"Transform ({kind}): nothing is authored here. "
                + ("Choose Declaration to author a mapping."
                   if kind == "identity" else
                   "A declaration file is edited where it lives.")
            )
        held = self._values.get(field)
        declared = _json_mapping(held) if _held(held) else {}
        if declared is None:
            raise ConfigError(
                f"Transform ({kind}): the declaration is not JSON, and is left as "
                "it was written rather than reformatted."
            )
        columns, aligned = _completed(
            _entries(declared), self._held_alignment(), self._source_columns,
        )
        return declared, columns, aligned

    def _write(
        self,
        declared: dict[str, Any],
        columns: list[dict[str, Any]],
        aligned: Optional[dict[str, list[Any]]],
    ) -> None:
        """Put the whole authored declaration back where its kind keeps it."""
        field = _AUTHORED_IN[self.selected_kind()]
        written = {**declared, "columns": columns}
        if field in TRANSFORM_PARAMETERS:
            # JSON text: the loader parameter's own form, read by `parse_yaml`
            # -- Declaration's `transform` and HTTP's `http-transform`.
            self._values[field] = json.dumps(written, indent=2)
        else:
            self._values[field] = written
        # ONLY WHAT IS HELD. No stored fact is manufactured for a declaration
        # that was never stored, nor a key a persistence never had.
        if aligned:
            self._values["persistence"] = {
                **dict(self._values["persistence"]),
                **{key: list(stored) for key, stored in aligned.items()},
            }

    # -- execution -----------------------------------------------------------

    def executed_declaration(self) -> Optional[dict[str, Any]]:
        """The declaration execution sees, as data -- or None for identity.

        THE ONE STATEMENT OF WHAT RUNS. ``resolve`` builds from this, and
        anything that must show what a load would do -- a preview, an
        inspection -- reads it rather than working it out again. Data, never a
        built transform: building is ``_build_transform``'s.

        Returns:
            None for ``identity``; the parsed declaration for ``declaration``
            (inline YAML or JSON text, or a mapping) and ``yaml`` (the file,
            read here); the stored declaration for ``manifest``; for ``http``,
            the connection and adapter names, the options as a mapping, and
            the column declaration applied before sending, or None.

        Raises:
            ConfigError: If the selected kind is incomplete, or a declaration is
                missing, empty, unreadable or declares no columns.
        """
        missing = self.validate()
        if missing:
            raise ConfigError(
                f"Transform ({self.selected_kind()}) is missing: {', '.join(missing)}."
            )
        held = self.configuration()
        kind = self.selected_kind()
        if _is_file_kind(kind):
            raise ConfigError(
                f"Transform ({kind}) is a file-level kind: it operates on a governed "
                "file and has no record declaration."
            )
        if kind == "identity":
            return None
        # WHAT IS SHOWN IS WHAT EXECUTES (backlog 681): the mapping the grid
        # shows, where it shows one; otherwise the authored text as it stands.
        shown = self._shown_declaration()
        if kind == "http":
            # NAMES ONLY: the operation's key. Its connection, credential
            # reference and behaviour are the governed job's (backlog 813).
            return {
                "adapter": str(held["http-adapter"]),
                # THE SHOWN MAPPING, applied before sending; None only where
                # nothing is shown or authored.
                "declaration": shown if shown is not None else (
                    _declared(held["http-transform"], "http-transform")
                    if _held(held.get("http-transform")) else None
                ),
                # THE VALUES SET FOR THE REQUEST SETTINGS (backlog 841), or None.
                "request": (
                    _plain(held[_HTTP_REQUEST], _HTTP_REQUEST)
                    if _held(held.get(_HTTP_REQUEST)) else None
                ),
            }
        if kind == "declaration":
            return shown if shown is not None else _declared(held["transform"], "transform")
        if kind == "yaml":
            return _declared_in(held["transform-file"])
        return shown if shown is not None else _plain(held["declaration"], "declaration")

    def _shown_declaration(self) -> Optional[dict[str, Any]]:
        """The declaration the grid shows -- every source column represented -- or None.

        None where the kind has no grid, where nothing is shown, or where the
        authored field holds text that is not JSON: hand-written text is
        executed as it is, never replaced by a pass-through.
        """
        field = _AUTHORED_IN.get(self.selected_kind())
        if field is None:
            return None
        declared = self.in_force_declaration()
        if declared is None and _held(self._values.get(field)):
            return None
        columns = self.columns()
        if not columns:
            return None
        return {**(declared or {}), "columns": columns}

    def resolve(self, ctx: Any) -> Any:
        """The transform the selected configuration applies, as the load builds it.

        Every kind goes through ``_build_transform`` -- the one place a load's
        transform is built, and where secrets are resolved -- from
        :meth:`executed_declaration`, and what it builds is handed back, never
        kept.

        Args:
            ctx: The runtime context, for context values a rule may reference.

        Returns:
            An ``IdentityTransform`` for ``identity``; a ``ColumnTransform`` for
            every other record kind, carrying its ``TransformPersistence`` where
            the configuration holds one; an ``HTTPTransform`` for ``http``
            (``load_operation._build_http_transform``); a ``FileTransform`` for
            a registered file-level kind (``rey_lib.load.file_transform``).

        Raises:
            ConfigError: As :meth:`executed_declaration` refuses, or where a
                file-level kind is incomplete.
        """
        kind = self.selected_kind()
        if _is_file_kind(kind):
            missing = self.validate()
            if missing:
                raise ConfigError(
                    f"Transform ({kind}) is missing: {', '.join(missing)}."
                )
            return build_file_transform(ctx, kind, self.configuration())
        if kind == "http":
            declared = self.executed_declaration()
            # THE HELD OPERATION EXECUTES (backlog 837): the one selected, whose
            # request is shown. A transform rebuilt from its declaration -- a
            # separate process or run -- holds none, and the job is read there.
            return load_operation._build_http_transform(
                ctx, declared, operation=self._http_operation,
            )
        declared = self.executed_declaration()
        # GOVERNED PERSISTENCE travels only where it is held, for the kinds
        # authored here.
        persistence = self._values.get("persistence")
        if self.selected_kind() in _AUTHORED_IN and _held(persistence):
            return load_operation._build_transform(
                ctx, None, declared, persistence=_persistence(persistence),
            )
        return load_operation._build_transform(ctx, None, declared)


def _hydrated_operations(control: Any, installation: str) -> dict[str, HttpOperation]:
    """The installation's enabled HTTP operations, read once and hydrated (backlog 841).

    The Transform's initial hydration: one control read, every enabled job
    hydrated, in read order. IF IT WAS NOT HYDRATED, IT DOES NOT EXIST: a job
    that cannot be executed is not held and so is never offered, and its
    refusal is logged naming it. One bad governed job must not stop every
    Transform from being built.
    """
    _logger.debug("transform: hydrating the HTTP operations of installation %s", installation)
    hydrated: dict[str, HttpOperation] = {}
    for row in control.http_operation(installation):
        if not row.get("is_enabled"):
            continue
        try:
            hydrated[str(row["adapter_key"])] = HttpOperation.hydrate(row)
        except ConfigError as exc:
            _logger.warning("transform: HTTP operation %s is not offered: %s",
                            row.get("adapter_key"), exc)
    return hydrated


def _request_values(value: Any) -> dict[str, dict[str, Any]]:
    """The held ``http-request`` values, ``{part: {name: value}}``; empty when none are set."""
    if not _held(value):
        return {}
    return {
        str(part): dict(named or {})
        for part, named in _plain(value, _HTTP_REQUEST).items()
    }


def _request_of(operation: HttpOperation) -> dict[str, Any]:
    """The operation's request, as plain display facts. No credential is in it.

    Fields keep the operation's order -- body, query, then header, each as the
    job orders them. Configured headers are ordered by name: a JSON object
    states no order of its own. Each row states its own part and kind, so a
    reader shows them without deciding what they are. The authentication
    header and its secret reference are never included.
    """
    return {
        "method": operation.http_method,
        "fields": [
            {
                "part": one.part, "name": one.name, "kind": one.kind, "value": one.value,
                # THE HYDRATED CHOICES (backlog 841): the inputs of this field's
                # value map, in hydrated order, only where it has one.
                **({"choices": list(operation.value_maps[(one.part, one.name)])}
                   if operation.value_maps.get((one.part, one.name)) else {}),
            }
            for one in operation.request_fields
        ],
        "headers": [
            {"part": "header", "name": str(name), "kind": _CONFIGURED, "value": value}
            for name, value in sorted((operation.default_headers or {}).items())
        ],
    }


def _json_mapping(value: Any) -> Optional[dict[str, Any]]:
    """A declaration as data where it is a mapping or JSON text of one, else None."""
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _entries(declared: Mapping[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """The declaration's column entries, each with its position in ``columns``."""
    held = declared.get("columns")
    if not isinstance(held, list):
        return []
    return [(at, dict(one)) for at, one in enumerate(held) if isinstance(one, Mapping)]


def _completed(
    entries: list[tuple[int, dict[str, Any]]],
    stored: Optional[Mapping[str, list[Any]]],
    structure: tuple[str, ...],
) -> tuple[list[dict[str, Any]], Optional[dict[str, list[Any]]]]:
    """Every source column represented, with no existing entry reordered.

    **A DECLARATION IS AUTHORITATIVE ONCE IT NAMES ONE COLUMN** -- every source
    column it does not name is dropped by a load that looks like it succeeded.
    So an edit authors a mapping that matches the source.

    Each missing source column, in source order, is spliced:

      1. after the LAST entry of the nearest source column before it that has
         one;
      2. failing that, before the FIRST entry of the nearest one after it;
      3. failing both, at the end.

    Every stored per-entry fact stays aligned: a spliced entry has none.
    """
    columns = [entry for _, entry in entries]
    aligned: Optional[dict[str, list[Any]]] = None
    if stored is not None:
        aligned = {
            key: [facts[at] if at < len(facts) else None for at, _ in entries]
            for key, facts in stored.items()
        }

    def held(source: str) -> list[int]:
        return [at for at, one in enumerate(columns) if str(one.get("source") or "") == source]

    for which, source in enumerate(structure):
        if held(source):
            continue
        at: Optional[int] = None
        for before in range(which - 1, -1, -1):
            entries_before = held(structure[before])
            if entries_before:
                at = entries_before[-1] + 1
                break
        if at is None:
            for after in range(which + 1, len(structure)):
                entries_after = held(structure[after])
                if entries_after:
                    at = entries_after[0]
                    break
        landed = len(columns) if at is None else at
        columns.insert(landed, {"source": source, "name": source})
        for facts in (aligned or {}).values():
            facts.insert(landed, None)
    return columns, aligned


def _position(at: Any, columns: list[Any]) -> int:
    """One entry's position, refused where no entry is there."""
    try:
        position = int(at)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Transform: '{at}' is not a column position.") from exc
    if position < 0 or position >= len(columns):
        raise ConfigError(f"Transform: there is no column at position {position}.")
    return position


def _set_or_clear(entry: dict[str, Any], key: str, value: Any) -> None:
    """Set one key, or remove it where the value says nothing."""
    if _held(value):
        entry[key] = str(value)
    else:
        entry.pop(key, None)


def _plain(value: Any, named: str) -> dict[str, Any]:
    """A declaration as a mapping: taken as it is, or parsed from YAML or JSON text.

    Raises:
        ConfigError: If it holds nothing, cannot be read, or is not a mapping.
    """
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value)
    if not text.strip():
        raise ConfigError(f"Transform ({named}) holds no declaration.")
    try:
        parsed = parse_yaml(text)
    except ConfigError as exc:
        raise ConfigError(f"Transform ({named}) could not be read: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"Transform ({named}) is not a declaration.")
    return parsed


def _declared(value: Any, named: str) -> dict[str, Any]:
    """A typed declaration, refused where it declares no columns.

    The refusals the CLI makes at its boundary today: a declaration with no
    columns would produce records with no fields, and look like success.
    """
    declared = _plain(value, named)
    if not declared.get("columns"):
        raise ConfigError(
            f"Transform ({named}) declares no columns. A transform says what "
            "each output column is and where it comes from; one with none "
            "would produce records with no fields."
        )
    return declared


def _declared_in(path_text: Any) -> dict[str, Any]:
    """The declaration a file holds, read at resolution as a SQL file is.

    Raises:
        ConfigError: If there is no such file, or what it holds is refused.
    """
    path = Path(str(path_text))
    if not path.is_file():
        raise ConfigError(f"Transform (yaml): no such file: {path}")
    return _declared(read_text_file(path), f"yaml {path}")


def _persistence(value: Any) -> TransformPersistence:
    """The stored identities, as the transform object carries them."""
    held = dict(value)
    return TransformPersistence(
        transform_id=int(held["transform_id"]),
        file_type_id=int(held["file_type_id"]),
        column_ids=tuple(
            None if one is None else int(one) for one in held.get("column_ids") or ()
        ),
    )

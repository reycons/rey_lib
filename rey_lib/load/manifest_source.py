"""A governed file as a source, hydrated from one control contract.

    control.f_file_source_context_get -> ManifestSource -> existing objects

**WIRING, not a new architecture.** Every object a load needs already exists;
what was missing was a way to populate them from a file that lives in Rey's own
lifecycle -- a ``control.file_manifest`` record, which is the form in which the
estate knows most of its files. The Loader could take a path, a query or a SQL
file, and not one of those.

So this is a FIRST-CLASS HYDRATION OBJECT: it performs the single source-context
read and is the authoritative populator of the runtime objects associated with
that governed file.

**It owns POPULATION, not IMPLEMENTATION.** That one sentence decides everything
here. It is neither a bag of rows nor a place a Loader assembles objects by
hand; it is the adapter from persisted state into executable objects, and each
object it returns keeps its own defaults, validation and execution semantics.

    data_file()        the existing DataFile, via data_file_for
    data_profile()     the existing DataProfile and its field objects
    column_transform() the existing ColumnTransform

Three identities, each with a different job
-------------------------------------------
    file_manifest_id   persistent file identity -- the working file instance
    file_mutation_id   the specific materialised state being used
    file_type_id       the reusable GOVERNING configuration scope

**Flattening them is the failure this object most guards against.** A
transformation edited while one manifest is open persists against its TYPE,
reaching every manifest of that type. The manifest is the working file used to
design and test; it never becomes the owner of type-level rules, and neither
does a mutation merely because it was the entry point.

What it does NOT do
-------------------
**It never reads the physical file.** It resolves the governed context and hands
the working path to ``data_file_for``; all physical reading stays in the file
object, which already has the registry and the suites for it. A source
constructed against a path that does not exist still resolves its context.

**It never writes.** Manifest, mutation, path, layout, profile and profile-field
state are read-only here -- they are owned by profiling, inventory and
conversion, each with its own rules. The one editable, persistable thing is the
``ColumnTransform`` this hydrates, which carries its own identities.

**It does not know how the records are joined.** That is the contract's, which
is the entire point of there being one.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from rey_lib.data.column_transform import ColumnTransform, TransformPersistence
from rey_lib.data.data_profile import DataProfile, FieldProfile, ProfileField
from rey_lib.data.errors import DataStructureError
from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.logs import get_logger

__all__ = ["ManifestSource", "SourceContextReader"]

_logger = get_logger(__name__)


class SourceContextReader(Protocol):
    """Whatever answers the source-context contract.

    A PROTOCOL rather than a concrete type, so this module imports no
    persistence. ``Control`` satisfies it; a test satisfies it with the rows
    directly.
    """

    def file_source_context(
        self,
        file_manifest_id: Optional[int] = None,
        file_mutation_id: Optional[int] = None,
        required: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        """Return the joined context rows for one governed file."""
        ...


class ManifestSource:
    """One governed file's context, and the objects it populates.

    Built through :meth:`create`, which is where the single control call and
    the identity validation happen. Constructing one directly with rows is
    legitimate and is what a test does; nothing here reaches for a reader.
    """

    def __init__(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        opened_by: str,
        requested_file_type_id: Optional[int] = None,
    ) -> None:
        """Materialise the joined rows into this object's state.

        Args:
            rows: The contract's result. One row per profile field x transform
                column, with parent values repeating -- which is the agreed
                shape and not a defect to work around.
            opened_by: ``manifest`` or ``mutation``, recording which identity
                this was entered by. Held rather than re-derived: the contract
                already decided, and a second answer here could disagree.
            requested_file_type_id: The caller's INTENDED governing scope, or
                None where it deliberately named none.

        Raises:
            DataStructureError: If the rows are empty. The contract raises for
                every no-such-file case, so an empty result means the reader
                was unavailable rather than that the file has no configuration
                -- and those must not be read alike.
        """
        if not rows:
            raise DataStructureError(
                "ManifestSource: the source-context read returned no rows. "
                "The contract raises for an unknown file, so an empty result "
                "means the context could not be read at all."
            )

        first = dict(rows[0])
        self.file_manifest_id: int = int(first["file_manifest_id"])
        self.file_mutation_id: int = int(first["file_mutation_id"])
        #: The manifest's PERSISTED governing scope, which is a fact and is not
        #: the same question as what the caller intended. See `governing_*`.
        self.persisted_file_type_id: Optional[int] = _as_int(first["file_type_id"])
        self.requested_file_type_id = requested_file_type_id
        self.opened_by = opened_by

        self.installation_id: Optional[int] = _as_int(first["installation_id"])
        self.path: Optional[str] = first["path"]
        #: The DECLARED format token, which beats the path's suffix.
        self.layout: Optional[str] = first["layout"]
        #: The SQL the database resolved for this file's path, as returned.
        self.resolved_query_sql: Optional[str] = first.get("resolved_query_sql")
        #: The governed query row this file's SQL template is stored in.
        self.transform_query_id: Optional[int] = _as_int(first.get("transform_query_id"))
        #: The manifest's live mutations, each with its path and resolved SQL.
        self.mutation_choices: list[dict[str, Any]] = [
            dict(one) for one in (first.get("mutation_choices") or [])
        ]

        #: The file facts, as the contract returned them. Read-only.
        self.file_facts: dict[str, Any] = {
            name: first[name] for name in _FILE_FACTS if name in first
        }

        self._profile_fields = _dedupe(rows, "data_profile_field_id")
        # IN column_ordinal ORDER, STATED HERE rather than inherited from how the
        # contract happens to join: the stored ordinal is the columns' order.
        self._transform_columns = _in_column_order(_dedupe(rows, "transform_column_id"))
        self.data_profile_id: Optional[int] = _as_int(first["data_profile_id"])
        self.transform_id: Optional[int] = _as_int(first["transform_id"])
        self._profile_header: str = first["profile_header_definition"] or ""
        # THE DEFINITION'S OWN, taken from the parent row rather than from a
        # column row: a transform may have a row filter and no columns yet, and
        # reading it off the children would lose it in exactly that case.
        self._row_filter: Any = first["transform_row_filter"]
        #: Whether the stored transformation is its file type's DEFAULT -- at most
        #: one per installation and file type, and possibly none. A FACT, read
        #: from the parent row for the same reason the row filter is:
        #: ``column_transform`` returns the stored definition either way.
        self.transform_is_default: bool = bool(first.get("transform_is_default"))

    # -- construction -------------------------------------------------------

    @classmethod
    def create(
        cls,
        reader: SourceContextReader,
        *,
        file_manifest_id: Optional[int] = None,
        file_mutation_id: Optional[int] = None,
        file_type_id: Optional[int] = None,
        adopt_persisted_type: bool = False,
    ) -> "ManifestSource":
        """Read one governed file's context and return the source for it.

        **EXACTLY ONE control call**, made here. Everything after it is
        materialisation of rows already in hand; there is no follow-up lookup
        for a type, a profile, its fields, a transform or its columns, because
        the contract returned all of them joined.

        **THE VALIDATION LIVES HERE, NOT IN THE LOADER.** A caller hands over
        the three supplied identities and receives either a valid object or an
        explicit failure. A Loader that compared the returned manifest's type
        against the requested one would have Manifest semantics leaking back
        into it, and the boundary would exist in name only.

        Args:
            reader: Whatever answers the source-context contract. INJECTED,
                not reached for, so this object has no ambient state and a test
                substitutes one by passing it -- the same reason ``QuerySource``
                takes its adapter.
            file_manifest_id: The file's persistent identity.
            file_mutation_id: A specific materialised state. Where given it is
                PRESERVED exactly; the effective-mutation rule is not applied
                over the top of a caller's explicit choice.
            file_type_id: The INTENDED governing scope, or None to open the
                file with none. **None is never replaced by the manifest's
                persisted type** unless the caller asks for exactly that with
                ``adopt_persisted_type`` -- see :meth:`governing_scope_available`.
            adopt_persisted_type: Where no ``file_type_id`` was supplied, govern
                by the type the file already has. OPT-IN, and for a caller whose
                missing type is a fact it could not carry rather than a choice:
                a reader opening the loader on a governed file from a tree node
                did not ask for no governing scope, the node simply holds none.
                Taken from the rows this call already read, so it costs no
                second read. A supplied ``file_type_id`` still wins and is still
                validated.

        Returns:
            The source, with its context resolved.

        Raises:
            ValueError: If neither identity is supplied. Refused here as well
                as by the routine, so no call is made that cannot succeed.
            DataStructureError: If the resolved context contradicts what was
                asked for.
        """
        if file_manifest_id is None and file_mutation_id is None:
            raise ValueError(
                "ManifestSource: a file_manifest_id or a file_mutation_id is "
                "required. There is no default file."
            )

        rows = list(reader.file_source_context(
            file_manifest_id=file_manifest_id,
            file_mutation_id=file_mutation_id,
        ))

        source = cls(
            rows,
            opened_by="mutation" if file_mutation_id is not None else "manifest",
            requested_file_type_id=file_type_id,
        )
        source._validate_against(file_manifest_id, file_mutation_id, file_type_id)
        if file_type_id is None and adopt_persisted_type:
            # The persisted type, now asked for. Validated by construction: it
            # is the value _validate_against would have compared against.
            source.requested_file_type_id = source.persisted_file_type_id
        return source

    def _validate_against(
        self,
        file_manifest_id: Optional[int],
        file_mutation_id: Optional[int],
        file_type_id: Optional[int],
    ) -> None:
        """Refuse a context that does not match what was asked for.

        The routine validates the manifest/mutation pair itself. What it cannot
        validate is CALLER INTENT about the governing scope, which is this
        object's business -- so that is checked here.

        Raises:
            DataStructureError: Naming both values, because a caller holding an
                identity the database disagrees with does not know what it
                opened, and a message without the numbers cannot be acted on.
        """
        if file_manifest_id is not None and file_manifest_id != self.file_manifest_id:
            raise DataStructureError(
                f"ManifestSource: asked for manifest {file_manifest_id} and the "
                f"context resolved to {self.file_manifest_id}."
            )
        if file_mutation_id is not None and file_mutation_id != self.file_mutation_id:
            raise DataStructureError(
                f"ManifestSource: asked for mutation {file_mutation_id} and the "
                f"context resolved to {self.file_mutation_id}."
            )

        # A SUPPLIED SCOPE MUST BE THE PERSISTED ONE. Refused rather than
        # resolved in favour of either: an edit made under a scope the file does
        # not belong to would reach a family of files nobody chose.
        if file_type_id is not None and file_type_id != self.persisted_file_type_id:
            raise DataStructureError(
                f"ManifestSource: asked to govern manifest "
                f"{self.file_manifest_id} by file type {file_type_id}, but it "
                f"is governed by {self.persisted_file_type_id}."
            )

    # -- capabilities -------------------------------------------------------

    @property
    def working_file_available(self) -> bool:
        """Whether the manifest and mutation resolved to a file to work on."""
        return bool(self.path)

    @property
    def governing_scope_available(self) -> bool:
        """Whether a governing scope was REQUESTED and validated.

        **NOT "whether the file has a type".** The manifest's persisted type is
        a fact the contract returns either way; this answers whether the caller
        asked to work within it. A caller that deliberately supplied none must
        not acquire one because the file happens to have it -- that is how a
        type-level edit reaches files nobody chose.
        """
        return self.requested_file_type_id is not None

    @property
    def type_configuration_editable(self) -> bool:
        """Whether type-level configuration may be edited through this source.

        The same question as :meth:`governing_scope_available`, and stated
        separately because that is what every surface downstream asks instead of
        inspecting a null. The only editable thing is transformation
        configuration governed by ``file_type_id``, so the two coincide by
        definition rather than by coincidence.
        """
        return self.governing_scope_available

    # -- the existing objects, populated ------------------------------------

    def data_file(self, **settings: Any) -> DataFile:
        """The existing DataFile for the working path.

        **The declared layout WINS over the suffix**, exactly as a configured
        load's declared ``file_type`` does -- ``ConfiguredLoad.data_file_for``
        is the same shape, and for the same reason: the estate has already said
        what this file is, so nothing guesses from a name.

        Args:
            settings: Format-specific settings for the subtype, passed through
                untouched.

        Returns:
            The registered subtype for this file's format.

        Raises:
            DataStructureError: Where the context resolved no path. A file
                object over an unknown path could only fail later and somewhere
                less informative.
        """
        if not self.path:
            raise DataStructureError(
                f"ManifestSource: manifest {self.file_manifest_id} resolved no "
                f"working path, so there is no file to open."
            )
        # NO READ HAPPENS HERE. A DataFile is built over a path; opening it is
        # the caller's, which is what keeps this object out of the filesystem.
        return data_file_for(
            Path(self.path), file_type=self.layout or "", **settings
        )

    def data_profile(self) -> DataProfile:
        """The existing DataProfile, populated from the returned field rows.

        **THE REDACTED READING ONLY**, because that is what the contract
        returns: the clear reading carries values taken from the file itself.
        So ``redacted`` is populated and ``clear`` is EMPTY, which is the honest
        statement of what was read rather than a gap to fill from elsewhere.

        ``field_count`` is not copied across -- ``DataProfile`` deliberately has
        no such attribute, because it is ``len(fields)`` and a stored second
        answer is the one that would disagree.

        Returns:
            The profile. An empty one where the type has no profile, which is an
            answer rather than a failure.
        """
        fields: list[ProfileField] = []
        readings: list[FieldProfile] = []
        for ordinal, row in enumerate(self._profile_fields, start=1):
            name = str(row["field_name"])
            # THE ROW'S OWN ORDINAL WHERE IT HAS ONE. data_profile_field.ordinal
            # is nullable, so a field without one falls back to its position in
            # the contract's ordering rather than to zero.
            at = _as_int(row["field_ordinal"]) or ordinal
            fields.append(ProfileField(name=name, ordinal=at))
            readings.append(FieldProfile(
                name=name,
                ordinal=at,
                detected_type=row["field_detected_type"],
            ))

        return DataProfile(
            fields=tuple(fields),
            # A STORED profile IS the complete structural definition: it was
            # written from a profiling event that read the whole file.
            structure_complete=bool(fields),
            # NEVER TRUE FROM HERE. Nothing in this path checked the data
            # against the structure, so nothing here may claim it was checked.
            structure_validated=False,
            clear=(),
            redacted=tuple(readings),
            header_definition=self._profile_header,
        )

    def column_transform(
        self,
        *,
        context: Any = None,
        secrets: Optional[dict[str, str]] = None,
    ) -> Optional[ColumnTransform]:
        """The existing ColumnTransform for the active governing scope.

        **ACTIVE, not persisted.** The contract returns the persisted type's
        transformation rows as facts even where the caller supplied no governing
        scope. Returning an executable object for a scope nobody activated is
        exactly the silent promotion this object exists to prevent, so the answer
        is None until a scope is requested and validated.

        Args:
            context: The run's context, for ``context`` and ``constant`` rules.
            secrets: Named secrets the declaration's rules may use.

        **BOTH ARE PASSED STRAIGHT THROUGH, AND NEITHER IS OWNED HERE.** They are
        runtime dependencies supplied by whatever trusted thing is running the
        load. This object hydrates the persisted half -- the declaration -- and
        never holds, resolves or defaults a secret. An ``encrypt`` rule's
        ``key_env`` is a NAME and travels in the declaration like any other key;
        resolving it against a map is the transform's, with the map from the
        caller.

        Returns:
            The transform, carrying the identities an edit is written back by,
            or None where no active governing scope or no stored definition
            exists. **An empty declaration is never invented**: one would pass
            records through and look like success.
        """
        if not self.governing_scope_available or self.transform_id is None:
            return None

        columns: list[dict[str, Any]] = []
        column_ids: list[Optional[int]] = []
        for row in self._transform_columns:
            columns.append(_column_declaration(row))
            column_ids.append(_as_int(row["transform_column_id"]))

        declaration: dict[str, Any] = {"columns": columns}
        if self._row_filter:
            declaration["row_filter"] = self._row_filter

        return ColumnTransform(
            declaration,
            context=context,
            secrets=secrets,
            # THE IDENTITIES TRAVEL WITH IT. transform_id names the definition,
            # each transform_column_id names one column rule, and file_type_id
            # is the scope an edit persists against. Dropped here, they could
            # only be recovered by reading the database again -- which is what
            # the one-call contract exists to make unnecessary.
            # persisted_file_type_id cannot be None here: a governing scope was
            # supplied, and _validate_against refused it unless it matched.
            persistence=TransformPersistence(
                transform_id=self.transform_id,
                file_type_id=int(self.persisted_file_type_id),
                column_ids=tuple(column_ids),
            ),
        )

    # -- the canonical objects, populated ------------------------------------

    def governed_context(self) -> dict[str, Any]:
        """The governed facts a canonical Source holds as read-only context.

        The file's identities and its name: what it IS in the estate, never
        what a load is configured to do with it.
        """
        return {
            "file_manifest_id": self.file_manifest_id,
            "file_mutation_id": self.file_mutation_id,
            "installation_id": self.installation_id,
            "data_profile_id": self.data_profile_id,
            "file_name": self.file_facts.get("file_name"),
            "mutation_choices": [dict(one) for one in self.mutation_choices],
            "transform_query_id": self.transform_query_id,
        }

    def populate(self, source: Any, transform: Any) -> None:
        """Hydrate one canonical Source and Transform from this governed file.

        **THE ONE POPULATOR, for every entry point.** The Console, a workflow
        step and the CLI hand their own instances here rather than each reading
        the context its own way.

        The Source is POPULATED AS AN ORDINARY SOURCE: it selects database, with
        the SQL the database resolved as its statement and the working
        mutation's path as its file, and holds the identities the contract
        resolved -- manifest, working mutation, and the governing type where one
        is active -- as its own configuration. It is then told its
        governed context, a fact about the file and never configuration. The
        Transform is given the stored definition as its
        Manifest configuration ONLY where the definition is its type's default
        and a governing scope is active; otherwise it is left exactly as it was.
        (TEMPORARY: the default stands in for the selection until Saved Settings
        -- loader_saved_settings_selects_transform_configuration -- replaces
        this gate with the transform actually selected.)
        Nothing is created and the Target is not touched.

        Args:
            source: The canonical Source to tell its governed context.
            transform: The canonical Transform to configure.
        """
        # THE SOURCE IS POPULATED FROM THE RESOLVED CONTEXT: the kind, and the
        # identities the contract resolved -- the working mutation, and the
        # governing type where one is active. Its own configuration, not context.
        source.select("database")
        source.update("statement", self.resolved_query_sql or "")
        source.update("file", self.path or "")
        source.update("file-manifest-id", str(self.file_manifest_id))
        source.update("file-mutation-id", str(self.file_mutation_id))
        if self.requested_file_type_id is not None:
            source.update("file-type-id", str(self.requested_file_type_id))
        # LAST: filling the identities clears context about any earlier file, so
        # this file's facts are told once its identities are in place.
        source.observe_governed_context(self.governed_context())
        built = self.column_transform() if self.transform_is_default else None
        if built is None:
            return
        # THE DECLARATION KIND, in its own JSON form: the governed mapping is an
        # ordinary declaration, with its stored facts beside it.
        transform.select("declaration")
        transform.update("transform", json.dumps(deepcopy(dict(built.declaration)), indent=2))
        # THE PERSISTED FACTS, held beside the declaration and never inside it:
        # each column's row id and its stored ordinal, aligned to the entries.
        transform.update("persistence", {
            "transform_id": built.persistence.transform_id,
            "file_type_id": built.persistence.file_type_id,
            "column_ids": list(built.persistence.column_ids),
            "column_ordinals": [
                _as_int(row["column_ordinal"]) for row in self._transform_columns
            ],
        })


#: File facts the contract returns, carried as read-only context.
#:
#: Named rather than "every column that is not something else": a list that
#: absorbed whatever arrived would silently acquire meaning from a contract
#: change nobody read.
_FILE_FACTS: tuple[str, ...] = (
    "base_path", "file_name", "file_extension", "checksum_sha256",
    "size_bytes", "source_name", "record_type", "action", "status",
    "result", "mutated_ts", "classification", "is_classified",
    "file_type_key", "file_type_name", "signature", "type_header_definition",
    "key_fields", "filename_match", "data_profile_key", "profile_row_count",
)


def _column_declaration(row: Mapping[str, Any]) -> dict[str, Any]:
    """One stored column rule, as the declaration ColumnTransform reads.

    Args:
        row: One context row carrying a transform column.

    Returns:
        The declaration entry: ``name``, ``source``, ``datatype``, ``export``
        and the ``transform`` rule.
    """
    entry: dict[str, Any] = {
        "name": str(row["column_name"]),
        "source": row["source_column"],
        # THE OPERATOR COMES FROM THE COLUMN, NOT THE JSON, and that spread
        # order is the rule. transform_type is NOT NULL in the store and is the
        # operator; transform_config is nullable and holds the rule's other
        # keys. Written the other way round, a `type` inside the JSON would
        # silently override the answer the schema guarantees.
        "transform": {
            **(dict(row["transform_config"]) if row["transform_config"] else {}),
            "type": str(row["transform_type"]),
        },
    }
    if row["column_datatype"]:
        entry["datatype"] = row["column_datatype"]
    # ABSENT MEANS EXPORTED, which is what `is_exported` already means. Only a
    # stored False is stated, so nothing changes for a column that never had an
    # opinion.
    if row["column_is_exported"] is False:
        entry["export"] = False
    return entry


def _dedupe(
    rows: Sequence[Mapping[str, Any]],
    id_column: str,
) -> list[Mapping[str, Any]]:
    """The distinct rows for one child family, in the order they arrived.

    **The repetition is the contract's agreed shape, not a defect.** Profile
    fields and transformation columns are independent one-to-many children, so
    17 fields over 3 columns arrives as 51 rows and is 17 fields and 3 columns.

    Deduplicated BY DATABASE ID rather than by name or ordinal: those are
    mutable, and two fields may legitimately share an ordinal.

    Args:
        rows: The contract's result.
        id_column: The child's identity column.

    Returns:
        One row per distinct child, excluding rows where the child is absent.
    """
    seen: set[int] = set()
    kept: list[Mapping[str, Any]] = []
    for row in rows:
        identity = _as_int(row[id_column])
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        kept.append(row)
    return kept


def _in_column_order(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Transform column rows by their stored ``column_ordinal``, absent ones last.

    STABLE, and on that key alone: rows with an equal or no ordinal keep the
    order the reader returned them in. A second sort key would be a second
    answer to what the order is.
    """
    return sorted(rows, key=lambda row: (
        row["column_ordinal"] is None, _as_int(row["column_ordinal"]) or 0,
    ))


def _as_int(value: Any) -> Optional[int]:
    """The value as an int, or None where it is absent.

    None is preserved rather than becoming 0: an absent file type, profile or
    transform is an ordinary state here, and a zero would be an identity that
    does not exist.
    """
    return None if value is None else int(value)

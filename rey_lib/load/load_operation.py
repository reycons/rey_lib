"""One data object into another, through one transform.

    DataObject -> TransformObject -> DataObject

**THE TRANSFER BOUNDARY IS ``_load_one_file``**, and the three domain inputs
it takes are the whole contract:

    source     what is being read
    transform  what the records are
    target     where they go

``source`` and ``target`` are ROLES in one operation, not types. Nothing here
asks which side a data object is on in order to know what it is, and no
connection crosses the boundary -- one is opened below it, from
``target.connection``, where a database is first actually needed.

**Why this is not under ``files/``.** It was, for as long as the only thing
ever loaded was a file, and the architecture said so in as many words. That
made one endpoint family privileged: a database source could only be added by
widening a package whose name already asserted the answer. The families are
peers now --

    rey_lib/data/   the contracts both answer
    rey_lib/files/  file-backed data objects, and the file pipeline
    rey_lib/db/     database-backed data objects, and the write mechanics

-- and this coordinates between them. It reaches into all three; none of them
reaches back.

**What it does not own.** Not the formats: a source decides how it is read.
Not the movements: a file's routing is ``files``'s, called from here. Not the
insert: that is the destination mechanic's. What is here is the ORDER those
things happen in, and which execution path a load can take.

Public API
----------
load_files(ctx, run_log, data_source, load_cfg)
    Every pending file for one configured load definition.
load_one(ctx, run_log, data_source, load_cfg, file_path)
    Exactly one file, through its definition.
load_file_to_table(ctx, run_log, file_path, destination, connection)
    One file into one table, with no configuration at all.
load_query_to_table(ctx, run_log, statement, source_connection, destination,
                    connection)
    What one statement returns into one table. The database sibling of the
    above -- both ends name their own configured connection.
run_load(ctx, run_log, sql_dir)
    Every configured load, then the SQL that follows them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rey_lib.data.column_transform import ColumnTransform
from rey_lib.data.data_transform import IdentityTransform
from rey_lib.data.errors import DataStructureError
from rey_lib.db.connection import shared_connection
from rey_lib.db.data_loader import DataLoader as _DataLoader
from rey_lib.db.data_loader import adapter_destination
from rey_lib.db.database_objects import DatabaseObjectIdentity
from rey_lib.db.db_adapter import DBAdapter
from rey_lib.db.procedure_map import execute_procedure_call, execute_sql_text
from rey_lib.db.query_source import QuerySource
from rey_lib.errors.error_utils import ConfigError, DatabaseError
from rey_lib.files.data_file import data_file_for
from rey_lib.files.data_file.base import DataFile
from rey_lib.files.file_loader import (
    build_secrets,
    execute_movements,
    log_loader_step_failure,
    namespace_to_plain,
    resolve_ctx_path,
    resolve_path,
    resolve_pattern,
)
from rey_lib.load.configured_load import ConfiguredLoad as _ConfiguredLoad
from rey_lib.logs.log_utils import (
    get_logger,
    log_enter,
    log_exit,
    log_input_discovered,
    log_row_count,
    log_validation_result,
)

__all__ = [
    "load_file_to_table",
    "load_files",
    "load_one",
    "load_query_to_table",
    "run_load",
]

_logger = get_logger(__name__)

# TWO ADAPTER ROLES, and they are not one shared instance.
#
#     QuerySource             -> a READ adapter, over the source connection
#     DataLoader / target     -> a WRITE adapter, over the target connection
#
# The same concrete class serves both -- it dispatches on each connection
# config's provider, so this module never imports a backend driver -- but the
# roles are distinct by contract. A source reads through its own; a
# destination is written through the loader's. Handing one object to both
# would make a source's read depend on what the destination happens to be,
# which is exactly backwards for endpoints that may sit on different
# connections and different providers.
_write_adapter = DBAdapter()
_read_adapter = DBAdapter()

# Matches {ctx.attr} and {data_source.attr} tokens in LLM prompt templates.
_PROMPT_TOKEN_RE = re.compile(r"\{(ctx|data_source)\.([^}]+)\}")

# Buffer added to the observed max length when widening a string column,
# so a one-character overflow doesn't immediately trigger another resize.
_COLUMN_EXPAND_BUFFER = 10

# Pre-compiled regexes for the only two type families the library knows
# how to widen automatically. Any other declared type is skipped -- the
# app must size those correctly up front.
_WIDENABLE_TYPE_PATTERNS = (
    ("NVARCHAR", re.compile(r"^NVARCHAR\((\d+)\)$")),
    ("VARCHAR",  re.compile(r"^VARCHAR\((\d+)\)$")),
)


def load_files(
    ctx: Any, run_log,
    conn: Any,
    data_source: Any,
    load_cfg: Any,
) -> int:
    """
    Find and load all pending files for one load configuration.

    Scans the configured source path for files matching the pickup_pattern,
    then loads each one independently. A failure on one file does not
    prevent processing of subsequent files.



    Parameters
    ----------
    ctx : Any

    conn : Any
        Open backend connection. Caller manages the connection lifecycle.
    data_source : Any
        Namespace for one data_sources entry from ctx. Provides paths and
        transforms list.
    load_cfg : Any
        Namespace for one loads entry. Provides source path key,
        pickup_pattern, version, load destination, and movements.

    Returns
    -------
    int
        Total number of rows successfully loaded across all files.
    """
    log_enter(ctx, f"load_files: {data_source.name} / {load_cfg.name}", _logger)
    total_rows = 0

    try:
        total_rows = _configured_load(ctx, run_log, data_source, load_cfg).load(
            conn, run_log
        )

    finally:
        log_exit(
            ctx,
            f"load_files done: {total_rows} total row(s) loaded",
            _logger,
        )

    return total_rows

def _route_file(
    ctx: Any,
    run_log: Any,
    movements: Any,
    outcome: str,
    source: Any,
    paths: Any,
) -> None:
    """Move a file according to this load's policy, where it has one.

    **THIS is a point that knows it is handling a file.** It takes the source
    data object and reads a path from it here, rather than the boundary
    fetching one for every load and handing it down. A source whose primitive
    is not a file has nothing to move and nowhere to move it.

    **A DIRECT load has no movement policy, and that is not the same as an
    empty one.** Nothing was picked up from an inbox, so there is nowhere to
    move the file to and no archive it belongs in -- the caller named a path
    and expects it left alone.

    ``movements is None`` therefore means "do not route", and is why the
    direct path needs no manufactured ``load_cfg`` carrying empty lists.

    Args:
        outcome: ``"success"`` or ``"failure"`` -- which movement list to run.
    """
    if movements is None:
        return

    # A source with no path is not routed. Routing moves a FILE between
    # configured folders; there is no such act for a source whose primitive is
    # a connection and a statement, and inventing one would be routing
    # nowhere rather than not routing.
    file_path = getattr(source, "path", None)
    if file_path is None:
        return

    # An EMPTY list still goes through. A configured load declaring no moves
    # for this outcome has a policy that moves nothing, which is not the same
    # as having no policy -- and keeping the call means a configured load
    # behaves exactly as it did before movements became a resolved value.
    execute_movements(
        getattr(movements, outcome, []), file_path, paths,
        ctx=ctx, run_log=run_log,
    )

def _build_transform(
    ctx: Any,
    transform_cfg: Any = None,
    declaration: Any = None,
) -> Any:
    """Build the transform object for one load.

    **THE SINGLE PLACE A LOAD'S TRANSFORM IS CHOSEN**, and the choice is one
    question: was this load TOLD what to do with its columns?

        a declaration     ColumnTransform, which applies it
        none              IdentityTransform, which passes records through

    Identity is not a fallback for a missing feature. A configured file feed's
    transform stage has already run by the time the load reads what it wrote,
    so there really is nothing left to apply -- and an ad-hoc load that names
    no transformation is asking for its records as they are.

    Secrets are resolved HERE rather than inside the object: a declaration
    names an environment variable and something trusted reads it, which is the
    same `build_secrets` the transform stage uses. One resolver, so an ad-hoc
    load cannot quietly end up with none.

    Args:
        ctx: Application context, for context values a rule may reference.
        transform_cfg: A configured definition's transform, where there is one.
        declaration: A transform declaration given with the invocation.

    Returns:
        The transform this load runs.
    """
    if declaration:
        plain = namespace_to_plain(declaration) or {}
        return ColumnTransform(
            plain, context=ctx, secrets=build_secrets(plain),
        )
    return _build_identity_transform(transform_cfg)


def _build_identity_transform(transform_cfg: Any) -> IdentityTransform:
    """Build the transform for one load definition.

    **The single place an IdentityTransform is constructed.** Both callers
    are ``ConfiguredLoad`` construction sites, so what they produce cannot
    drift.

    ``transform_cfg`` may be None -- a DIRECT load has no configuration at
    all. Every read below is a ``getattr``, so absent configuration yields no
    transform map and no declared columns, which is what that load means.
    Passing a manufactured config object instead would be a stand-in for a
    definition that does not exist.

    Reads the configured columns, which is where an unreadable ``columns:``
    shape is refused rather than treated as absent.
    """
    return IdentityTransform(
        _transform_map(transform_cfg),
        columns=_configured_columns(transform_cfg),
    )

def _destination_identity(
    destination_table: str,
    connection: str,
) -> DatabaseObjectIdentity:
    """Resolve one configured destination into the data object it names.

    **THE TARGET DATA OBJECT.** Past this point the destination is an object
    rather than a path through strings, and nothing downstream re-parses it.

    The parts are kept APART, which is the difference from
    ``_parse_destination``: that one joins a three-part name into the single
    schema argument the adapter takes, and it still does, for the callers that
    hand strings straight to the adapter. An identity carries no rendered
    reference, so the join happens where the adapter is actually called --
    ``data_loader.adapter_destination``.

        'schema.table'             -> catalog '',         schema 'schema'
        'database.schema.table'    -> catalog 'database',  schema 'schema'

    Args:
        destination_table: ``schema.table``, or ``database.schema.table``.
        connection: The CONFIGURED CONNECTION NAME this object lives on.
            Required, and not defaulted: an identity without one is a partial
            endpoint, which is what passing objects exists to stop. Every
            construction site has it -- a direct load is given it, and a
            configured load reads ``load.connection``.

    Returns:
        The identity. The connection is named, never resolved here: opening it
        belongs to whoever runs the load.

    Raises:
        ValueError: When the destination names no schema, or when no
            connection is given.
    """
    name = str(destination_table or "")
    parts = name.split(".")
    if len(parts) < 2:
        raise ValueError(
            f"destination_table '{name}' must be at least 'schema.table' — "
            f"got {len(parts)} part(s)."
        )
    if not str(connection or "").strip():
        raise ValueError(
            f"destination '{name}' needs the configured connection it lives "
            f"on; a database object identity without one is incomplete."
        )
    return DatabaseObjectIdentity(
        connection=str(connection).strip(),
        catalog=".".join(parts[:-2]),
        schema=parts[-2],
        name=parts[-1],
    )

def _build_data_loader(
    ctx: Any,
    create_declared: bool,
    replace_declared: bool = False,
) -> _DataLoader:
    """Build the loader for one load's policy.

    **The single place a DataLoader is constructed on this path**, so the
    definition-level build and the single-file build cannot differ.

    **No destination.** Which object is written to is the target data object's
    and arrives per call, so this builds POLICY only -- what the load may do,
    which is not a property of the object it writes to.

    The widening callback is bound here because widening is CONFIGURED
    behaviour -- a declared routine, never inline DDL -- and its parameters
    travel through ``ctx``. This is the boundary that still holds ``ctx``; the
    loader never learns how widening is configured. The destination reaches it
    per call now, rather than being closed over, for the same reason it is no
    longer held.
    """
    return _DataLoader(
        adapter=_write_adapter,
        create_destination=create_declared,
        replace_destination=replace_declared,
        widen_columns=(
            lambda _conn, target, records, defs: _alter_oversized_columns(
                ctx, *adapter_destination(target), records, defs,
            )
        ),
    )

def _configured_load(
    ctx: Any,
    run_log: Any,
    data_source: Any,
    load_cfg: Any,
    explicit_files: Optional[list[Path]] = None,
) -> _ConfiguredLoad:
    """Resolve one load definition into the object that runs it.

    **This is the construction boundary.** Everything configuration has to say
    is read here -- where files come from, what they are called, which
    transform describes them, which table they go to, whether it may be
    created -- and nothing below re-reads it.

    ``ctx`` is consumed here for resolution. It still travels into the
    per-file step, which needs it for run logging and file movements; those
    are not configuration.
    """
    transform_cfg = _find_transform(
        data_source.transforms, load_cfg.name, load_cfg.version,
    )
    # THE TARGET DATA OBJECT, resolved once where configuration is read. The
    # connection it names comes from the same load block that has always
    # named it, so nothing downstream reads that name again.
    target = _destination_identity(
        load_cfg.load.destination_table,
        getattr(getattr(load_cfg, "load", None), "connection", "") or "",
    )
    create_declared = _create_destination_declared(load_cfg)
    source_config = f"{data_source.name}.{load_cfg.name}"

    def _record_discovered(file_path: Path) -> None:
        log_input_discovered(run_log,
            input_name=file_path.name,
            path=str(file_path),
            pattern=resolve_pattern(load_cfg.pickup_pattern, load_cfg.version,
                                     ctx=ctx),
            source_config=source_config,
            exists=True,
            safe_to_preview=True,
            size_bytes=file_path.stat().st_size if file_path.exists() else None,
        )

    def _load_one(source: Any, transform: Any, target: Any,
                  **context: Any) -> int:
        # The definition's own source, transform and target arrive here; this
        # step wraps them in movements and evidence and builds nothing of its
        # own.
        return _load_one_file(
            source, transform, target,
            ctx=ctx,
            transform_cfg=transform_cfg,
            load_cfg=load_cfg,
            paths=data_source.paths,
            **context,
        )

    return _ConfiguredLoad(
        source_dir=resolve_path(data_source.paths, load_cfg.source, ctx=ctx),
        pattern=resolve_pattern(load_cfg.pickup_pattern, load_cfg.version,
                                 ctx=ctx),
        load_one_file=_load_one,
        transform=_build_identity_transform(transform_cfg),
        loader=_build_data_loader(ctx, create_declared),
        target=target,
        file_type=getattr(transform_cfg, "file_type", "CSV"),
        encoding=getattr(transform_cfg, "encoding", "utf-8-sig"),
        max_files=getattr(data_source, "max_files_per_run", None),
        name=source_config,
        on_discovered=_record_discovered,
        explicit_files=explicit_files,
    )

def load_file_to_table(
    ctx: Any,
    run_log: Any,
    file_path: Path,
    destination: str,
    connection: str,
    *,
    create_destination: bool = False,
    replace_destination: bool = False,
    file_type: str = "",
    encoding: str = "utf-8-sig",
    transform: Any = None,
) -> int:
    """Load one named file into one named table. No configuration at all.

    The direct answer to "load this file into this table over this
    connection", which for a long time could not be given without first
    authoring a ``data_sources:`` block.

    **It builds the same object graph a configured feed uses** -- a
    ``DataFile`` for the format, an ``IdentityTransform``, a ``DataLoader``
    for the destination -- and runs the same pipeline. It is a different way
    of CONSTRUCTING the graph, not a second implementation of it.

    What a direct load does not have, and does not pretend to:

    - **no configured columns**, so the schema is inferred from the records;
    - **no movements**, because nothing was picked up from an inbox and the
      caller's file is left where they put it;
    - **no transform**, because there is no conversion step ahead of it.

    Args:
        ctx: Application context, for logging and any configured widening.
        run_log: The run's evidence recorder.
        file_path: The file to load.
        destination: ``schema.table``, or ``database.schema.table`` where the
            backend qualifies that way.
        connection: The CONFIGURED CONNECTION NAME the destination lives on.
            A name, not a handle: the target is built from it and the handle
            is opened from the target, so one value says where the object is
            and nothing reads it twice. Required -- an identity without a
            connection is a partial endpoint.
        replace_destination: Whether the destination's existing contents are
            removed before these records are written. False adds to them,
            which is what a load has always done.
        create_destination: Whether an absent table may be created from the
            file. False means it must already exist.
        file_type: Declared format. Empty infers it from the suffix, which
            REFUSES a suffix naming no known format -- which is why this
            parameter exists.
        encoding: How to decode the file. A library parameter with the
            estate's default; no CLI exposes it.

    Returns:
        Rows loaded.
    """
    target = _destination_identity(destination, connection)
    load_name = ".".join(
        part for part in (target.catalog, target.schema, target.name) if part
    )

    def _load_one(source: Any, transform: Any, target_: Any,
                  **context: Any) -> int:
        # movements=None: a direct load has no movement policy, which is not
        # the same as one that moves nothing.
        return _load_one_file(
            source, transform, target_,
            ctx=ctx, movements=None, load_name=load_name, **context,
        )

    return _ConfiguredLoad(
        load_one_file=_load_one,
        target=target,
        explicit_files=[Path(file_path)],
        # Through the builder, which is the ONE place a load's transform is
        # chosen. No declaration means identity: a direct load has no
        # configuration, and absent configuration produces no transform map
        # and no declared columns -- exactly what such a load means.
        transform=_build_transform(ctx, None, transform),
        loader=_build_data_loader(ctx, create_destination, replace_destination),
        file_type=file_type,
        encoding=encoding,
        name=f"direct:{load_name}",
    ).load(run_log)


def load_query_to_table(
    ctx: Any,
    run_log: Any,
    statement: str,
    source_connection: str,
    destination: str,
    connection: str,
    *,
    create_destination: bool = False,
    replace_destination: bool = False,
    transform: Any = None,
) -> int:
    """Load what one statement returns into one named table.

    The database sibling of ``load_file_to_table``, and the construction site
    for a load that begins at a database rather than a file:

        QuerySource -> IdentityTransform -> DatabaseObjectIdentity

    **It does not go through ``ConfiguredLoad``.** That object is a load
    DEFINITION built around picking files up -- ``select_files``,
    ``data_file_for``, ``input_files`` -- and a query is one source with
    nothing to discover. Passing a statement through it would mean calling a
    query a one-element file list, which is the flattening the boundary just
    stopped doing. So this reaches the transfer boundary directly, which is
    the same boundary every file load crosses.

    **BOTH ENDS NAME THEIR OWN CONNECTION, and they may differ.** The target's
    is resolved inside the boundary from ``target.connection``, where a
    database is first needed; the source's is resolved here, because a source
    must be readable before there is anything to transfer. Each is a
    CONFIGURED NAME rather than a handle, and ``shared_connection`` answers
    both from the one registry -- so naming the same connection twice yields
    the same object rather than a second one.

    What a direct query load does not have, and does not pretend to:

    - **no movements**, because nothing was picked up from anywhere and there
      is no file to route;
    - **no configured columns**, so the destination schema is inferred from
      the records;
    - **no transform**, because there is no conversion step ahead of it.

    Args:
        ctx: Application context, for logging and any configured widening.
        run_log: The run's evidence recorder.
        statement: The query to read. Held as written -- nothing here parses
            it, rewrites it, or decides what it means.
        source_connection: The CONFIGURED CONNECTION NAME the statement runs
            on.
        destination: ``schema.table``, or ``database.schema.table`` where the
            backend qualifies that way.
        connection: The CONFIGURED CONNECTION NAME the destination lives on.
        replace_destination: Whether the destination's existing contents are
            removed before these records are written. False adds to them,
            which is what a load has always done.
        create_destination: Whether an absent table may be created from the
            records. False means it must already exist.
        transform: A transform declaration. Absent means identity -- the rows
            are loaded as the query returned them.

    Returns:
        Rows loaded.
    """
    target = _destination_identity(destination, connection)
    load_name = ".".join(
        part for part in (target.catalog, target.schema, target.name) if part
    )
    source = QuerySource(
        shared_connection(ctx, source_connection).handle(),
        statement,
        # ITS OWN READ ADAPTER. Not the loader's: a source reads on the source
        # connection, and which provider answers there has nothing to do with
        # where the rows are going.
        adapter=_read_adapter,
    )

    return _load_one_file(
        source,
        # Through the builder rather than around it: ONE place a load's
        # transform is chosen. No declaration means identity, which is what a
        # load asking for its records as they are means.
        _build_transform(ctx, None, transform),
        target,
        ctx=ctx,
        run_log=run_log,
        loader=_build_data_loader(ctx, create_destination, replace_destination),
        # None, not an empty policy. A load with no file has nothing to route,
        # which is not the same as a policy that routes nothing.
        movements=None,
        load_name=f"query:{load_name}",
    )


def load_query_to_file(
    ctx: Any,
    run_log: Any,
    statement: str,
    source_connection: str,
    out_file: str,
    *,
    transform: Any = None,
) -> int:
    """Load what one statement returns into one file.

    The same construction as ``load_query_to_table`` with the other end
    swapped, which is the whole point of the target being a role:

        QuerySource -> IdentityTransform -> DataFile

    **ONE CONNECTION, and it is the SOURCE's.** A file destination has none,
    so this takes none -- rather than accepting one and ignoring it, which
    would leave a reader believing the file was written somewhere.

    **THE FORMAT COMES FROM THE PATH, and there is no second way to say it.**
    ``data_file_for`` reads the suffix and refuses by name when it names no
    known format, which is the same resolver every file source goes through.
    Nothing here lists a suffix, and no declared type is accepted: the one
    the estate has -- ``file_type`` -- describes a data file being READ, and
    giving a second meaning to a name that already has one is how a parameter
    comes to mean "roughly format".

    What a query-to-file load does not have, for the same reasons the
    query-to-table one does not: no movements, no configured columns, no
    transform.

    Args:
        ctx: Application context, for logging.
        run_log: The run's evidence recorder.
        statement: The query to read, held as written.
        source_connection: The CONFIGURED CONNECTION NAME it runs on.
        out_file: Where the rows go. Its suffix names the format.
        transform: A transform declaration. Absent means identity -- the rows
            are written as the query returned them.

    Returns:
        Rows written.
    """
    target = data_file_for(out_file)
    source = QuerySource(
        shared_connection(ctx, source_connection).handle(),
        statement,
        # ITS OWN READ ADAPTER, exactly as the table sibling's is. Which
        # provider answers on the source connection has nothing to do with
        # where the rows are going -- and here there is no provider at the
        # other end at all.
        adapter=_read_adapter,
    )

    return _load_one_file(
        source,
        _build_transform(ctx, None, transform),
        target,
        ctx=ctx,
        run_log=run_log,
        # NO LOADER. That is the database's destination mechanic, and this
        # destination is its own; the boundary asks the target which it is.
        movements=None,
        load_name=f"query:{target.path.name}",
    )


@dataclass(frozen=True)
class LoadEndpoints:
    """What each end of a prospective load is made of.

    Ordered column lists and nothing else. Not a mapping: how they correspond
    is the caller's to propose, and a correspondence a reader has authored is a
    different thing again -- an object nobody has built yet.

    Any of them may be empty, which means that end is not named yet rather than
    that it has no columns. A load being set up is half-named most of the
    time.
    """

    #: What the load SEES: the transform's output over the source's structure.
    #:
    #: Named `source` because that is what it was when the only transform was
    #: identity and the two were the same list. They are not the same once a
    #: declaration renames anything, which is what `source_structure` is for.
    source: tuple[str, ...] = ()
    destination: tuple[str, ...] = ()
    #: The source's OWN field names, before the transform.
    #:
    #: A SECOND ANSWER, because they are two questions and only looked like one
    #: while every transform was identity. A surface authoring a declaration
    #: needs the names it is mapping FROM: reading `source` for them would show
    #: the output name a reader had just typed and call it the source's own.
    source_structure: tuple[str, ...] = ()


def inspect_load_endpoints(
    ctx: Any,
    *,
    file: str = "",
    file_type: str = "",
    statement: str = "",
    source_connection: str = "",
    destination: str = "",
    connection: str = "",
    out_file: str = "",
    transform: Any = None,
) -> LoadEndpoints:
    """What the two ends of this load are made of, WITHOUT running it.

    **THE SAME OBJECTS, ASKED INSTEAD OF USED.** This builds exactly what the
    load entry points build from exactly the arguments they take -- the same
    ``data_file_for``, the same ``QuerySource``, the same identity transform
    -- and then asks each end for its structure. It inserts nothing, writes
    nothing and creates nothing.

    That is the whole of why it is here rather than in a surface. A second
    place that worked out "what columns will this load see" would be a second
    answer, free to disagree with the one the load acts on, and the
    disagreement would surface as a load that did something other than what
    the screen showed.

    A DESTINATION HAS TWO ANSWERS, and which one applies is a property of the
    destination rather than a choice:

    - a table that EXISTS answers with the columns it has, because those are
      what the insert must match;
    - a destination that does not exist yet -- a file, or a table under
      ``--create`` -- answers with the columns this load WOULD write. It has
      no physical structure to read, but the load is not ignorant of where
      the values go, and saying nothing would claim it was.

    The second answer is ``transform.columns_for_names(...)`` over the
    source's declared structure: the same call the non-row path makes, which
    states that rule once for every caller that has no records in hand.

    Args:
        ctx: Application context, for opening a named connection.
        file: A file source, by path.
        file_type: Its declared format, where the suffix does not name one.
        statement: A query source, as written.
        source_connection: The configured connection the statement runs on.
        destination: A table destination, as ``schema.table``.
        connection: The configured connection that table lives on.
        out_file: A file destination, by path.
        transform: The transform declaration in force, which decides what the
            produced columns ARE -- and therefore what a destination that does
            not exist yet would be given.

    Returns:
        The two ends, and the source's own structure beside them. An end that
        is not named yet is empty.

    Raises:
        ConfigError: As the load would, when a named end cannot be read.
            A half-named load is not an error; an unreadable one is.
    """
    source = _inspection_source(ctx, file, file_type, statement, source_connection)
    produced: tuple[str, ...] = ()
    structure: tuple[str, ...] = ()
    if source is not None:
        # READ ONCE and answered twice. The source's own structure is what a
        # declaration maps FROM; the produced columns are what the load sees.
        structure = tuple(source.source_structure())
        # The TRANSFORM'S answer, not the source's: what a load sees is what
        # the transform produces from what the source declares, and today's
        # identity transform makes those the same. Reading it off the source
        # directly would be right by coincidence and wrong the moment a
        # transform declares columns.
        produced = tuple(
            _build_transform(ctx, None, transform).columns_for_names(
                list(structure)
            )
        )

    return LoadEndpoints(
        source=produced,
        destination=_inspection_destination(
            ctx, destination, connection, out_file, produced,
        ),
        source_structure=structure,
    )


#: How many records a preview shows unless a caller says otherwise.
#:
#: A number, because "what is being exported" is a question about SHAPE and
#: content, not about volume -- and a surface that answered it with the whole
#: source would make looking at a load as expensive as running one.
_PREVIEW_ROWS = 50


@dataclass(frozen=True)
class LoadPreview:
    """What this load would carry, as far as anyone needs to see it.

    The columns are the produced ones -- what the transform makes from what
    the source declares -- so they are the same names
    ``inspect_load_endpoints`` reports, from the same call. The rows are what
    would actually be written, which is why they are taken AFTER the
    transform rather than as the source returned them.

    ``truncated`` says the source held more. Stated rather than left to be
    inferred from a row count matching the limit, which is also what a source
    holding exactly that many looks like.
    """

    columns: tuple[str, ...] = ()
    rows: tuple[dict[str, Any], ...] = ()
    truncated: bool = False


def preview_load(
    ctx: Any,
    *,
    file: str = "",
    file_type: str = "",
    statement: str = "",
    source_connection: str = "",
    limit: int = _PREVIEW_ROWS,
    declaration: Any = None,
) -> LoadPreview:
    """The first records this load would carry, WITHOUT carrying them.

    **EXACTLY WHAT WOULD BE EXPORTED.** The rows are taken through the same
    transform the load applies, so what a reader sees is what would be
    written rather than what the source happens to hold -- today those are
    the same, because the transform is identity, and the day one is not this
    still shows the right thing.

    **NO DESTINATION.** A preview is about what is leaving, not about where
    it lands: it writes nothing, creates nothing and validates against
    nothing. Refusing a source that does not match its destination is the
    LOAD's job, and doing it here would mean a reader could not look at a
    mismatch in order to fix it.

    Bounded by ``sample(limit)``, which every source answers and which is
    deliberately not ``read()`` -- see either implementation for why those
    must not converge.

    Args:
        ctx: Application context, for opening a named connection.
        file: A file source, by path.
        file_type: Its declared format, where the suffix does not name one.
        statement: A query source, as written.
        source_connection: The configured connection the statement runs on.
        limit: How many records at most.
        declaration: The transform declaration in force. What is previewed is
            what would be EXPORTED, so the records are shown through it.

    Returns:
        The produced columns and the records, or nothing at all where no
        source is named yet.

    Raises:
        ConfigError: As the load would, when a named source cannot be read.
    """
    source = _inspection_source(ctx, file, file_type, statement, source_connection)
    if source is None:
        return LoadPreview()

    transform = _build_transform(ctx, None, declaration)
    # ONE MORE THAN ASKED FOR, which is how "there is more" is established
    # without a second read. A count equal to the limit says nothing on its
    # own -- a source holding exactly that many looks identical.
    sampled = source.sample(limit + 1)
    truncated = len(sampled) > limit
    rows = transform.transform(sampled[:limit])

    return LoadPreview(
        # From the STRUCTURE, not from the rows: an empty source still has
        # columns, and a preview that showed none would look like a broken
        # query rather than an empty one.
        columns=tuple(
            transform.columns_for_names(list(source.source_structure()))
        ),
        rows=tuple(_for_display(row) for row in rows),
        truncated=truncated,
    )


def _for_display(row: dict[str, Any]) -> dict[str, Any]:
    """One record as the TEXT of each value, which is what a preview shows.

    **A PREVIEW IS A DISPLAY, and a load is not.** The load carries typed values
    to a destination that has a schema for them; a preview crosses to a browser
    over JSON and is read by a person. A database source hands back exactly what
    its driver holds -- ``datetime``, ``date``, ``Decimal`` -- and none of those
    survive ``json.dumps``, so a preview over a query failed outright with
    "Object of type datetime is not JSON serializable" and showed nothing.

    ONE RULE, not a table of types. Every value is its own text, and the types
    that broke it already spell themselves correctly: a ``datetime`` is
    ``2026-09-26 11:04:00``, a ``date`` is ``2026-09-26``, and a ``Decimal`` is
    ``1234.50`` with the scale the database is keeping on purpose -- which
    ``float`` would have rounded away. A branch per type would be a second
    account of how each one reads, free to drift from how it reads everywhere
    else.

    THIS IS NOT A SECOND FORMATTER. A column the transform touched arrives as
    text already -- ``_transform_date`` and its siblings render through
    ``output_format`` -- so its own formatting is what passes through here.

    NULL STAYS NULL, because it is absence rather than a value, and a surface
    draws a blank for it. ``str(None)`` would put the word "None" in the cell.

    The destination's declared ``datatype`` is deliberately not consulted: it
    says what the column should BE where it lands, which is a question about the
    load rather than about what a reader is looking at.
    """
    return {
        name: None if value is None else str(value)
        for name, value in row.items()
    }


def _inspection_source(
    ctx: Any,
    file: str,
    file_type: str,
    statement: str,
    source_connection: str,
) -> Any:
    """The source object this load would read, or None where none is named.

    The SAME two constructions the entry points make, in the same order the
    dispatch prefers them. Nothing is manufactured to stand in for an end
    nobody has named.
    """
    if statement.strip():
        return QuerySource(
            shared_connection(ctx, source_connection).handle(),
            statement,
            adapter=_read_adapter,
        )
    if file.strip():
        return data_file_for(file, file_type=file_type)
    return None


def _inspection_destination(
    ctx: Any,
    destination: str,
    connection: str,
    out_file: str,
    produced: tuple[str, ...],
) -> tuple[str, ...]:
    """The destination's columns: the ones it HAS, or the ones it WOULD get.

    A file has no physical structure until it is written, and a table under
    a create policy may not exist yet. Both are destinations the load knows
    the shape of, so both answer with what this load would put there.
    """
    if out_file.strip():
        # Resolved even though its columns come from the source: a path whose
        # suffix names no format is refused here, exactly as the load would
        # refuse it, rather than previewing a load that cannot run.
        data_file_for(out_file)
        return produced
    if not (destination.strip() and connection.strip()):
        return ()

    target = _destination_identity(destination, connection)
    existing = _build_data_loader(ctx, False).destination_columns(
        shared_connection(ctx, target.connection).handle(), target,
    )
    # None means ABSENT, and [] would mean a table with no columns -- the same
    # distinction the boundary draws. An absent table is one this load would
    # create, so it answers with what would be created.
    return tuple(existing) if existing is not None else produced


def load_one(ctx: Any, run_log, data_source: Any, load_cfg: Any, file_path: Path) -> int:
    """Load exactly one file into its destination table. No discovery, no hooks.

    Resolves the load connection, destination schema/table, and matching
    transform from config, then runs the existing single-file load. Returns the
    number of rows loaded. Fails closed on a missing/unknown connection.
    """
    from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

    conn_name = getattr(getattr(load_cfg, "load", None), "connection", None)
    if not conn_name:
        raise ConfigError(
            f"load.connection is not set for load '{getattr(load_cfg, 'name', '?')}' "
            f"in data source '{getattr(data_source, 'name', '?')}'."
        )
    # The connection name is CHECKED here and resolved nowhere here: the
    # target carries it, and the per-file step opens one from that. Resolving
    # a handle to pass down would be a second reading of the same name.
    #
    # Through a ConfiguredLoad like every other caller, so this is the same
    # object graph a discovered load runs -- one file selected explicitly
    # rather than found by pattern. Calling the per-file step directly would
    # be the second construction path this step exists to remove.
    return _configured_load(
        ctx, run_log, data_source, load_cfg, explicit_files=[file_path],
    ).load(run_log)

def run_load(
    ctx: Any, run_log,
    sql_dir: Optional[Path] = None,
) -> int:
    """
    Run the load stage for every data source and load config declared in ctx.

    Iterates ``ctx.data_sources`` and each ``loads`` entry. For each load
    config the connection named by ``load_cfg.load.connection`` is resolved
    from the shared connections and reused — no connection
    management in the caller. Connections are reused when multiple load
    configs within the same data source share the same connection name,
    and are all closed after that data source's ``post_load_sql`` files
    run.

    All behaviour is driven entirely by YAML config. No app-specific
    schema knowledge lives here: the library just bulk-inserts the rows
    that the transform produced. Per-file idempotency, dedup, or reload
    semantics belong in the calling application (or in the destination
    schema, via constraints).

    YAML shape expected per loads entry::

        - name:           my_load
          version:        "v01"
          source:         converted_path
          pickup_pattern: "file_*_{version}.csv"
          load:
            connection:          <db_connection_name>
            destination_table:   <database>.<schema>.<table>
          movements:
            success: ...
            failure: ...

    Parameters
    ----------
    ctx : Any
        Application context. Must have:
        - ``ctx.data_sources`` — iterable of data source Namespaces
        - ``ctx.connections`` — the configured connections, which the runtime
          connection owner resolves objects from by name

    sql_dir : Optional[Path]
        Base directory for resolving ``post_load_sql`` file names and
        ``type: sql_file`` hook configs. Pass ``None`` when no data
        source uses either.

    Returns
    -------
    int
        Total rows loaded across all data sources and load configs.

    Raises
    ------
    ConfigError
        If ``load.connection`` is missing or names an unknown connection.
    """
    from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

    total = 0

    for data_source in ctx.data_sources:
        object.__setattr__(ctx, "data_sources", data_source)
        # Cache open connections by name so multiple load configs that share
        # a connection only open it once per data source. Hook bindings reuse
        # this cache so they may share the load connection or open their own.
        open_conns: dict[str, Any] = {}
        last_conn: Any = None

        try:
            for load_cfg in getattr(data_source, "loads", []):
                object.__setattr__(ctx, "loads", load_cfg)

                conn_name = getattr(getattr(load_cfg, "load", None), "connection", None)
                if not conn_name:
                    raise ConfigError(
                        f"load.connection is not set for load '{load_cfg.name}' "
                        f"in data source '{data_source.name}'. "
                        "Add 'connection: <name>' under the load: section in YAML."
                    )

                if conn_name not in open_conns:
                    open_conns[conn_name] = shared_connection(ctx, conn_name).handle()
                    _logger.debug("Using shared connection '%s'", conn_name)

                conn = open_conns[conn_name]
                last_conn = conn
                rows = load_files(ctx, run_log, conn, data_source, load_cfg)
                total += rows

            # post_load_sql runs on the last connection used for this source
            # (backward-compat with existing post_load_sql YAML key).
            if last_conn is not None:
                _execute_post_load_sql(ctx, run_log, last_conn, data_source, sql_dir)

        finally:
            # Nothing is closed here. These are shared Connections held by every
            # other consumer of the same name; closing one at the end of a data
            # source would take the handle from all of them. Their lifetime
            # belongs to runtime shutdown.
            open_conns.clear()

    return total

def _execute_post_load_sql(ctx: Any, run_log, conn: Any, data_source: Any, sql_dir: Optional[Path]) -> None:
    """Execute each SQL file listed in data_source.post_load_sql.

    Skips silently when ``post_load_sql`` is absent, empty, or ``sql_dir``
    is ``None``.  Raises ``ConfigError`` when a declared file does not exist.
    Each file is executed as a single statement — use semicolons within the
    file to separate multiple statements where the driver supports it.

    Parameters
    ----------
    conn : Any
        Open database connection — must support ``conn.execute(sql)``.
    data_source : Any
        Data source Namespace; may have a ``post_load_sql`` list attribute.
    sql_dir : Optional[Path]
        Base directory for resolving SQL file names.
    """
    sql_files = getattr(data_source, "post_load_sql", None) or []
    if not sql_files or sql_dir is None:
        return

    for sql_filename in sql_files:
        sql_path = sql_dir / sql_filename
        if not sql_path.exists():
            raise ConfigError(
                f"post_load_sql file not found: {sql_path} "
                f"(data_source='{data_source.name}')"
            )
        sql_text = sql_path.read_text(encoding="utf-8")
        execute_sql_text(
            ctx,
            run_log, conn,
            sql_text,
            sql_path=str(sql_path),
            sql_label=sql_filename,
            operation="post_load_sql",
            safe_to_preview=True,
            data_source=getattr(data_source, "name", ""),
        )
        _logger.info("post_load_sql executed: %s", sql_filename)

def _find_sql_config(ctx: Any, name: str) -> Any:
    """
    Find a named sql_config entry in ctx.sql_configs.

    Parameters
    ----------
    ctx : Any
        Application context.  Must have ``ctx.sql_configs`` list attribute.
    name : str
        Name of the sql_config to locate.

    Returns
    -------
    Any
        The matching sql_config Namespace.

    Raises
    ------
    ConfigError
        If ``ctx.sql_configs`` is absent or the name is not found.
    """
    from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

    configs = getattr(ctx, "sql_configs", None)
    if configs is None:
        raise ConfigError(
            f"sql_config '{name}' referenced in hooks but ctx.sql_configs is not "
            "defined. Add a sql_configs section to your config YAML."
        )
    result = find_by_name(configs, name)
    if result is None:
        raise ConfigError(
            f"sql_config '{name}' not found in ctx.sql_configs. "
            "Check config/app/sql_configs.yaml."
        )
    return result

def _find_llm_config(ctx: Any, name: str) -> Any:
    """
    Find a named llm_config entry in ctx.llm_configs.

    Parameters
    ----------
    ctx : Any
        Application context. Must have ``ctx.llm_configs`` list attribute.
    name : str
        Name of the llm_config to locate.

    Returns
    -------
    Any
        The matching llm_config Namespace.

    Raises
    ------
    ConfigError
        If ``ctx.llm_configs`` is absent or the name is not found.
    """
    from rey_lib.config.ctx import find_by_name  # local import — avoids circular dep

    configs = getattr(ctx, "llm_configs", None)
    if configs is None:
        raise ConfigError(
            f"llm_config '{name}' referenced in hooks but ctx.llm_configs is not "
            "defined. Add a llm_configs section to your config YAML."
        )
    result = find_by_name(configs, name)
    if result is None:
        raise ConfigError(
            f"llm_config '{name}' not found in ctx.llm_configs. "
            "Check config/app/llm_configs.yaml."
        )
    return result

def _render_prompt(template: str, ctx: Any, data_source: Any) -> str:
    """
    Render an LLM prompt template by substituting {ctx.attr} and
    {data_source.attr} tokens with live values.

    Unresolved tokens are left in place so the LLM still receives a
    readable prompt rather than silently losing context.

    Parameters
    ----------
    template : str
        Prompt template string with ``{ctx.attr}`` / ``{data_source.attr}``
        tokens.
    ctx : Any
        Application context — source for ``ctx.*`` tokens.
    data_source : Any
        Current data source Namespace, or ``None`` for run-scoped hooks.

    Returns
    -------
    str
        Rendered prompt text.
    """
    def _replace(m: re.Match) -> str:
        scope, attr = m.group(1), m.group(2)
        obj = ctx if scope == "ctx" else data_source
        if obj is None:
            return m.group(0)
        return str(getattr(obj, attr, m.group(0)))

    return _PROMPT_TOKEN_RE.sub(_replace, template)

def _execute_one_hook_llm(
    ctx: Any,
    data_source: Any,
    llm_cfg: Any,
) -> dict[str, Any]:
    """
    Execute one llm_config hook and return any row-column values.

    Renders the prompt template, calls the configured LLM, writes the
    response to ctx via any declared output_params, and returns any params
    that declare ``row_column`` so they can be injected into transformed rows.

    Parameters
    ----------
    ctx : Any
        Application context. Must expose ``ctx.llm`` with at least one
        configured LLM instance.
    data_source : Any
        Current data source Namespace, or ``None`` for run-scoped hooks.
    llm_cfg : Any
        A single llm_config Namespace from ctx.llm_configs.

    Returns
    -------
    dict[str, Any]
        ``{column_name: value}`` for every output_param that declares
        ``row_column``. Empty dict when there are no such params.

    Raises
    ------
    AIUnavailableError
        If this runtime has no AI configured.
    AISelectionError
        If the named profile is not one this runtime offers.
    """
    # Local import: the AI runtime is optional, and a loader hook that names no
    # AI must not make importing this module depend on one.
    from rey_lib.ai import (  # noqa: PLC0415
        AIInstruction,
        AIInstructionKind,
        AIRequest,
        AIRequestOptions,
    )
    from rey_lib.ai.errors import AIUnavailableError  # noqa: PLC0415

    ai = getattr(ctx, "shared_ai", None)
    if ai is None:
        raise AIUnavailableError(
            f"LLM hook '{llm_cfg.name}' needs AI, and this runtime has none "
            "configured."
        )

    llm_name      = getattr(llm_cfg, "llm", None) or ""
    system_prompt = getattr(llm_cfg, "system_prompt", None)
    max_tokens    = int(getattr(llm_cfg, "max_tokens", 500))
    template      = getattr(llm_cfg, "prompt_template", "") or ""

    prompt = _render_prompt(template, ctx, data_source)

    _logger.debug("LLM hook '%s': calling profile '%s'", llm_cfg.name, llm_name)
    response = ai.execute(
        AIRequest.prompt(
            prompt,
            profile_id=llm_name,
            instruction=(
                AIInstruction(kind=AIInstructionKind.RAW, text=str(system_prompt))
                if system_prompt else None
            ),
            options=AIRequestOptions(max_tokens=max_tokens),
        ),
    ).text
    _logger.info("LLM hook '%s' response (truncated): %.200s", llm_cfg.name, response)

    row_columns: dict[str, Any] = {}
    for param in (getattr(llm_cfg, "output_params", None) or []):
        ctx_var = getattr(param, "ctx_var", None)
        row_col = getattr(param, "row_column", None)
        if ctx_var:
            object.__setattr__(ctx, ctx_var, response)
            _logger.debug("LLM hook '%s': wrote ctx.%s", llm_cfg.name, ctx_var)
        if row_col:
            row_columns[row_col] = response

    return row_columns

def _resolve_hook_param_value(ctx: Any, data_source: Any, source: str) -> Any:
    """
    Resolve a sql_config param ``source`` value at runtime.

    Supports three formats:

    * ``ctx.<dotted_attr>``     — resolved by walking ctx attribute path
    * ``data_source.<attr>``    — resolved from data_source Namespace
    * anything else             — used as a literal string value

    Parameters
    ----------
    ctx : Any
        Application context.
    data_source : Any
        Current data source Namespace.
    source : str

        ``'data_source.name'`` or ``'2026'``.

    Returns
    -------
    Any
        Resolved value.  Returns empty string if a ctx/data_source path
        segment is missing rather than raising.
    """
    if source.startswith("ctx."):
        return resolve_ctx_path(ctx, source[4:])
    if source.startswith("data_source."):
        attr = source[len("data_source."):]
        return getattr(data_source, attr, "")
    return source

def _execute_one_hook(
    ctx: Any,
    data_source: Any,
    sql_cfg: Any,
    open_conns: dict[str, Any],
    sql_dir: Optional[Path],
) -> dict[str, Any]:
    """
    Execute one sql_config hook and return any output-param row-column values.

    Resolves and opens the connection (caching by name in ``open_conns``),
    builds input param values, calls the procedure or SQL file, captures
    output params, stores them in ``ctx`` via ``ctx_var``, and returns any
    output params that have a ``row_column`` declared — these will be
    injected as extra columns into every transformed row.

    Parameters
    ----------
    ctx : Any
        Application context.
    data_source : Any
        Current data source Namespace.
    sql_cfg : Any
        A single sql_config Namespace (from ctx.sql_configs).
    open_conns : dict[str, Any]
        Shared connection cache — keyed by connection name.
        Connections are opened on first use and closed by the caller.
    sql_dir : Optional[Path]
        Base directory for ``type: sql_file`` configs.

    Returns
    -------
    dict[str, Any]
        ``{column_name: value}`` for every output_param that declares
        ``row_column``.  Empty dict when there are no such params.

    Raises
    ------
    ConfigError
        If the connection is not found or a required file is missing.
    DatabaseError
        If procedure or SQL execution fails.
    """
    from rey_lib.config.ctx import find_by_name  # local import

    conn_name = getattr(sql_cfg, "connection", None)
    if not conn_name:
        raise ConfigError(
            f"sql_config '{sql_cfg.name}' is missing 'connection'. "
            "Add 'connection: <name>' to the sql_config entry."
        )

    # Resolve and cache the connection.
    if conn_name not in open_conns:
        open_conns[conn_name] = shared_connection(ctx, conn_name).handle()
        _logger.debug("Using shared connection '%s' for hook '%s'",
                      conn_name, sql_cfg.name)

    conn = open_conns[conn_name]
    hook_type = getattr(sql_cfg, "type", "procedure")
    row_columns: dict[str, Any] = {}

    if hook_type == "sql_file":
        _execute_one_hook_sql_file(ctx, run_log, conn, sql_cfg, sql_dir)

    else:
        # Default: type == "procedure"
        row_columns = _execute_one_hook_procedure(ctx, run_log, data_source, conn, sql_cfg)

    return row_columns

def _execute_one_hook_sql_file(
    ctx: Any, run_log,
    conn: Any,
    sql_cfg: Any,
    sql_dir: Optional[Path],
) -> None:
    """
    Execute a sql_file hook: read the file and execute it on conn.

    Parameters
    ----------
    conn : Any
        Open database connection.
    sql_cfg : Any
        sql_config Namespace with ``file`` attribute.
    sql_dir : Optional[Path]
        Base directory for resolving the file name.

    Raises
    ------
    ConfigError
        If ``sql_dir`` is None or the file does not exist.
    """
    if sql_dir is None:
        raise ConfigError(
            f"sql_config '{sql_cfg.name}' is type 'sql_file' but no sql_dir "
            "was provided to the pipeline call. Pass sql_dir to run_load/run_transform."
        )
    file_name = getattr(sql_cfg, "file", None)
    if not file_name:
        raise ConfigError(
            f"sql_config '{sql_cfg.name}' is type 'sql_file' but 'file' is not set."
        )
    sql_path = sql_dir / file_name
    if not sql_path.exists():
        raise ConfigError(
            f"sql_config '{sql_cfg.name}': file not found: {sql_path}"
        )
    sql_text = sql_path.read_text(encoding="utf-8")
    execute_sql_text(
        ctx,
        run_log, conn,
        sql_text,
        sql_path=str(sql_path),
        sql_label=str(sql_cfg.name),
        operation="hook_sql_file",
        safe_to_preview=True,
    )
    _logger.info("Hook sql_file executed: %s (config='%s')", file_name, sql_cfg.name)

def _execute_one_hook_procedure(
    ctx: Any, run_log,
    data_source: Any,
    conn: Any,
    sql_cfg: Any,
) -> dict[str, Any]:
    """
    Execute a procedure hook, capture output params, write to ctx.

    Parameters
    ----------
    ctx : Any
        Application context — output params are written here via ``ctx_var``.
    data_source : Any
        Current data source Namespace — used to resolve ``data_source.*``
        param sources.
    conn : Any
        Open SQL Server connection.
    sql_cfg : Any
        sql_config Namespace with ``proc``, ``params``, and optionally
        ``output_params`` attributes.

    Returns
    -------
    dict[str, Any]
        ``{row_column: value}`` for each output_param that declares
        ``row_column``.

    Raises
    ------
    ConfigError
        If ``proc`` is missing from the sql_config.
    DatabaseError
        If procedure execution fails.
    """
    proc_name = getattr(sql_cfg, "proc", None)
    if not proc_name:
        raise ConfigError(
            f"sql_config '{sql_cfg.name}' is type 'procedure' but 'proc' is not set."
        )

    # Resolve input params: [(param_name, value), ...]
    raw_params = getattr(sql_cfg, "params", None) or []
    named_inputs: list[tuple[str, Any]] = [
        (p.name, _resolve_hook_param_value(ctx, data_source, str(p.source)))
        for p in raw_params
    ]

    # Resolve output param specs: [(param_name, sql_type), ...]
    raw_outputs = getattr(sql_cfg, "output_params", None) or []
    output_specs: list[tuple[str, str]] = [
        (op.name, str(getattr(op, "sql_type", "NVARCHAR(MAX)")))
        for op in raw_outputs
    ]

    # Log input params (resolved values) BEFORE the proc fires so the call
    # can be reproduced from the log alone — useful when a proc misbehaves
    # in production and you need to replay it in SSMS.
    if named_inputs:
        inputs_repr = ", ".join(f"{name}={value!r}" for name, value in named_inputs)
    else:
        inputs_repr = "(none)"
    _logger.info(
        "Hook procedure invoking: %s (config='%s') inputs=[%s]",
        proc_name,
        sql_cfg.name,
        inputs_repr,
    )

    output_values = execute_procedure_call(
        ctx,
        run_log, conn,
        proc_name,
        named_inputs,
        output_specs,
        sql_label=str(sql_cfg.name),
        operation="hook_procedure",
        safe_to_preview=False,
    )


    if output_values:
        outputs_repr = ", ".join(f"{k}={v!r}" for k, v in output_values.items())
    else:
        outputs_repr = "(none)"
    _logger.info(
        "Hook procedure executed: %s (config='%s') outputs=[%s]",
        proc_name,
        sql_cfg.name,
        outputs_repr,
    )

    # Write each output param to ctx via ctx_var, collect row_column mappings.
    # Each ctx assignment is logged at INFO so the resolved value is visible
    # in the log file alongside the executed-procedure line.
    row_columns: dict[str, Any] = {}
    for out_cfg in raw_outputs:
        value = output_values.get(out_cfg.name)
        ctx_var = getattr(out_cfg, "ctx_var", None)
        if ctx_var:
            object.__setattr__(ctx, ctx_var, value)
            _logger.info(
                "  → ctx.%s = %r  (from %s output '%s')",
                ctx_var,
                value,
                sql_cfg.name,
                out_cfg.name,
            )
        row_col = getattr(out_cfg, "row_column", None)
        if row_col:
            row_columns[row_col] = value

    return row_columns

def _alter_oversized_columns(
    ctx: Any,
    schema: str,
    table: str,
    rows: list[dict[str, Any]],
    column_defs: list[tuple[str, str]],
) -> bool:
    """
note:: this function must call a configure stored procedure or sql file. no DDL should ever be contained on this file.

    Widen any columns whose values exceeded their declared length by
    running the configured ``alter_column_data_type`` sql_config once
    per affected column.

    Parameters
    ----------
    ctx : Any
        Application context. Must expose ``ctx.sql_configs`` containing
        an entry named ``alter_column_data_type``.
    schema : str
        Target schema — may be ``database.schema`` for cross-db inserts.
    table : str
        Target table name.
    rows : list[dict[str, Any]]
        Rows that failed to insert — scanned for observed string lengths.
    column_defs : list[tuple[str, str]]
        Current ``(column_name, sql_type)`` pairs for the staging table.

    Returns
    -------
    bool
        ``True`` if at least one column was altered and the caller should
        retry the bulk insert. ``False`` if no column needed widening or
        the affected columns were of unsupported type families.

    Raises
    ------
    ConfigError
        When ctx has no ``alter_column_data_type`` sql_config configured.
    """
    sql_cfg      = _find_sql_config(ctx, "alter_column_data_type")
    fq_table     = f"{schema}.{table}"
    expanded_any = False
    open_conns: dict[str, Any] = {}

    try:
        for col_name, col_type in column_defs:
            new_type = _compute_expanded_type(
                col_type, _max_string_len(rows, col_name)
            )
            if new_type is None:
                continue

            # The sql_config's params point at these ctx attrs — set them
            # immediately before the call so each invocation widens the
            # right column.
            object.__setattr__(ctx, "alter_table",     fq_table)
            object.__setattr__(ctx, "alter_column",    col_name)
            object.__setattr__(ctx, "alter_data_type", new_type)

            _execute_one_hook(ctx, None, sql_cfg, open_conns, None)
            _logger.info(
                "Altered column '%s.%s' to %s", fq_table, col_name, new_type,
            )
            expanded_any = True
    finally:
        # Committed but not closed: the commit is this work's transaction, the
        # connection is shared and outlives it.
        for conn in open_conns.values():
            try:
                conn.commit()
            except Exception:  # noqa: BLE001 — commit must never mask the error above
                pass
        open_conns.clear()

    return expanded_any

def _compute_expanded_type(
    current_type: str,
    max_observed: int,
) -> Optional[str]:
    """
    Return the new DataType string for a column that overflowed, or
    ``None`` when no expansion is needed or possible.

    Recognizes NVARCHAR(n) and VARCHAR(n) only. Any other declared type
    is the app's responsibility to size correctly up front — rey_lib
    will not invent type conversions.
    """
    if max_observed <= 0:
        return None
    upper = (current_type or "").upper().strip()
    for prefix, pattern in _WIDENABLE_TYPE_PATTERNS:
        m = pattern.match(upper)
        if not m:
            continue
        current_len = int(m.group(1))
        if max_observed <= current_len:
            return None
        return f"{prefix}({max_observed + _COLUMN_EXPAND_BUFFER})"
    return None

def _max_string_len(rows: list[dict[str, Any]], col_name: str) -> int:
    """
    Maximum string length of ``col_name`` across all rows. None values and
    missing keys count as length 0.
    """
    return max(
        (len(str(row.get(col_name, "") or "")) for row in rows),
        default=0,
    )

def _source_path_field(source: Any) -> dict[str, str]:
    """The ``path`` evidence field, for a source that has one.

    **File-specific evidence, emitted where something knows it has a file.**
    Every run-log row a file load has ever written carried its path and still
    does; what changed is that the boundary no longer demands one in order to
    write any row at all.

    Absent rather than empty for a source with no path: a blank ``path`` in a
    record reads as a delivery whose location was lost, which is a different
    and more alarming fact than a source that never had one.
    """
    path = getattr(source, "path", None)
    return {} if path is None else {"path": str(path)}


def _related_path_field(source: Any) -> dict[str, str]:
    """``related_path`` for a step failure, on the same terms."""
    path = getattr(source, "path", None)
    return {} if path is None else {"related_path": str(path)}


def _reject_structure(
    ctx: Any,
    run_log: Any,
    structure_exc: DataStructureError,
    source: Any,
    destination: dict[str, Any],
    movements: Any,
    paths: Any,
) -> int:
    """Refuse one file for a structural fault, recording why.

    SHARED BY BOTH EXECUTION PATHS, so a file refused without being
    materialized produces the same evidence as one refused after being read.
    The validation NAME comes from the error, because only the check that ran
    knows what it was -- ``load_header`` and ``configured_columns`` both
    arrive here.

    ``destination`` is the evidence form of the target, ALREADY IN THE TARGET'S
    OWN VOCABULARY -- a table names a schema and a table, a file names a path.
    Carried as fields rather than two strings so a file destination is not
    recorded under column names that mean something else.

    Returns:
        0. A file fault is not a run fault.
    """
    _logger.error("%s — source rejected: %r", structure_exc, source)
    log_validation_result(run_log,
        validation_name=structure_exc.validation_name,
        status="failed",
        message=str(structure_exc),
        **destination,
        **_source_path_field(source),
    )
    _route_file(ctx, run_log, movements, "failure", source, paths)
    log_exit(ctx, f"_load_one_file rejected (structure): {source!r}", _logger)
    return 0

def _reject_empty(
    ctx: Any,
    run_log: Any,
    source: Any,
    destination: dict[str, Any],
    movements: Any,
    paths: Any,
) -> int:
    """Refuse one file that produced no rows.

    SHARED BY BOTH EXECUTION PATHS. The materialised path knows the source
    was empty because reading it produced nothing; the non-row path knows
    because the engine reported inserting nothing. Different evidence for the
    same fact, and the same refusal.

    ``destination`` is the target's own evidence fields, as in
    ``_reject_structure``.

    Returns:
        0.
    """
    _logger.warning("No rows produced from source: %r", source)
    log_validation_result(run_log,
        validation_name="load_rows",
        status="failed",
        message="No rows produced",
        **destination,
        **_source_path_field(source),
    )
    _route_file(ctx, run_log, movements, "failure", source, paths)
    log_exit(ctx, f"_load_one_file rejected (empty): {source!r}", _logger)
    return 0

def _non_row_execution_possible(
    source: Any,
    transform: Any,
    loader: Any,
    conn: Any,
    exists: bool,
) -> bool:
    """Whether this load can be executed without materializing its records.

    **CAN THE EXECUTION ENVIRONMENT USE IT**, which is deliberately not "does
    the target's provider read the source". The latter is true of a file and
    false of database-to-database, where the source is already a relation and
    nothing reads a file at all.

    Four questions, and every one of them has to answer yes:

    1. **The transform offers an equivalent non-row form.** ``None`` means
       row-by-row only, which is every transform until it says otherwise.
       Asked of the transform because equivalence is its claim to make.
    2. **The source DECLARES its structure.** A header states every column in
       order before a row is read, so the file can be validated and its
       column order established without materializing it. A source that only
       exhibits its structure -- keys on each record, positions in each line
       -- cannot, and has nothing to offer here. This is a source primitive,
       not a format test: no name is checked.
    3. **The destination already exists.** Creating one needs a schema, and a
       schema is derived from records this path will not have. An absent
       destination is not a refusal of the load, only of this path.
    4. **The provider implements it.** Resolved from the provider module by
       ``supports_provider_capability``, so nothing declares support twice
       and a provider that lacks the function simply answers no.

    Returns:
        True when all four hold. False is never a failure -- it means the
        materialised path runs, which is the guaranteed one.
    """
    if transform is None or transform.execution_form() is None:
        return False
    if not getattr(source, "declares_structure", False):
        return False
    if not exists:
        return False
    return bool(
        loader.adapter.supports_provider_capability(conn, "insert_from_path")
    )

def _load_one_file(
    source: Any,
    transform: Any,
    target: Any,
    *,
    ctx: Any,
    run_log: Any,
    transform_cfg: Any = None,
    load_cfg: Any = None,
    paths: Any = None,
    loader: Any = None,
    movements: Any = None,
    load_name: str = "",
) -> int:
    """Transfer one source into one target, through one transform.

    **THE TRANSFER BOUNDARY.** Three domain inputs, and they are the whole
    contract:

        source     what is being read     a DataFile or a QuerySource
        transform  what the records are   a DataTransform
        target     where they go          a DatabaseObjectIdentity, or a
                                          DataFile writing itself

    **SOURCE AND TARGET ARE ROLES**, and the two ends are independent: a
    DataFile answers either one, and neither end is told what the other is.
    That is what makes file->table, query->table, query->file and file->file
    one mechanism rather than four.

    Everything else is context, evidence or policy, and is keyword-only
    precisely so it cannot be mistaken for part of the contract.

    WHAT IS NOT HERE, and must not come back
    ----------------------------------------
    **No connection.** An open connection is not generic execution context --
    it is the runtime form of ONE KIND of data object. A file-to-file transfer
    has no meaningful connection, so a boundary that demanded one would not be
    symmetric however its parameters were spelled. It is resolved BELOW, from
    ``target.connection``, where a database is first actually needed.

    **No path, schema or table.** Those are the endpoints, and the endpoints
    are the objects. A second representation travelling alongside would mean
    the flattening was never removed, only joined.

    **Not a batch.** One source, one target. Selecting many files is
    ``ConfiguredLoad``'s, and a many-to-one feeder is a different abstraction
    that would need its own object.

    Args:
        source: The data object being read. Its own type settles how.
        transform: What the produced records contain.
        target: The data object being written, naming its configured
            connection rather than holding one.
        ctx: Application context, for logging and configured widening.
        run_log: The run's evidence recorder.
        transform_cfg: Declared columns, where a definition declares them.
        load_cfg: The definition, for its movements and create policy.
        paths: Where routed files go.
        loader: The destination mechanic.
        movements: Resolved movement policy. None means do not route, which
            is not the same as routing nowhere.
        load_name: What this load is called in evidence.

    Returns:
        Rows loaded, or 0 when this file was rejected. A file fault is 0; a
        run-level fault raises.
    """
    # NO `source.path`. The source is a DATA OBJECT and stays one through this
    # boundary; a path is one family's primitive, and demanding it here is what
    # made a load impossible to begin anywhere but a file.
    #
    # WHAT THE DESTINATION IS, asked once, and the only thing this boundary
    # branches on. A target is a ROLE: a database object is written by the
    # loader through a connection, and a data file writes itself. Neither end
    # is told what the other is -- which is what makes file->table,
    # query->table, query->file and file->file one mechanism rather than four.
    writes_a_file = isinstance(target, DataFile)
    # The evidence form of the destination, rendered once, IN THE TARGET'S OWN
    # VOCABULARY. Logs and run-log rows have always named a table the way the
    # adapter does; a file names its path, because recording one under `table`
    # would say something untrue in a column other things read.
    schema = table = ""
    if writes_a_file:
        destination = {"destination_path": str(target.path)}
        named = str(target.path)
    else:
        schema, table = adapter_destination(target)
        destination = {"schema": schema, "table": table}
        named = f"{schema}.{table}"
    # Named before the try so the handlers can ask whether there is anything
    # to roll back -- the connection is resolved inside, and a failure before
    # that point has opened nothing.
    conn: Any = None
    log_enter(ctx, f"_load_one_file: {source!r}", _logger)

    try:
        # RESOLVED, not re-read. A configured load supplies its definition's
        # movements; a DIRECT load has none, because nothing was picked up
        # from an inbox and there is nowhere to move it to. None means "do
        # not route", which is different from "route nowhere".
        if movements is None and load_cfg is not None:
            movements = getattr(load_cfg, "movements", None)
        if not load_name:
            load_name = str(getattr(load_cfg, "name", "") or "load")

        # ONE CONSTRUCTION PATH. Handed the definition's own objects where a
        # ConfiguredLoad is driving; building them only when called directly.
        # Re-reading the configuration here when it has already been read
        # would be a second graph that looks identical and can drift.
        # EVERY ONE OF THESE IS A TABLE'S QUESTION, and a file target is asked
        # none of them. Not stubbed: the dispatch is on what the target IS, so
        # a connection, a create policy and an existing column set are simply
        # not part of writing a file, and nothing has to answer them emptily.
        #
        # `expected_columns` stays None, which this boundary ALREADY means as
        # "no destination to match, so check the source is coherent with
        # itself" -- see `validate`'s two questions below. The file writers
        # take no column set at all: the names and their order come from the
        # records, which `logical_schema` fixes one step before the write.
        expected_columns: list[str] | None = None
        if not writes_a_file:
            if loader is None:
                loader = _build_data_loader(
                    ctx, _create_destination_declared(load_cfg),
                )

            # THE CONNECTION IS DERIVED FROM THE TARGET, and this is the first
            # point a database is actually needed. It is resolved here rather
            # than handed in so that `target.connection` is the one source of
            # truth -- nothing reads a configured connection name once the
            # target exists, so there is no second answer for a guard to check.
            conn = shared_connection(ctx, target.connection).handle()

            # Asked of the LOADER, which owns the policy, rather than re-read
            # from configuration. A direct load declares it as an argument and
            # has no config to read; reading config here would have refused its
            # own --create.
            create_declared = loader.create_destination

            # EXISTENCE ASKED ONCE, and its answer serves both decisions: how
            # to validate the file, and whether the destination must be
            # created. None means absent; [] would mean a table with no
            # columns.
            expected_columns = loader.destination_columns(conn, target)
            exists = expected_columns is not None

            if not exists and not create_declared:
                # A misconfiguration, NOT a bad file -- so it raises rather
                # than running movements.failure. The file is fine and moving
                # it to rejected_path would strand a good file for a fault it
                # did not cause; every later file would fail identically
                # anyway.
                raise ConfigError(
                    f"load '{load_name}' requires destination "
                    f"{schema}.{table}, and it does not exist. Set "
                    f"create_destination_table: true under that load's 'load:' "
                    f"block to have the loader create it."
                )

        # NO FORMAT BRANCH, and no format name either. Every source reaching
        # here is a data object; a format with no DataFile is refused
        # upstream, where the object would have been built. The
        # XLSX/DELIMITED_NO_HEADER exclusion that used to stand here is gone
        # -- one is migrated, the other is converted to CSV upstream.
        #
        # The file decides WHEN its shape is checked, because that depends on
        # where its column names live: a header is one line and is checked
        # before the rows, record keys only after, and a headerless file
        # states its width nowhere but in its rows. This step knows none of
        # that.
        if transform is None:
            transform = _build_identity_transform(transform_cfg)

        # The DB-NATIVE fast path, and it belongs to a database target: it asks
        # the destination's provider to read the source itself. A file target
        # has no provider to ask, so the materialised path -- the guaranteed
        # one -- runs.
        if not writes_a_file and _non_row_execution_possible(
            source, transform, loader, conn, exists,
        ):
            # THE SAME TWO CHECKS, from the structure the file declares
            # instead of from records. Neither is skipped and neither is
            # reimplemented: `validate` is the half of `read_validated` that
            # reads no rows, and the configured-columns rule is asked over
            # ordered names by both paths.
            try:
                source.validate(expected_columns)
                columns = transform.columns_for_names(source.source_structure())
            except DataStructureError as structure_exc:
                return _reject_structure(
                    ctx, run_log, structure_exc, source, destination,
                    movements, paths,
                )

            # THE PATH IS READ HERE and nowhere above. This branch has already
            # established the source is eligible for the file-native path, so
            # the path is that implementation's own primitive rather than
            # something the shared boundary demanded of every source.
            inserted = loader.load_from_path(conn, target, source.path, columns)
            if not inserted:
                # The source held no rows. Reported by the engine rather than
                # counted here, and refused exactly as an empty read is.
                return _reject_empty(
                    ctx, run_log, source, destination, movements, paths,
                )

            _logger.info(
                "Loaded: %r → %s  rows=%d  (non-row execution)",
                source, named, inserted,
            )
            log_row_count(run_log,
                count_name="loaded_rows",
                count=inserted,
                subject=repr(source),
                **destination,
                **_source_path_field(source),
            )
            _route_file(ctx, run_log, movements, "success", source, paths)
            log_exit(ctx, f"_load_one_file done: {source!r}", _logger)
            return inserted

        try:
            # ALWAYS VALIDATED, but against different things.
            #
            #   a destination  -> does this file match the table?
            #   None (absent)  -> is this file coherent with ITSELF?
            #
            # The second is what the create path never had. An absent
            # destination used to mean no check at all, so an inconsistent
            # file had its table created from the first record and then
            # failed inside the insert -- a database error for what is a
            # file defect, raised after DDL.
            rows = source.read_validated(expected_columns)
        except DataStructureError as structure_exc:
            return _reject_structure(
                ctx, run_log, structure_exc, source, destination,
                movements, paths,
            )

        if not rows:
            return _reject_empty(
                ctx, run_log, source, destination, movements, paths,
            )

        # The transform says what the produced records contain. On the load
        # path it is IDENTITY -- the transform stage ran earlier and wrote its
        # output to this file -- but it is explicit rather than absent, so the
        # pipeline is one shape whether or not a transform is configured.
        rows        = transform.transform(rows)
        column_defs = transform.logical_schema(rows)
        columns     = [name for name, _sql_type in column_defs]

        # THE WRITE, and the only place the two target families differ.
        #
        # A data file writes ITSELF, through the writer its format already
        # owns; the records carry their own column names and order, which is
        # what `logical_schema` settled one line above and what those writers
        # already read their header from. A database object is written by the
        # loader: the create policy, the insert and the truncation retry, told
        # what the existence check already found so the destination is
        # inspected once per file rather than once per decision.
        if writes_a_file:
            target.write(rows)
        else:
            loader.load(conn, target, rows, column_defs, expected_columns)

        _logger.info(
            "Loaded: %r → %s  rows=%d",
            source, named, len(rows),
        )
        log_row_count(run_log,
            count_name="loaded_rows",
            count=len(rows),
            subject=repr(source),
            **destination,
            **_source_path_field(source),
        )

        _route_file(ctx, run_log, movements, "success", source, paths)
        log_exit(ctx, f"_load_one_file done: {source!r}", _logger)
        return len(rows)

    except DatabaseError as exc:
        # Only if one was opened. The connection is resolved inside the try
        # now, so a failure before that point has nothing to undo -- and
        # calling rollback on nothing would replace a real database error
        # with an AttributeError.
        #
        # THE SAME HAZARD, ONE LEVEL DOWN: a provider whose statements are
        # already atomic has no transaction open to undo, and answers the
        # call by raising -- DuckDB says "cannot rollback - no transaction is
        # active". Undoing nothing is exactly what was wanted there, so the
        # refusal is not a failure and must not be allowed to replace the
        # error being reported. The original exception is what the run log
        # and the operator need.
        if conn is not None:
            try:
                conn.rollback()
            except Exception as rollback_exc:   # noqa: BLE001 -- see above
                _logger.debug(
                    "Nothing to roll back for %r: %s",
                    source, rollback_exc,
                )
        _logger.error(
            "Database error loading %r — rolled back: %s",
            source, exc,
        )
        log_loader_step_failure(
            ctx,
            run_log, exc,
            failed_step_id=load_name,
            failed_step_name=load_name,
            **_related_path_field(source),
        )
        _route_file(ctx, run_log, movements, "failure", source, paths)
        log_exit(ctx, f"_load_one_file failed: {source!r}", _logger)
        return 0

def _configured_columns(transform_cfg: Any) -> Optional[list[str]]:
    """Return the declared output columns in order, or None when none are.

    **Three answers, and the third is a refusal.**

    ``None``
        No ``columns:`` block. A load that declares no schema -- the direct
        single-file path is one -- and the records decide their own shape.
    ``[...]``
        The current LIST shape. These are the columns, in this order.
    *raises*
        A ``columns:`` block in any other shape. A config that declares a
        schema the loader cannot read is WRONG, and says so.

    **Absent and unreadable are different answers and must not collapse.**
    They did: the older mapping shape iterated to plain strings, every entry
    was skipped as "not a dict", and a feed carrying 22 declared columns
    behaved exactly like one declaring none. Two feeds drifted that way
    unnoticed.

    Raises:
        ConfigError: When ``columns:`` is present in an unsupported shape.
    """
    declared = namespace_to_plain(getattr(transform_cfg, "columns", None))

    if declared is None:
        return None

    if not isinstance(declared, list):
        raise ConfigError(
            f"transform '{getattr(transform_cfg, 'name', '?')}' declares "
            f"columns: as a {type(declared).__name__}, which is not the "
            "supported shape. Each output column is one list entry: "
            "- name: <column>, with an optional source: and transform:."
        )

    names: list[str] = []
    for position, col_cfg in enumerate(declared, start=1):
        if not isinstance(col_cfg, dict) or not col_cfg.get("name"):
            raise ConfigError(
                f"transform '{getattr(transform_cfg, 'name', '?')}' has a "
                f"columns: entry at position {position} that is not a "
                f"mapping with a name: -- found {col_cfg!r}."
            )
        names.append(str(col_cfg["name"]))

    return names

def _transform_map(transform_cfg: Any) -> dict[str, dict[str, Any]]:
    """Return output column -> inline transform config for native columns."""
    transform_map: dict[str, dict[str, Any]] = {}

    for col_cfg in namespace_to_plain(getattr(transform_cfg, "columns", None)) or []:
        if not isinstance(col_cfg, dict):
            continue
        name = str(col_cfg.get("name", ""))
        transform = col_cfg.get("transform") or {}
        if name and isinstance(transform, dict) and transform:
            transform_map[name] = transform

    return transform_map

def _find_transform(
    transforms: list[Any],
    name: str,
    version: str,
) -> Any:
    """
    Find a transform config by name and version.

    Parameters
    ----------
    transforms : list[Any]
        List of transform Namespace objects from data_source.transforms.
    name : str
        Transform name to match.
    version : str
        Transform version to match.

    Returns
    -------
    Any
        Matching transform Namespace.

    Raises
    ------
    ValueError
        If no matching transform is found.
    """
    for t in transforms:
        if (
            getattr(t, "name",    None) == name
            and getattr(t, "version", None) == version
        ):
            return t
    raise ValueError(
        f"No transform found with name='{name}' version='{version}'."
    )

def _create_destination_declared(load_cfg: Any) -> bool:
    """Whether this load declares that its destination may be created.

    ``loads[].load.create_destination_table``, registered beside ``connection``
    and ``destination_table`` in rey_loader's configuration reference.

    **Absent means false**, which is what the loader already did: an absent
    destination returned no columns, every shape check failed against them and
    the file was rejected before creation was ever reached. So a config that
    says nothing keeps behaving exactly as it does today, and creating a table
    is something a data source has to ask for.

    A value that is not a boolean is REFUSED rather than read for its
    truthiness. This governs whether the loader issues DDL, and ``"false"`` as
    a quoted string is truthy in Python -- silently creating a table for a
    config that spelled out the opposite.

    Args:
        load_cfg: The load Namespace.

    Returns:
        Whether creation is declared.

    Raises:
        ConfigError: If the key is present and is not a boolean.
    """
    declared = getattr(getattr(load_cfg, "load", None),
                       "create_destination_table", None)
    if declared is None:
        return False
    if not isinstance(declared, bool):
        raise ConfigError(
            f"load '{getattr(load_cfg, 'name', '?')}' declares "
            f"create_destination_table: {declared!r}, which is not true or "
            "false."
        )
    return declared

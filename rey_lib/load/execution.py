"""A load executed through the three canonical objects.

    Source / Transform / Target
       -> validate each, through its own contract
       -> resolve each, through its own contract
       -> the existing transfer boundary, ``_load_one_file``

**NOTHING IS TRANSLATED BACK.** The objects are not turned into strings for a
builder to rebuild them from: each resolves itself -- a ``DataFile`` or
``QuerySource``, a built transform, a ``DatabaseObjectIdentity`` with its
``DataLoader`` or a target ``DataFile`` -- and those are what the transfer
boundary is handed. That boundary is the one every load path already ends in.

**ROUTING IS NOT JUDGEMENT.** ``derived_load_shape`` chooses among the execution
paths implemented here. A pair it has no shape for is refused as "no execution
path is implemented for this pair" -- the pair itself is not judged invalid.

Whatever is resolved is this call's and is not kept.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files.data_file.base import DataFile
from rey_lib.load import load_operation
from rey_lib.load.manifest_source import SourceContextReader
from rey_lib.load.shape import derived_load_shape
from rey_lib.load.source import Source, _as_int
from rey_lib.load.target import Target
from rey_lib.load.transform import Transform

__all__ = ["load_arguments", "preview_selected", "run_selected_load", "source_columns"]


def run_selected_load(
    ctx: Any,
    run_log: Any,
    source: Source,
    transform: Transform,
    target: Target,
    *,
    reader: Optional[SourceContextReader] = None,
) -> int:
    """Validate, resolve and execute one load from its three canonical objects.

    Args:
        ctx: The runtime context, for configured connections and widening.
        run_log: The run's evidence recorder.
        source, transform, target: The load's three canonical objects.
        reader: Whatever answers the source-context contract. Needed only for
            a manifest source.

    Returns:
        Rows loaded.

    Raises:
        ConfigError: If any object is incomplete, no execution path is
            implemented for the pair, or a named end cannot be resolved.
    """
    # EACH OBJECT'S OWN CONTRACT, before anything is resolved or built.
    problems = [
        f"{name}: {', '.join(found)}"
        for name, found in (
            ("Source", source.validate()),
            ("Transform", transform.validate()),
            ("Target", target.validate()),
        )
        if found
    ]
    if problems:
        raise ConfigError(f"The load is not complete. {'; '.join(problems)}.")

    shape = derived_load_shape(source, target)
    if shape is None:
        raise ConfigError(
            f"No execution path is implemented for a {source.selected_kind()} "
            f"source into a {target.selected_kind()} target."
        )

    # EACH OBJECT RESOLVES ITSELF. The read adapter is the one the load paths
    # read through, so a query reads exactly as it always has.
    resolved_source = source.resolve(
        ctx, reader=reader, adapter=load_operation._read_adapter,
    )
    if isinstance(resolved_source, DataFile) and source.selected_kind() == "file":
        # A named file that is not there is a mistyped path, refused before any
        # destination is touched.
        path = Path(str(resolved_source.path))
        if not path.is_file():
            raise ConfigError(f"Source (file): no such file: {path}")
    resolved_transform = transform.resolve(ctx)
    resolved_target = target.resolve(ctx)

    # THE GOVERNED IDENTITY IS THE SOURCE OBJECT'S, whatever kind executes: a
    # Source populated from a governed file keeps it while a query runs.
    governed = {
        name: held
        for name, held in (
            ("file_manifest_id", _as_int(source.value("file-manifest-id"))),
            ("file_mutation_id", _as_int(source.value("file-mutation-id"))),
        )
        if held is not None
    }
    if governed:
        # WHAT THE LOAD DID, as the Destination states it: its write policy
        # for a table, its format for a file.
        governed["reason"] = (
            f"transform_to_file_{resolved_target.file_type}"
            if isinstance(resolved_target, DataFile)
            else f"load_to_table_{target.write_policy()}"
        )
        # WHAT RAN, read from the objects that ran it: the Transform's saved
        # setting and executed definition, and the Destination's identity.
        persistence = transform.value("persistence")
        saved = dict(persistence).get("transform_id") if persistence else None
        governed["transform_id"] = int(saved) if saved is not None else None
        governed["transform_snapshot"] = {
            "kind": transform.selected_kind(),
            "declaration": transform.executed_declaration(),
        }
        governed["destination_identity"] = (
            {"path": str(target.value("out-file")), "file_type": resolved_target.file_type}
            if isinstance(resolved_target, DataFile)
            else resolved_target[0].to_dict()
        )

    # THE TRANSFER BOUNDARY, as every load path reaches it.
    if isinstance(resolved_target, DataFile):
        return load_operation._load_one_file(
            resolved_source, resolved_transform, resolved_target,
            ctx=ctx, run_log=run_log, movements=None,
            load_name=f"query:{resolved_target.path.name}",
            **governed,
        )
    identity, loader = resolved_target
    name = ".".join(part for part in (identity.catalog, identity.schema, identity.name) if part)
    return load_operation._load_one_file(
        resolved_source, resolved_transform, identity,
        ctx=ctx, run_log=run_log, loader=loader, movements=None,
        load_name=name if shape in ("direct", "manifest") else f"query:{name}",
        **governed,
    )


def source_columns(
    ctx: Any,
    source: Source,
    *,
    reader: Optional[SourceContextReader] = None,
) -> list[str]:
    """The columns the Source's selected configuration carries, read through it.

    Args:
        ctx: The runtime context, for configured connections.
        source: The canonical Source.
        reader: Whatever answers the source-context contract. Needed only for
            a manifest source.

    Returns:
        The source's own column names, or none where it is not complete.

    Raises:
        ConfigError: If a named source cannot be resolved.
    """
    if source.validate():
        return []
    resolved = source.resolve(ctx, reader=reader, adapter=load_operation._read_adapter)
    return list(resolved.source_structure())


def preview_selected(
    ctx: Any,
    source: Source,
    transform: Transform,
    *,
    limit: int,
    reader: Optional[SourceContextReader] = None,
) -> load_operation.LoadPreview:
    """The first records the objects would carry, through the objects themselves.

    The Source and Transform each resolve through their own contract, as
    :func:`run_selected_load` resolves them, and the resolved source is SAMPLED
    rather than read. A Source that is not complete is answered empty; a
    Transform that is not complete shows the rows as they came.

    Args:
        ctx: The runtime context, for configured connections.
        source: The canonical Source.
        transform: The canonical Transform.
        limit: How many records at most.
        reader: Whatever answers the source-context contract. Needed only for
            a manifest source.

    Returns:
        The produced columns, the records, and whether the source held more.

    Raises:
        ConfigError: If a named source or transform cannot be resolved.
    """
    if source.validate():
        return load_operation.LoadPreview()
    resolved = source.resolve(ctx, reader=reader, adapter=load_operation._read_adapter)
    built = (
        load_operation._build_transform(ctx, None, None)
        if transform.validate() else transform.resolve(ctx)
    )
    # ONE MORE THAN ASKED FOR, which is how "there is more" is established.
    sampled = resolved.sample(limit + 1)
    return load_operation.LoadPreview(
        columns=tuple(built.columns_for_names(list(resolved.source_structure()))),
        rows=tuple(
            load_operation._for_display(row) for row in built.transform(sampled[:limit])
        ),
        truncated=len(sampled) > limit,
    )


def load_arguments(source: Source, transform: Transform, target: Target) -> dict[str, Any]:
    """The objects' execution arguments: each selected configuration, as held.

    The inverse of rey_loader's parsing of its arguments into these objects: an
    object's field names ARE the loader's option names, so its selected
    configuration is its part of the invocation. Unset values are dropped;
    nothing is resolved or validated here -- the objects the invocation builds
    do that.

    Args:
        source, transform, target: The load's three canonical objects.

    Returns:
        Option name to value, for the three selected configurations.
    """
    arguments: dict[str, Any] = {}
    for one in (source, transform, target):
        for name, value in one.configuration().items():
            if value in (None, "", False):
                continue
            arguments[name] = value
    # THE GOVERNED IDENTITY TRAVELS WITH THE SOURCE, whatever kind is selected:
    # a database Source populated from a governed file still names that file,
    # and the load's mutation is recorded against it. The kind is unchanged --
    # neither field is one the selected kind infers from.
    for name in ("file-manifest-id", "file-mutation-id"):
        value = source.value(name)
        if value not in (None, "", False):
            arguments.setdefault(name, value)
    return arguments

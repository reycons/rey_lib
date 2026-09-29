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
from rey_lib.load.source import Source
from rey_lib.load.target import Target
from rey_lib.load.transform import Transform

__all__ = ["run_selected_load"]


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

    # THE TRANSFER BOUNDARY, as every load path reaches it.
    if isinstance(resolved_target, DataFile):
        return load_operation._load_one_file(
            resolved_source, resolved_transform, resolved_target,
            ctx=ctx, run_log=run_log, movements=None,
            load_name=f"query:{resolved_target.path.name}",
        )
    identity, loader = resolved_target
    name = ".".join(part for part in (identity.catalog, identity.schema, identity.name) if part)
    return load_operation._load_one_file(
        resolved_source, resolved_transform, identity,
        ctx=ctx, run_log=run_log, loader=loader, movements=None,
        load_name=name if shape in ("direct", "manifest") else f"query:{name}",
    )

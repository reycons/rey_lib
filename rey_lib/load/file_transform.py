"""File-level transforms: what is done to a governed FILE, not to its records.

    DataFile -> FileTransform -> DataFile(s)

The record-level ``DataTransform`` (``IdentityTransform``, ``ColumnTransform``)
turns records into records. A ``FileTransform`` takes one governed
:class:`DataFile` -- exactly the state the caller selected and
``ManifestSource.data_file()`` materialised -- and returns the file or files the
operation leaves behind. Both are built by :meth:`Transform.resolve`, the one
resolver.

**THE FILE-LEVEL KINDS ARE A REGISTRY.** Each kind's implementation registers
itself with :func:`file_transform`, the way a DataFile format registers with
``@data_file``; nothing keeps a central list, and registration adds no
resolution path.

**It selects nothing.** Which mutation a file is operated on at is the caller's
decision, made before the DataFile was built.

**The filesystem work is the existing primitives'.** ``move`` goes through
``rey_lib.files.file_routing``, which validates the governed roots, moves the
file and writes the move's evidence -- success or failure -- itself.

**Runtime dependencies, not configuration.** The run log is the one already
bound to the current run, and the application name is the runtime context's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from rey_lib.errors.error_utils import ConfigError
from rey_lib.files import file_routing
from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.files.file_routing import (
    CollisionPolicy,
    FileRoutingContext,
    FileRoutingResult,
    FileRoutingRole,
    GovernedFileReference,
)
from rey_lib.logs import bound_run_log, get_logger

__all__ = [
    "FileKind", "FileTransform", "MoveTransform", "build_file_transform",
    "file_kind", "file_kinds", "file_transform",
]

_logger = get_logger(__name__)

#: The bare lifecycle-root placeholder a route may carry. Resolved from the
#: DataFile's own ``base_path``; ``<classification.*>`` is routing's.
_BASE_PATH_TOKEN = "<base_path>"


class FileTransform(ABC):
    """One operation on one governed file."""

    @abstractmethod
    def apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Operate on exactly this governed file, and return what it leaves.

        Args:
            data_file: The governed file, at the state the caller selected.

        Returns:
            The resulting DataFile or DataFiles, each carrying its governed
            identity -- or nothing, where the operation produced none.
        """


@dataclass(frozen=True)
class FileKind:
    """One file-level kind: its fields, which it needs, and what builds it."""

    id: str
    fields: tuple[str, ...]
    required: tuple[str, ...]
    builder: Callable[..., FileTransform]


#: The file-level kinds, in registration order.
_REGISTRY: dict[str, FileKind] = {}


def file_transform(
    kind: str,
    *,
    fields: tuple[str, ...],
    required: tuple[str, ...] | None = None,
) -> Callable[[type[FileTransform]], type[FileTransform]]:
    """Register the decorated FileTransform as the implementation of one kind.

    :meth:`Transform.resolve` builds the registered class with the kind's
    configuration as keyword arguments.

    Args:
        kind: The kind's name.
        fields: The configuration fields the kind holds.
        required: The fields it cannot run without; every field by default.
    """
    def _decorator(cls: type[FileTransform]) -> type[FileTransform]:
        _REGISTRY[kind] = FileKind(
            id=kind, fields=tuple(fields),
            required=tuple(fields if required is None else required),
            builder=cls,
        )
        return cls

    return _decorator


def file_kind(kind: str) -> FileKind | None:
    """The registered file-level kind called ``kind``, or None."""
    return _REGISTRY.get(kind)


def file_kinds() -> tuple[FileKind, ...]:
    """Every registered file-level kind, in registration order."""
    return tuple(_REGISTRY.values())


@file_transform("move", fields=("role", "route", "operation", "name"),
                required=("role", "route", "operation"))
class MoveTransform(FileTransform):
    """Move one governed file to a role's route.

    The same file comes back at its new path, under the same governed identity,
    at the state the move recorded. :meth:`plan` answers where it WOULD go,
    changing nothing.
    """

    def __init__(
        self,
        ctx: Any,
        *,
        role: str,
        route: str,
        operation: str,
        name: str | None = None,
    ) -> None:
        """Hold the move's configuration and the runtime it runs in.

        Args:
            ctx: The runtime context: its ``app_name`` and its ``data`` path.
            role: processing | kickouts | failed | archive.
            route: The destination DIRECTORY. ``<base_path>`` resolves from the
                file; ``<classification.*>`` resolves in routing.
            operation: The operation this move serves -- the caller's, which
                the move's evidence states.
            name: The destination file name, where the caller resolved one (a
                declared destination such as ``{kickouts}/<file_name>``).
                Unset keeps the file's own name, as every move did before.

        Raises:
            ConfigError: If the role names no routing role.
        """
        try:
            self._role = FileRoutingRole(str(role).strip())
        except ValueError as exc:
            raise ConfigError(
                f"Transform (move): '{role}' is not a routing role. Roles: "
                f"{', '.join(member.value for member in FileRoutingRole)}."
            ) from exc
        self._ctx = ctx
        self._route = str(route)
        self._operation = str(operation)
        self._name = str(name).strip() if name else None

    def apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Move the file, and return it at its new path.

        Raises:
            ConfigError: If the file is not governed, the route needs a
                ``base_path`` the file does not carry, or the runtime has no
                application name or ``data`` path.
            FileRoutingError: As routing refuses or fails; routing has already
                recorded a failed move where the filesystem operation failed.
        """
        _logger.debug(
            "Moving governed file %s (mutation %s) to %s for %s",
            data_file.file_manifest_id, data_file.file_mutation_id,
            self._role.value, self._operation,
        )
        result = self._route_file(data_file, dry_run=False)
        # UNCHANGED is the file already where the route puts it: the same
        # state, so the same mutation. Every other outcome that returns is a
        # move routing recorded, and that record is the new state.
        file_mutation_id = (
            data_file.file_mutation_id
            if result.file_manifest_record_id is None
            else result.file_manifest_record_id
        )
        facts = {**data_file.governed_facts(), "file_mutation_id": file_mutation_id}
        # MOVING THE ORIGINAL MOVES WHERE THE ORIGINAL IS (backlog 624). When
        # this file is the original -- a classify move to processing, an
        # archive move, a kickout -- the result's original facts are the new
        # location and state; a derived file's original stays where it was.
        if (
            data_file.original_path is None
            or Path(data_file.original_path).expanduser().resolve()
            == Path(data_file.path).expanduser().resolve()
        ):
            facts["original_path"] = str(result.resulting_path)
            facts["original_mutation_id"] = file_mutation_id
        return (
            data_file_for(
                result.resulting_path,
                file_type=data_file.file_type,
                **facts,
                **data_file.settings,
            ),
        )

    def plan(self, data_file: DataFile) -> Path:
        """Where :meth:`apply` would put this file, changing nothing.

        Routing's own dry run: every validation a move makes -- governed roots,
        the source exists, the destination is usable -- with no filesystem
        change and no mutation evidence.

        Raises:
            ConfigError: As :meth:`apply` refuses.
            FileRoutingError: As routing refuses.
        """
        return self._route_file(data_file, dry_run=True).resulting_path

    def _route_file(self, data_file: DataFile, *, dry_run: bool) -> FileRoutingResult:
        """Hand this file to the routing primitive for this move's role."""
        if data_file.file_manifest_id is None:
            raise ConfigError(
                f"Transform (move): {data_file!r} carries no governed identity, "
                "so there is no governed file to move."
            )
        # Looked up when called, from routing's published wrappers: one per
        # role, and the module's own -- never a copy taken at import.
        move = getattr(file_routing, f"move_to_{self._role.value}")
        return move(
            replace(self._routing_context(data_file), dry_run=dry_run),
            GovernedFileReference(
                file_id=data_file.file_manifest_id,
                current_path=data_file.path,
                classification=data_file.classification,
            ),
        )

    def _routing_context(self, data_file: DataFile) -> FileRoutingContext:
        """The routing primitive's operation-scoped context for this move."""
        application_name = str(getattr(self._ctx, "app_name", "") or "").strip()
        if not application_name:
            raise ConfigError(
                "Transform (move): the runtime context carries no 'app_name' to "
                "record the move under."
            )
        try:
            governed_root = Path(self._ctx.paths.resolve("data")).expanduser().resolve()
        except (AttributeError, ConfigError, TypeError, ValueError) as exc:
            raise ConfigError(
                "Transform (move): routing requires the configured 'data' path."
            ) from exc
        return FileRoutingContext(
            state_ctx=self._ctx,
            run_log=bound_run_log(),
            application_name=application_name,
            operation=self._operation,
            routes={self._role: _route_for(self._route, data_file)},
            governed_roots=(governed_root,),
            dry_run=False,
            destination_name=self._name,
            collision_policy=CollisionPolicy.OVERWRITE,
            file_operation_metadata={
                "pipeline_step_name": getattr(self._ctx, "pipeline_step_name", ""),
                "pipeline_step_id": getattr(self._ctx, "pipeline_step_id", ""),
            },
            # The move's lineage: the governed state it moved from, written into
            # the move's run-log evidence by routing.
            mutation_run_log_fields={"source_record_id": data_file.file_mutation_id},
            pipeline_name=getattr(self._ctx, "pipeline_name", None),
            workflow_name=getattr(self._ctx, "workflow_name", None),
        )


def _route_for(route: str, data_file: DataFile) -> str:
    """The route with ``<base_path>`` resolved from the file.

    Raises:
        ConfigError: If the route names ``<base_path>`` and the file carries
            none.
    """
    if _BASE_PATH_TOKEN not in route:
        return route
    base_path = str(data_file.base_path or "").strip()
    if not base_path:
        raise ConfigError(
            f"Transform (move): the route '{route}' needs the file's base_path, "
            f"and governed file {data_file.file_manifest_id} at mutation "
            f"{data_file.file_mutation_id} carries none."
        )
    return route.replace(_BASE_PATH_TOKEN, base_path)


def build_file_transform(
    ctx: Any,
    kind: str,
    configuration: Mapping[str, Any],
) -> FileTransform:
    """Build the FileTransform for one file-level kind's configuration.

    The file-level sibling of ``load_operation._build_transform``, and called
    only from :meth:`Transform.resolve`.

    Raises:
        ConfigError: If no file-level kind is registered as ``kind``.
    """
    registered = _REGISTRY.get(kind)
    if registered is None:
        raise ConfigError(
            f"Transform: no file-level kind is registered as '{kind}'. "
            f"File kinds: {', '.join(_REGISTRY)}."
        )
    return registered.builder(ctx, **dict(configuration))

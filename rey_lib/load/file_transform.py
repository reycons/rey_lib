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
from rey_lib.logs import bound_run_log, current_step, get_logger

__all__ = [
    "FileKind", "FileTransform", "MoveTransform", "build_file_transform",
    "file_kind", "file_kinds", "file_transform",
]

_logger = get_logger(__name__)

#: The bare lifecycle-root placeholder a route may carry. Resolved from the
#: DataFile's own ``base_path``; ``<classification.*>`` is routing's.
_BASE_PATH_TOKEN = "<base_path>"

#: The directory containing the ORIGINAL file as inventoried, from the file's
#: own ``inbox`` (backlog 624). Kickouts live beneath it.
_INBOX_TOKEN = "<inbox>"

#: The kickouts route is intrinsic, not configured: every kicked-out original
#: goes to the kickouts folder of the inbox it arrived in (backlog 624).
_KICKOUTS_ROUTE = f"{_INBOX_TOKEN}/kickouts"


class FileTransform(ABC):
    """One operation on one governed file.

    **THE ONE COMMON EXECUTION PATH** (backlog 624). :meth:`apply` is defined
    here, once, and no kind may define its own: a kind implements
    :meth:`_apply`. When ``_apply`` raises one of the kind's ``file_failures``
    -- a terminal failure of THIS FILE, never a configuration error -- the
    original file is moved to kickouts through ``Transform(kind="move",
    role="kickouts")``, from the facts the input DataFile carries, and the same
    error is raised again so the step's iteration records it and continues.
    """

    #: The errors that are a terminal failure of the file this kind operated
    #: on. Empty catches nothing: the move kind declares none, which is also
    #: why its own failure never triggers another move.
    file_failures: tuple[type[BaseException], ...] = ()

    #: The runtime context; every kind is built with it.
    _ctx: Any

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse a kind that would bypass the common execution path."""
        super().__init_subclass__(**kwargs)
        if "apply" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} defines apply(). A file-level kind implements "
                "_apply(); apply() is FileTransform's, so every kind runs "
                "through the one failure path (backlog 624)."
            )

    def apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Run this kind's operation on exactly this file; on a terminal
        failure of the file, move its original to kickouts and raise again.

        Args:
            data_file: The file, at the state the caller selected.

        Returns:
            What ``_apply`` returns.
        """
        try:
            return self._apply(data_file)
        except self.file_failures:
            original_path = data_file.original_path or str(data_file.path)
            original = data_file_for(Path(original_path), **{
                **data_file.governed_facts(),
                "file_mutation_id": (
                    data_file.original_mutation_id
                    if data_file.original_path
                    else data_file.file_mutation_id
                ),
                "original_path": original_path,
            })
            try:
                build_file_transform(self._ctx, "move", {"role": "kickouts"}).apply(original)
            except (ConfigError, file_routing.FileRoutingError, OSError) as kickout_error:
                # The file's failure is the error to raise; a move that could
                # not be made must not replace it. Routing has recorded it.
                _logger.warning("'%s' could not be kicked out: %s",
                                Path(original_path).name, kickout_error)
            raise

    @abstractmethod
    def _apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
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
                required=("role",))
class MoveTransform(FileTransform):
    """Move one governed file to a role's route.

    The same file comes back at its new path, under the same governed identity,
    at the state the move recorded. :meth:`plan` answers where it WOULD go,
    changing nothing.
    """

    #: Its own failure is never kicked out: routing has recorded the failed
    #: move, and a second move would recurse.
    file_failures = ()

    def __init__(
        self,
        ctx: Any,
        *,
        role: str,
        route: str | None = None,
        operation: str | None = None,
        name: str | None = None,
    ) -> None:
        """Hold the move's configuration and the runtime it runs in.

        Args:
            ctx: The runtime context: its ``app_name`` and its ``data`` path.
            role: processing | kickouts | failed | archive.
            route: The destination DIRECTORY. ``<base_path>`` and ``<inbox>``
                resolve from the file; ``<classification.*>`` resolves in
                routing. Kickouts need none: their route is intrinsic,
                ``<inbox>/kickouts`` (backlog 624).
            operation: The operation this move serves, which the move's
                evidence states. Unset: the bound workflow step's.
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
        declared = str(route).strip() if route is not None else ""
        if not declared and self._role is not FileRoutingRole.KICKOUTS:
            raise ConfigError(
                f"Transform (move): the {self._role.value} role needs a route; "
                "only kickouts has an intrinsic one."
            )
        self._ctx = ctx
        self._route = declared or _KICKOUTS_ROUTE
        self._operation = str(operation) if operation else None
        self._name = str(name).strip() if name else None

    def _apply(self, data_file: DataFile) -> tuple[DataFile, ...]:
        """Move the file, and return it at its new path.

        Raises:
            ConfigError: If the route needs a ``base_path`` or ``inbox`` the
                file does not carry, no operation is stated or bound, or the
                runtime has no application name or ``data`` path.
            FileRoutingError: As routing refuses or fails; routing has already
                recorded a failed move where the filesystem operation failed.
        """
        _logger.debug(
            "Moving governed file %s (mutation %s) to %s for %s",
            data_file.file_manifest_id, data_file.file_mutation_id,
            self._role.value, self._operation,
        )
        resulting_path, recorded = self._route_file(data_file, dry_run=False)
        # UNCHANGED is the file already where the route puts it: the same
        # state, so the same mutation. Every other governed outcome that
        # returns is a move routing recorded, and that record is the new state.
        # An ungoverned move records no mutation.
        file_mutation_id = data_file.file_mutation_id if recorded is None else recorded
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
            facts["original_path"] = str(resulting_path)
            facts["original_mutation_id"] = file_mutation_id
        return (
            data_file_for(
                resulting_path,
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
        return self._route_file(data_file, dry_run=True)[0]

    def _route_file(self, data_file: DataFile, *, dry_run: bool) -> tuple[Path, int | None]:
        """Hand this file to routing, and say where it is and what recorded it.

        One move; routing distinguishes PERSISTENCE beneath it. A governed file
        goes through the role's governed wrapper (the move plus its mutation);
        a file nothing governs yet goes through the one ungoverned primitive
        (the move and its run-log evidence -- there is no manifest to record a
        mutation against).

        Returns:
            Where the file is after the move, and the mutation that recorded
            it -- None when nothing was recorded (unchanged, planned, or an
            ungoverned file).
        """
        context = replace(self._routing_context(data_file), dry_run=dry_run)
        if data_file.file_manifest_id is None:
            return (
                file_routing.move_ungoverned(
                    context, data_file.path, destination_role=self._role),
                None,
            )
        # Looked up when called, from routing's published wrappers: one per
        # role, and the module's own -- never a copy taken at import.
        move = getattr(file_routing, f"move_to_{self._role.value}")
        result: FileRoutingResult = move(
            context,
            GovernedFileReference(
                file_id=data_file.file_manifest_id,
                current_path=data_file.path,
                classification=data_file.classification,
            ),
        )
        return result.resulting_path, result.file_manifest_record_id

    def _operation_for_evidence(self) -> str:
        """The operation the move states: the caller's, else the bound step's.

        A kickout is made by the common execution path on a kind's behalf, so
        nobody names an operation for it; the workflow step it happened in is
        bound (``current_step``), and that is what the move served.

        Raises:
            ConfigError: If none is named and no step is bound -- the mutation
                must state an operation, and guessing one is not allowed.
        """
        if self._operation:
            return self._operation
        step = current_step() or {}
        operation = str(step.get("step_id") or "").strip()
        if not operation:
            raise ConfigError(
                "Transform (move): no operation was named and no workflow step is "
                "bound, so the move cannot state what it served."
            )
        return operation

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
            operation=self._operation_for_evidence(),
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
    """The route with ``<base_path>`` and ``<inbox>`` resolved from the file.

    Raises:
        ConfigError: If the route names one the file carries none of.
    """
    for token, value in ((_BASE_PATH_TOKEN, data_file.base_path),
                         (_INBOX_TOKEN, data_file.inbox)):
        if token not in route:
            continue
        resolved = str(value or "").strip()
        if not resolved:
            raise ConfigError(
                f"Transform (move): the route '{route}' needs the file's "
                f"{token.strip('<>')}, and file {data_file.file_manifest_id} at "
                f"mutation {data_file.file_mutation_id} carries none."
            )
        route = route.replace(token, resolved)
    return route


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

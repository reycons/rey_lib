"""One live rollback of governed files: the stateful operation around FileManifest.rollback.

Backlog 1181 (user, 2026-10-26): a Rollback opening starts from one identity -- a
File Manifest file, a mutation (the boundary) or a Run History run -- and the live
thing presented is this rollback, not a File and not a governed Workflow. It owns
the execution protocol:

    request --preview--> confirmation --execute--> executing --> complete

The scope is chosen while requesting; a preview is FileManifest.rollback as a dry
run; execution is allowed only after a preview and applies exactly what that
preview showed -- if the scope's mutations changed since, it is refused and a new
preview is required. FileManifest.rollback stays the one rollback algorithm: this
object calls it, never repeats it.

Its projection carries its state and the complete commands it currently accepts
(``actions``), so whoever presents it sends them unchanged. Every change is told
to its observers (``observe``), as a Workflow tells its own.
"""

from __future__ import annotations

import threading

from typing import Any, Mapping, Optional

from rey_lib.errors.error_utils import AppError, ConfigError
from rey_lib.files.manifest import FileManifest

__all__ = ["COMPLETE", "CONFIRMATION", "EXECUTING", "FILE_SCOPES", "MUTATION_SCOPES",
           "REQUEST", "Rollback"]

#: The scopes a rollback from a selected mutation can take (backlog 612), in the
#: order offered.
MUTATION_SCOPES: tuple[str, ...] = ("file", "run", "file_type", "batch_step", "batch",
                                    "installation")

#: The scopes a selected FILE can take: it carries no run, batch or batch step.
FILE_SCOPES: tuple[str, ...] = ("file", "file_type", "installation")

#: A run is rolled back with scope run and nothing else.
_RUN_SCOPES: tuple[str, ...] = ("run",)

#: What an observer's display can fail with: the channel it writes to, or the
#: projection it serializes.
_OBSERVER_FAILURES = (AppError, OSError, TypeError, ValueError)

REQUEST = "request"
CONFIRMATION = "confirmation"
EXECUTING = "executing"
COMPLETE = "complete"


class Rollback:
    """One rollback of governed files, opened from the file, mutation or run it stands on."""

    def __init__(
        self,
        manifest: Any,
        *,
        file_manifest_id: Any = None,
        file_mutation_id: Any = None,
        run_id: Any = None,
    ) -> None:
        """Open the rollback of exactly one identity, requesting, with its default scope.

        Args:
            manifest: The installation's FileManifest, which performs the rollback.
            file_manifest_id: The selected File Manifest file, when no mutation was selected.
            file_mutation_id: The selected mutation: the rollback boundary.
            run_id: The Run History run.

        Raises:
            ConfigError: Unless exactly one identity is given, as a whole number.
        """
        given = {name: value for name, value in (
            ("file_manifest_id", file_manifest_id),
            ("file_mutation_id", file_mutation_id),
            ("run_id", run_id),
        ) if value is not None and str(value).strip() != ""}
        if len(given) != 1:
            raise ConfigError(
                "A rollback is opened from the selected file, the selected mutation or "
                "the run, exactly one.")
        ((name, value),) = given.items()
        try:
            identity = int(str(value).strip())
        except ValueError as exc:
            raise ConfigError(f"A rollback's {name} is a whole number, not {value!r}.") from exc
        self._manifest = manifest
        self._identity: dict[str, int] = {name: identity}
        self._scopes = {"run_id": _RUN_SCOPES, "file_manifest_id": FILE_SCOPES,
                        "file_mutation_id": MUTATION_SCOPES}[name]
        self._scope = self._scopes[0]
        self._phase = REQUEST
        self._preview: Optional[dict[str, Any]] = None
        self._result: Optional[dict[str, Any]] = None
        self._failure: Optional[str] = None
        self._observers: list[Any] = []
        # One operation at a time: a preview or execution runs off the reader's
        # thread, and a second one while it runs is refused, never queued.
        self._busy = threading.Lock()

    @classmethod
    def for_control(cls, control: Any, **identity: Any) -> "Rollback":
        """The rollback of one identity, performed through this installation's FileManifest."""
        return cls(FileManifest(control), **identity)

    # -- its observers -------------------------------------------------------

    def observe(self, observer: Any) -> None:
        """Be told, through ``observer.changed(rollback)``, each time this rollback changes."""
        self._observers.append(observer)

    def _changed(self) -> None:
        """Tell every observer this rollback changed.

        An observer cannot decide the rollback's outcome: a display failure is
        converted to AppError, reported once, and the next observer is still told.
        """
        for observer in list(self._observers):
            try:
                observer.changed(self)
            except _OBSERVER_FAILURES as exc:
                error = exc if isinstance(exc, AppError) else AppError(
                    "rollback: display observer failed")
                if error is not exc:
                    error.__cause__ = exc
                error.report("rollback observer notification", source=__name__)

    # -- its state -----------------------------------------------------------

    @property
    def identity(self) -> dict[str, int]:
        """The one identity this rollback stands on."""
        return dict(self._identity)

    @property
    def phase(self) -> str:
        """Where the rollback is: request, confirmation, executing or complete."""
        return self._phase

    @property
    def scope(self) -> str:
        """Which files the rollback reaches."""
        return self._scope

    def actions(self) -> list[dict[str, Any]]:
        """The complete commands this rollback accepts now; ``inputs`` names a value to supply."""
        if self._phase == EXECUTING:
            return []
        offered: list[dict[str, Any]] = []
        if len(self._scopes) > 1:
            offered.append({"id": "rollback_scope", "command": {"command": "choose_scope"},
                            "inputs": {"scope": list(self._scopes)}})
        offered.append({"id": "rollback_preview", "command": {"command": "preview"}})
        if self._phase == CONFIRMATION:
            offered.append({"id": "rollback_execute", "command": {"command": "execute"}})
        return offered

    def projection(self) -> dict[str, Any]:
        """What this rollback holds, and the commands it accepts."""
        return {
            "identity": self.identity,
            "scope": self._scope,
            "scopes": list(self._scopes),
            "phase": self._phase,
            "preview": self._preview,
            "result": self._result,
            "failure": self._failure,
            "actions": self.actions(),
        }

    # -- its behaviour -------------------------------------------------------

    def choose_scope(self, scope: Any) -> None:
        """Reach ``scope``'s files; what was previewed or done for another scope is discarded.

        Raises:
            ConfigError: While another operation runs, or for a scope this rollback
                does not offer.
        """
        chosen = str(scope or "").strip()
        if chosen not in self._scopes:
            raise ConfigError(
                f"This rollback offers the scopes {', '.join(self._scopes)}, not {chosen!r}.")
        with _Operation(self, "choose a scope"):
            self._scope = chosen
            self._phase, self._preview, self._result, self._failure = REQUEST, None, None, None
        self._changed()

    def preview(self) -> None:
        """What rolling back would reverse, changing nothing: FileManifest.rollback as a dry run.

        A refusal by the manifest is held as this rollback's failure, and it stays
        requesting.

        Raises:
            ConfigError: While another operation runs.
        """
        with _Operation(self, "preview"):
            self._result, self._failure = None, None
            try:
                self._preview = self._rollback(dry_run=True)
            except (ValueError, AppError) as refused:
                self._preview, self._phase = None, REQUEST
                self._failure = str(refused)
            else:
                self._phase = CONFIRMATION
        self._changed()

    def execute(self) -> None:
        """Reverse exactly what the preview showed: FileManifest.rollback, applied.

        Refused unless previewed. When the scope's mutations are no longer the
        ones previewed, nothing is reversed: the rollback returns to requesting
        and asks for a new preview. The result -- including a reversal the
        manifest could not do -- is held, and the rollback is complete.

        Raises:
            ConfigError: Unless previewed, or while another operation runs.
        """
        with _Operation(self, "execute"):
            if self._phase != CONFIRMATION or self._preview is None:
                raise ConfigError("Preview the rollback before executing it.")
            previewed = _mutation_ids(self._preview)
            self._phase = EXECUTING
            self._changed()
            try:
                if _mutation_ids(self._rollback(dry_run=True)) != previewed:
                    self._phase, self._preview = REQUEST, None
                    self._failure = ("What this rollback reaches changed since the preview; "
                                     "preview it again.")
                else:
                    self._result = self._rollback(dry_run=False)
                    failed = self._result.get("failed")
                    self._failure = (str(failed.get("reason") or "A reversal failed.")
                                     if failed else None)
                    self._phase = COMPLETE
            except (ValueError, AppError) as refused:
                self._phase, self._preview, self._failure = REQUEST, None, str(refused)
        self._changed()

    def _rollback(self, *, dry_run: bool) -> dict[str, Any]:
        """FileManifest.rollback for this identity and scope."""
        return self._manifest.rollback(scope=self._scope, dry_run=dry_run, **self._identity)


class _Operation:
    """The rollback's one operation at a time: entered, or refused while another runs."""

    def __init__(self, rollback: Rollback, what: str) -> None:
        """Name the operation ``what`` that wants to run on ``rollback``."""
        self._lock = rollback._busy
        self._what = what

    def __enter__(self) -> None:
        """Take the rollback's operation, or refuse.

        Raises:
            ConfigError: While another operation -- an execution included -- runs.
        """
        if not self._lock.acquire(blocking=False):
            raise ConfigError(f"The rollback is busy; cannot {self._what} now.")

    def __exit__(self, *exc_info: Any) -> None:
        """Release the rollback's operation."""
        self._lock.release()


def _mutation_ids(answer: Mapping[str, Any]) -> list[int]:
    """The mutations a FileManifest.rollback answer reaches, in its order."""
    return [int(row["file_mutation_id"]) for row in answer.get("mutations") or []]

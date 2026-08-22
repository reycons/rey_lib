"""
The Run: one identity, created by recording it.

A Run exists because a row exists. ``Run.start`` inserts into
``control.run_manifest``, the database generates ``run_manifest_id``, and that
value *is* the Run identity -- the application calls it ``run_id``. There is no
separately minted id and no translation table: one value, two vocabularies, and
the procedure map binding is the only place the two names meet.

That is what makes a finished run knowable. Before this, a run was reconstructed
by reading its JSONL log, so a run whose log was unreachable was unreachable
itself, and a client had to carry a path to ask about one. The durable row
outlives the process, so a poll that arrives after the process is gone is
answered from the row rather than failing.

What this is not
----------------
Not a registry of live runs. While a run executes, its live state is owned by
the execution service and the active-run record it keeps; this object is the
durable identity and the reconstruction of a finished run. The two lifetimes
stay separate: whether a *process* is alive is the execution service's answer,
and what the *Run* is, is this.

Not a second reader of execution evidence. Status, progress, current step and
metrics during execution are derived from the run's own records by
``rey_lib.run.state``, which takes a log path and stays identity-free. Nothing
here duplicates that, and the terminal read deliberately does not reach for it:
telling a caller the run has finished needs only the manifest row.

Layering
--------
``rey_lib.run`` owns the Run domain, and two shortcuts are prohibited: it must
not reach the DB adapter directly, and ``rey_lib.logs`` must not reach up into
it. So the manifest is reached through ``Control`` -- the same chain every other
control operation uses -- and nothing in the logging layer imports this module.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["Run"]


class Run:
    """One execution, identified by the manifest row that records it."""

    def __init__(
        self,
        *,
        run_id: int,
        control: Any,
        subject_type: str = "",
        subject_id: str = "",
        subject_name: str = "",
        app_name: str = "",
        parent_run_id: Optional[int] = None,
        settings: Optional[dict[str, Any]] = None,
        status: str = "RUNNING",
        started_at: str = "",
        finished_at: str = "",
    ) -> None:
        """Bind an identity to the Control that recorded it.

        Constructed by :meth:`start` or :meth:`open` rather than directly: a Run
        without a manifest row behind it has an identity nothing else can
        resolve.

        Parameters
        ----------
        run_id : int
            The manifest's ``run_manifest_id``.
        control : Any
            The ``Control`` this run reaches its manifest through.
        subject_type, subject_id, subject_name : str
            What ran -- kind, identifier and display name. Creation facts,
            supplied by the launch site, never recovered later from evidence.
        app_name : str
            The executing owner, which is not the subject.
        parent_run_id : int | None
            The run this one belongs to, for a child execution.
        settings : dict | None
            The launch request, recorded once at creation.
        status, started_at, finished_at : str
            Lifecycle facts as the manifest holds them.
        """
        self.run_id = int(run_id)
        self._control = control
        self.subject_type = subject_type
        self.subject_id = subject_id
        self.subject_name = subject_name
        self.app_name = app_name
        self.parent_run_id = parent_run_id
        self.settings = dict(settings or {})
        self.status = status
        self.started_at = started_at
        self.finished_at = finished_at

    def __repr__(self) -> str:
        """Identify the run by id, subject and status."""
        return (f"Run(run_id={self.run_id}, subject={self.subject_type}:"
                f"{self.subject_id}, status={self.status})")

    # -- creation -----------------------------------------------------------

    @classmethod
    def start(
        cls,
        control: Any,
        *,
        subject_type: str,
        subject_id: str,
        subject_name: str = "",
        app_name: str = "",
        parent_run_id: Optional[int] = None,
        settings: Optional[dict[str, Any]] = None,
    ) -> "Run":
        """Record a starting run and return it, carrying the id it was given.

        Recording is what creates the identity, so this is the first thing a
        launch does and everything downstream receives the result. A failure
        here is a failure to start: there is no id to run under, so it raises
        rather than degrading to an unrecorded run.

        The subject facts are arguments because they are creation facts. Each
        launch site already holds them -- a workflow route knows its workflow, a
        pipeline coordinator knows its pipeline -- and passing them here is what
        stops them being reconstructed later from a log.

        Parameters
        ----------
        control : Any
            The ``Control`` for this installation's control database.
        subject_type : str
            What kind of thing ran: ``workflow``, ``pipeline``, ``app``.
        subject_id : str
            Its identifier, stable across runs of the same subject.
        subject_name : str
            Display name; falls back to ``subject_id``.
        app_name : str
            The executing owner application.
        parent_run_id : int | None
            The parent run for a child execution.
        settings : dict | None
            The launch request as configured.

        Returns
        -------
        Run
            The started run, with ``run_id`` set from the manifest.

        Raises
        ------
        DatabaseError
            If the run could not be recorded, and therefore has no identity.
        """
        run_id = control.create_run_manifest(
            subject_type=subject_type,
            subject_id=subject_id,
            subject_name=subject_name or subject_id,
            app_name=app_name,
            parent_run_id=parent_run_id,
            settings=settings,
            required=True,
        )
        return cls(
            run_id=int(run_id),
            control=control,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_name=subject_name or subject_id,
            app_name=app_name,
            parent_run_id=parent_run_id,
            settings=settings,
        )

    # -- reconstruction -----------------------------------------------------

    @classmethod
    def open(cls, control: Any, run_id: int) -> Optional["Run"]:
        """Return the durable Run for ``run_id``, or None if it was never recorded.

        No process, no log path and no evidence: the row is the whole answer.
        This is what a poll falls through to once the live run is gone, and
        keeping it to the row is why a finished run stays knowable for as long
        as the manifest keeps it.

        Parameters
        ----------
        control : Any
            The ``Control`` for this installation's control database.
        run_id : int
            The manifest's ``run_manifest_id``.

        Returns
        -------
        Run | None
            The reconstructed run, or None when no such run was recorded.
        """
        row = control.get_run_manifest(int(run_id))
        if not row:
            return None
        return cls(
            run_id=int(row.get("run_manifest_id") or run_id),
            control=control,
            subject_type=str(row.get("subject_type") or ""),
            subject_id=str(row.get("subject_id") or ""),
            subject_name=str(row.get("subject_name") or ""),
            app_name=str(row.get("app_name") or ""),
            parent_run_id=row.get("parent_run_manifest_id"),
            settings=row.get("settings") or {},
            status=str(row.get("status") or ""),
            started_at=_text(row.get("started_at")),
            finished_at=_text(row.get("finished_at")),
        )

    # -- completion ---------------------------------------------------------

    def finish(self, status: str, finished_at: Optional[str] = None) -> None:
        """Record the terminal status, after which the live run may disappear.

        Written before the live run is released, so there is never a moment when
        neither the live run nor the manifest can answer for this id.

        Parameters
        ----------
        status : str
            The terminal status, e.g. ``SUCCEEDED``, ``FAILED``, ``ABORTED``.
        finished_at : str | None
            When it ended; the database stamps the current time when omitted.
        """
        self._control.finish_run_manifest(
            self.run_id, status, finished_at=finished_at, required=True)
        self.status = status
        if finished_at:
            self.finished_at = finished_at

    # -- reporting ----------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Return this run's durable state, in the shape a reader polls for.

        The durable facts only. Progress, current step and metrics belong to a
        live run and are derived from its evidence by ``rey_lib.run.state``;
        a finished run reports what the manifest holds, which is what tells a
        reader the run is over.
        """
        return {
            "run_id":        self.run_id,
            "found":         True,
            "live":          False,
            "status":        self.status,
            "subject_type":  self.subject_type,
            "subject_id":    self.subject_id,
            "subject_name":  self.subject_name,
            "app_name":      self.app_name,
            "parent_run_id": self.parent_run_id,
            "settings":      dict(self.settings),
            "started_at":    self.started_at,
            "finished_at":   self.finished_at,
        }


def _text(value: Any) -> str:
    """Render a timestamp as ISO text, and anything absent as an empty string."""
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)

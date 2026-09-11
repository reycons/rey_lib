"""The phases of one run, as a contiguous partition of its elapsed time.

A run's declared steps do not account for all of it. Work happens before the
first step and after the last -- composing the context, resolving the workflow,
summarizing the log, and interpreting it -- and none of that is a step, so a
step summary can never say where the time went.

**A phase is named when it is entered, never when it ends.** That is what lets
the timeline know what is running right now, and it is what makes termination
correct: when a phase raises, the interval it was in already has its name, and
the timeline closes it. Naming on completion would leave that interval unnamed
and force the closer to invent a label for work it did not describe.

**One timestamp per transition, shared by both sides.** ``enter`` closes the
active phase and opens the next from the same value. Two adjacent clock reads
would leave real elapsed time belonging to no phase, and the only way to absorb
that is a tolerance -- which is the hiding place this exists to remove. Here the
durations telescope to the total exactly, so an unattributed gap is impossible
by construction rather than merely unlikely.

    PhaseTimeline(started=t0, phase="bootstrap")
    enter("resolve")     bootstrap -> resolve
    enter("steps")       resolve   -> steps
    finish()             steps     -> tn, and no phase is created

Monotonic throughout. Wall-clock timestamps describe *when* a run happened and
remain the audit record; they are not the duration authority, because a clock
adjustment mid-run would manufacture an overlap or a gap in exactly the signal
being looked for.

Nested timings are deliberately not modelled. A phase may contain steps that
are timed separately, and those durations are evidence in their own right --
but adding them to this partition would double-count and break the sum.
"""

from __future__ import annotations

import time
from typing import Any

__all__ = ["PhaseTimeline"]


class PhaseTimeline:
    """Contiguous phases of one run, measured from a single monotonic clock."""

    __slots__ = ("_started", "_boundaries", "_ended")

    def __init__(self, started: float, phase: str) -> None:
        """Open the timeline with its first phase already running.

        Both arguments are required. A timeline that could be built without a
        boundary mark would silently measure from whenever it happened to be
        constructed, and a timeline with no opening phase would have an unnamed
        interval at its head -- the two failures this design exists to prevent.

        Args:
            started: The monotonic value the measured run began at.
            phase: What is running from that instant. Nothing "enters" it; it
                is active from the start.

        Raises:
            ValueError: If the opening phase is unnamed.
        """
        self._started = float(started)
        self._boundaries: list[tuple[str, float]] = [(_named(phase), float(started))]
        self._ended: float | None = None

    def enter(self, phase: str) -> None:
        """Close the active phase and open ``phase`` at the same instant.

        One value, used as both an end and a start, so the partition
        telescopes.

        A repeated name is not merged. Two stretches genuinely named the same
        are two intervals, and combining them would report a duration no single
        stretch of the run ever took.

        Args:
            phase: The phase being entered.

        Raises:
            ValueError: If the phase is unnamed, or the timeline has finished.
                Entering after termination would extend a partition that has
                already been reported as complete.
        """
        if self._ended is not None:
            raise ValueError("Cannot enter a phase after the timeline has finished.")
        self._boundaries.append((_named(phase), time.monotonic()))

    def finish(self) -> bool:
        """Close the active phase at the termination boundary, creating no successor.

        The end of the measured run, which is a different thing from a
        transition between two phases -- hence a different operation.

        Returns:
            True when this call terminated the run, False when it had already
            terminated. A run ends once, and the caller needs to know whether
            it was the one that ended it: a finalizer reached from a ``finally``
            as well as a return path would otherwise persist the same timeline
            twice, and one run would have two records claiming to be its whole
            partition.
        """
        if self._ended is not None:
            return False
        self._ended = time.monotonic()
        return True

    def active_phase(self) -> str | None:
        """Return what is running now, or None once the timeline has finished."""
        return None if self._ended is not None else self._boundaries[-1][0]

    def phases(self) -> list[dict[str, Any]]:
        """Return each closed phase with its duration, in order.

        The active phase is absent until ``finish`` closes it: reporting a
        duration for something still running would be a measurement of when it
        was asked, not of how long it took.

        Returns:
            One entry per closed phase: ``phase`` and ``duration_ms``.
        """
        if self._ended is None:
            closing = [start for _name, start in self._boundaries[1:]]
        else:
            closing = [start for _name, start in self._boundaries[1:]] + [self._ended]

        entries: list[dict[str, Any]] = []
        previous_ms = 0
        for (name, _start), ends_at in zip(self._boundaries, closing):
            # Each boundary is converted to milliseconds *once*, and a phase is
            # the difference between two converted boundaries. Converting each
            # interval separately would truncate every one of them, and the
            # truncations would not cancel -- the partition would drift from
            # the total and need a tolerance to hide it. Sharing a boundary
            # means sharing its rounding.
            elapsed_ms = int((ends_at - self._started) * 1000)
            entries.append({"phase": name, "duration_ms": elapsed_ms - previous_ms})
            previous_ms = elapsed_ms
        return entries

    def total_ms(self) -> int:
        """Return the elapsed time the phases partition.

        Measured to the termination boundary, not to now: a timeline is read
        after the run, and the read is not part of what it measures. Zero while
        the run is still going, because no complete partition exists yet.
        """
        if self._ended is None:
            return 0
        return int((self._ended - self._started) * 1000)

    def unattributed_ms(self) -> int:
        """Return elapsed time belonging to no phase.

        Zero by construction, because the boundaries are shared. It is reported
        rather than assumed: the value of this partition is that a gap cannot
        hide inside it, and an invariant nobody checks is a comment.
        """
        return self.total_ms() - sum(entry["duration_ms"] for entry in self.phases())


def _named(phase: str) -> str:
    """Return a phase name, refusing an empty one.

    Args:
        phase: The candidate name.

    Returns:
        The trimmed name.

    Raises:
        ValueError: If it is empty. An unnamed interval is exactly the hidden
            work this partition exists to rule out.
    """
    name = str(phase or "").strip()
    if not name:
        raise ValueError("A phase must be named; an unnamed interval hides work.")
    return name

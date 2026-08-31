"""The current settings, and the one place they are answered.

An immutable value. The root owns it and replaces it; nothing else holds a
second copy, and a consumer that wants to react observes the root rather than
keeping its own.

Two levels, because an AI runtime serves more than one purpose:

    default     what applies when nothing more specific does
    tasks       overrides for one named purpose, each field optional

A **task** names what the AI is being asked to do -- commenting code, generating
it, interpreting a log. It is not a profile and not an instruction: those are
which engine answers and which contract governs, and one task selects both. The
collection is overrides only, never the set of valid purposes, so a task named
nowhere here inherits the defaults rather than being refused. That is what lets
a new consumer arrive without editing this list first.

Settings identity and policy only. No provider client, no callback, no loaded
session, no editor state, and no authorization envelope -- those belong to
whoever owns them, and putting any of them here is how a settings value becomes
a second runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

__all__ = ["AISettings", "AISettingsTask"]


@dataclass(frozen=True)
class AISettingsTask:
    """What one named purpose overrides.

    Every field except the name is optional, and absent means **inherit** -- the
    empty string for an identity and ``None`` for a value, which is how the
    defaults already spell absence. A task overriding one field keeps inheriting
    the rest, so a caller changing a temperature does not silently pin a model.
    """

    name: str
    profile_id: str = ""
    instruction_id: str = ""
    temperature: float | None = None
    representation: str = ""

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("An AI settings task must be named.")


@dataclass(frozen=True)
class AISettings:
    """What is selected: globally, and per task.

    ``representation`` is the representation this runtime *asks* for. It is not
    the authorization envelope -- which representations a configured model may
    ever receive is ``profile_access.allowed`` on the profile, checked against
    this. Changing a setting therefore cannot widen what a model is permitted,
    which is why the envelope is not carried here.
    """

    profile_id: str = ""
    instruction_id: str = ""
    temperature: float | None = None
    representation: str = ""
    tasks: tuple[AISettingsTask, ...] = field(default_factory=tuple)

    def task(self, name: str) -> AISettingsTask | None:
        """The overrides configured for ``name``, or nothing.

        Nothing is the ordinary answer for a purpose no one configured, not a
        failure: the caller then gets the defaults.
        """
        wanted = str(name or "").strip()
        if not wanted:
            return None
        for configured in self.tasks:
            if configured.name == wanted:
                return configured
        return None

    def with_profile(self, profile_id: str) -> "AISettings":
        """The same settings with a different profile selected."""
        return replace(self, profile_id=str(profile_id or ""))

    def with_instruction(self, instruction_id: str) -> "AISettings":
        """The same settings with a different instruction selected."""
        return replace(self, instruction_id=str(instruction_id or ""))

    def with_temperature(self, temperature: float | None) -> "AISettings":
        """The same settings at a different temperature."""
        return replace(self, temperature=temperature)

    def with_representation(self, representation: str) -> "AISettings":
        """The same settings requesting a different representation."""
        return replace(self, representation=str(representation or ""))

    def with_task(self, task: AISettingsTask) -> "AISettings":
        """The same settings with one task's overrides replaced.

        Replaces rather than accumulates, so applying a change twice leaves one
        entry, and keeps the configured order so a reader meets tasks where
        configuration put them. A task not yet present is appended.
        """
        existing = [configured.name for configured in self.tasks]
        if task.name in existing:
            return replace(self, tasks=tuple(
                task if configured.name == task.name else configured
                for configured in self.tasks
            ))
        return replace(self, tasks=(*self.tasks, task))

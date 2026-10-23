"""The presentation projection of one resolved governed subject (backlog 1215).

    governed subject -> Projection.for_subject -> Projection -> Console presentation registry -> Panel

``Projection.for_subject`` is the one place a subject's presentation is resolved:
it reads the subject's governed declaration (tree.subject_panel, through
``Control.subject_panel``) and returns the Projection that carries it -- the
Panel, its settings, and the subject's projected content. It consumes an
already-resolved subject key: how the subject was opened, which application
hosts it, and how its key was resolved (a file's format, for one) are not its
concern. A subject with no declaration is refused, never given a default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from rey_lib.errors.error_utils import ConfigError

__all__ = ["Projection"]

#: The keys the Projection itself carries; projected content may not name them.
#: ``subject_key`` is the governed presentation lookup key -- never an identity.
#: The canonical object's own ``subject`` (its subject identity) is content.
_DECLARATION_KEYS = frozenset({"subject_key", "panel", "panel_settings"})


@dataclass(frozen=True)
class Projection:
    """One resolved subject's declared presentation, beside its projected content.

    Attributes:
        subject_key: The governed presentation lookup key the host resolved
            (e.g. ``file.yaml``). Not an identity: the object's own subject
            identity, where it has one, travels in its content.
        panel: The Panel its governed declaration names.
        panel_settings: The settings that declaration gives the Panel.
        content: The subject's projected state, as its owner projected it.
    """

    subject_key: str
    panel: str
    panel_settings: Mapping[str, Any]
    content: Mapping[str, Any]

    @classmethod
    def for_subject(
        cls, control: Any, subject: str, content: Mapping[str, Any],
    ) -> Projection:
        """The Projection of ``subject``: its governed presentation and ``content``.

        Args:
            control: The shared Control, through which the declaration is read.
            subject: The resolved subject key.
            content: The subject's projected state, carried unread.

        Raises:
            ConfigError: When no subject is named, no presentation is declared
                for it, the declaration is malformed, or the content names a
                key the Projection itself carries.
        """
        key = str(subject or "").strip()
        if not key:
            raise ConfigError("A Projection is for a resolved subject; none was named.")
        _refuse_clashing(key, content)
        declared = control.subject_panel(key)
        if declared is None:
            raise ConfigError(f"No governed presentation is declared for '{key}'.")
        panel = str(declared.get("panel") or "").strip()
        settings = declared.get("panel_settings")
        if not panel or not isinstance(settings, Mapping):
            raise ConfigError(f"The governed presentation of '{key}' is malformed: {declared!r}.")
        return cls(subject_key=key, panel=panel, panel_settings=dict(settings),
                   content=dict(content))

    def projecting(self, content: Mapping[str, Any]) -> Projection:
        """The same governed declaration over new projected content.

        The declaration is read once, when the subject is established; each
        later projection of its changing state carries that same declaration.

        Raises:
            ConfigError: When the content names a key the Projection itself carries.
        """
        _refuse_clashing(self.subject_key, content)
        return Projection(subject_key=self.subject_key, panel=self.panel,
                          panel_settings=dict(self.panel_settings), content=dict(content))

    def to_dict(self) -> dict[str, Any]:
        """The Projection as it travels: its declaration beside its content."""
        return {
            "subject_key": self.subject_key,
            "panel": self.panel,
            "panel_settings": dict(self.panel_settings),
            **dict(self.content),
        }


def _refuse_clashing(subject_key: str, content: Mapping[str, Any]) -> None:
    """Refuse projected content that names a key the Projection itself carries.

    Raises:
        ConfigError: When it does.
    """
    clashing = sorted(_DECLARATION_KEYS & set(content))
    if clashing:
        raise ConfigError(
            f"The projected content of '{subject_key}' names {clashing}, which the "
            "Projection itself carries."
        )

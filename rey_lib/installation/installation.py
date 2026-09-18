"""The installation, as an object.

The installation is the estate's most load-bearing identity and had none. It was
two lines of YAML wrapped in a namespace, so every consumer re-implemented the
read -- five readers across three files, no two alike, and one of them wrong in
a way that failed silently: ``str()`` of a namespace is truthy, so a caller
stringifying it got ``"namespace(name='local')"`` and never reached its fallback.

This object is **identity only**: which installation this is. It holds no paths,
no runtime block, no logging or security configuration. Those remain their own
context areas, and ``ctx.paths`` remains the single authority on path state and
the only thing that resolves a path. An ``Installation.paths`` would be a second
way to reach one authority, which is the drift this exists to remove.
"""

from __future__ import annotations

from typing import Any

from rey_lib.errors.error_utils import ConfigError


class Installation:
    """Which installation a context belongs to.

    Built once, from the ``installation:`` block of the installation's YAML, and
    assigned to ``ctx.installation``. It is not registered with the runtime
    object registry: registration is for objects holding a resource with a
    lifecycle to close, and this holds configuration and owns nothing.
    """

    def __init__(
        self,
        *,
        name: Any,
        type: Any = None,
        installation_id: int | None = None,
    ) -> None:
        """Normalize the declared identity, once.

        Args:
            name: The declared installation name. Required and non-blank -- an
                installation that cannot say which one it is has no identity,
                and every consumer downstream would have to guess.
            type: The declared installation type, or None where the
                configuration declares none. Neither defaulted nor validated
                here: what a type *means* is the consuming application's
                vocabulary, not this object's.
            installation_id: The registry id, where it is already known --
                a child process receives its parent's rather than resolving a
                second time. None where it has not been resolved yet, which is
                every context at construction: the id comes from the control
                database, and configuration does not reach a database.

        Raises:
            ConfigError: If ``name`` is absent or blank.
        """
        resolved_name = str(name or "").strip()
        if not resolved_name:
            raise ConfigError(
                "The installation configuration declares no name. Every governed "
                "record, path and run is attributed to an installation, so a "
                "context that cannot name its own has nothing to attribute to."
            )
        self._name = resolved_name

        # A declared-but-blank type is the same as no type: the configuration
        # said nothing. Preserved as None rather than "" so one absent value has
        # one spelling.
        declared_type = str(type or "").strip()
        self._type: str | None = declared_type or None

        self._installation_id: int | None = (
            int(installation_id) if installation_id is not None else None
        )

    @property
    def name(self) -> str:
        """Return the installation's name."""
        return self._name

    @property
    def installation_id(self) -> int | None:
        """Return the registry id, or None where it has not been resolved.

        None is an ordinary state rather than a failure. Not every installation
        configures a control database -- the Console's own does not -- and there
        is no registry to resolve against without one. A consumer that needs the
        id refuses at the point of use, naming what is missing.
        """
        return self._installation_id

    def attach_installation_id(self, installation_id: int) -> None:
        """Record the resolved registry id, once.

        The one transition this object has. It is resolved at bootstrap, from
        the control database, and handed here -- this object holds identity and
        does not go to a database for it.

        Re-attaching the SAME value is accepted, because a re-entered bootstrap
        or an adopting child is not an error. A DIFFERENT value raises: two
        answers to one identity is the defect this whole line of work exists to
        remove, and silently keeping either one would be choosing which records
        get misattributed.

        Idempotence here is a guard against contradiction, not a licence to
        resolve twice -- the caller is expected to skip resolution entirely when
        the id is already known.

        Args:
            installation_id: The id ``control.installation`` holds for this key.

        Raises:
            ConfigError: If a different id is already attached.
        """
        resolved = int(installation_id)
        if self._installation_id is not None and self._installation_id != resolved:
            raise ConfigError(
                f"Installation {self._name!r} is already attached to id "
                f"{self._installation_id} and cannot be reattached to {resolved}. "
                "One installation has one registry id; two answers means two "
                "control databases, or a registry that moved underneath a "
                "running process."
            )
        self._installation_id = resolved

    @property
    def type(self) -> str | None:
        """Return the declared installation type, or None where none was declared.

        Deliberately undefaulted. The Console decides what an absent type means
        for what it shows, and rey_lib does not carry application vocabulary.
        """
        return self._type

    def to_dict(self) -> dict[str, Any]:
        """Return the identity surface as plain data.

        The estate's one convention for "an object states its own plain form":
        ``rey_lib.config.inventory.to_plain_data`` asks every value for this, so
        implementing it makes the object correct on every surface that hands
        configuration outward -- an inventory dump, a Tree payload, an API
        response, a pipeline context snapshot -- rather than only the one that
        prompted it.

        The surface is what this object models, not whatever the YAML happened
        to contain. A key added under ``installation:`` becomes part of it by a
        deliberate change here, and not before.

        ``installation_id`` travels so a child inherits the id its parent
        resolved rather than resolving it again. Two resolutions of one identity
        is the defect; the lookup is not.

        Returns:
            ``name``, ``type`` and ``installation_id``, each None where not
            declared or not yet resolved.
        """
        return {
            "name": self._name,
            "type": self._type,
            "installation_id": self._installation_id,
        }

    def __str__(self) -> str:
        """Return the installation's name.

        An installation's string form is its name. This is what every caller
        that stringified the old namespace was reaching for, and what two of
        them got wrong -- so the mistake stops being expressible rather than
        merely being fixed at the sites that made it.
        """
        return self._name

    def __repr__(self) -> str:
        """Return an unambiguous form, for a log line or a traceback."""
        return (
            f"Installation(name={self._name!r}, type={self._type!r}, "
            f"installation_id={self._installation_id!r})"
        )

    def __eq__(self, other: object) -> bool:
        """Compare by declared identity.

        The resolved id is deliberately excluded. Two objects naming the same
        installation ARE the same installation, whether or not one of them has
        been to the database yet -- the id is what the registry calls it, not
        what it is.
        """
        if not isinstance(other, Installation):
            return NotImplemented
        return self._name == other._name and self._type == other._type

    def __hash__(self) -> int:
        """Hash by declared identity, so the object may key a mapping.

        Excluding the resolved id is what keeps this stable: attaching one
        must not move an object already used as a key.
        """
        return hash((self._name, self._type))

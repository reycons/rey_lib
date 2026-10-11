"""One canonical object for every choice a mapping's Transform dropdown offers.

    TransformChoice
        name, authorable, declaration     what it is and what it starts as
        setting                           the ONE authored setting, if any,
                                          stating its own control
        settings_for / edit               what a surface draws, and what an
                                          edit makes of the declaration
        execute                           how it applies to one value

**AT MOST ONE SETTING.** A rule applies to the row's own input value, so the
input is never a setting; what is left to author is one value at most.

**THE SURFACE INFERS NOTHING.** The setting says which control it is --
``select``, ``text`` or ``none`` -- and, for a ``select``, exactly the choices
it offers. A surface renders that and sends edits back; it never knows which
choice needs which control.

The registry of choices, ``TRANSFORM_CHOICES``, sits beside the rules it
executes in :mod:`rey_lib.data.column_transform`, as the starters always have:
the dispatch owns the types. This module holds the object and its contract.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Optional

from rey_lib.errors.error_utils import ConfigError

__all__ = [
    "CONTROLS",
    "ChoiceSetting",
    "RuleCall",
    "TransformChoice",
    "source_metadata_names",
]

#: The controls a setting may state. A surface renders exactly the one given.
CONTROLS: tuple[str, ...] = ("select", "text", "none")

#: How a stored rule refers to what the canonical Source exposes.
SOURCE_PREFIX = "source."


@dataclass(frozen=True)
class RuleCall:
    """Everything one rule may read while applying to one value of one row."""

    db_col: str
    value: Any
    rule: dict[str, Any]
    out: dict[str, Any]
    raw_row: Any
    secrets: dict[str, str]
    row_num: int = 0
    ctx: Any = None
    source: Any = None


@dataclass(frozen=True)
class ChoiceSetting:
    """One authored part of a choice's declaration, and the control that authors it.

    Attributes:
        name: The setting, as a surface addresses it.
        label: What a surface shows beside it.
        control: ``select``, ``text`` or ``none``.
        key: The declaration key it authors.
        choices: For a ``select``: what it offers, asked of the canonical Source.
        shown: The stored value as a surface shows it; the value itself where None.
        stored: A shown value as the declaration stores it; the value itself where None.
    """

    name: str
    label: str
    control: str
    key: str
    choices: Optional[Callable[[Any], list[str]]] = None
    shown: Optional[Callable[[Any], Any]] = None
    stored: Optional[Callable[[Any], Any]] = None


@dataclass(frozen=True)
class TransformChoice:
    """One choice of the Transform dropdown: what it is, how it is authored, how it runs.

    Attributes:
        name: The rule ``type`` it is.
        authorable: Whether a surface may offer it.
        declaration: The declaration it starts as.
        execute: Applies it to one value of one row.
        setting: The one authored part of its declaration, or None where
            nothing is authored.
        deferred: Computed after every other column of the row (``hash``).
    """

    name: str
    authorable: bool
    declaration: dict[str, Any]
    execute: Callable[[RuleCall], Any]
    setting: Optional[ChoiceSetting] = None
    deferred: bool = False

    def starter(self) -> dict[str, Any]:
        """A fresh copy of the declaration it starts as."""
        return deepcopy(self.declaration)

    def settings_for(self, rule: Optional[dict[str, Any]], source: Any) -> list[dict[str, Any]]:
        """What a surface draws for one rule of this choice.

        ``{name, label, control, value, choices}`` for its one setting, or
        nothing where it has none. ``choices`` is
        exactly what a ``select`` offers -- asked of the canonical Source now --
        and empty for every other control.
        """
        one = self.setting
        if one is None:
            return []
        return [{
            "name": one.name,
            "label": one.label,
            "control": one.control,
            "value": _shown(one, (rule or {}).get(one.key)),
            "choices": _offered(one, source),
        }]

    def edit(
        self, rule: Optional[dict[str, Any]], setting: str, value: Any, source: Any,
    ) -> dict[str, Any]:
        """The rule with one setting authored -- a new mapping, the given one untouched.

        Raises:
            ConfigError: If this choice has no such setting, a ``select`` is given
                a value it does not offer, or a structured value is not readable.
        """
        found = self.setting
        if found is None or found.name != setting or found.control == "none":
            raise ConfigError(
                f"Transform '{self.name}': no setting '{setting}'. "
                f"Setting: {found.name if found else 'none'}."
            )
        text = "" if value is None else str(value)
        if found.control == "select":
            offered = _offered(found, source)
            if text not in offered:
                raise ConfigError(
                    f"Transform '{self.name}': '{text}' is not one of the {found.label} "
                    f"choices ({', '.join(offered) or 'none'})."
                )
        built = deepcopy(dict(rule or self.declaration))
        built["type"] = self.name
        built[found.key] = (
            found.stored(text) if found.stored else _typed(self.declaration.get(found.key), text)
        )
        return built


def source_metadata_names(source: Any) -> list[str]:
    """The names of what the canonical Source exposes that a column can carry.

    Read through the Source's own ``governed_context``: each name holding a
    single value, in the Source's order. Empty with no Source or none exposed.
    """
    facts = source.governed_context() if source is not None else None
    return [
        str(name) for name, held in (facts or {}).items()
        if not isinstance(held, (list, tuple, dict))
    ]


def shown_source_name(stored: Any) -> Any:
    """A stored ``source.<name>`` as a surface shows it: the bare name."""
    text = "" if stored is None else str(stored)
    return text[len(SOURCE_PREFIX):] if text.startswith(SOURCE_PREFIX) else stored


def stored_source_name(name: str) -> str:
    """A chosen Source name as a rule stores it: ``source.<name>``."""
    return f"{SOURCE_PREFIX}{name}"


def _offered(setting: ChoiceSetting, source: Any) -> list[str]:
    """Exactly what a ``select`` offers, asked now; nothing for any other control."""
    if setting.control != "select" or setting.choices is None:
        return []
    return list(setting.choices(source))


def _shown(setting: ChoiceSetting, stored: Any) -> Any:
    """A stored value as the surface shows it: structured ones as JSON text."""
    if setting.shown is not None:
        return setting.shown(stored)
    if isinstance(stored, (dict, list)):
        return json.dumps(stored)
    return "" if stored is None else stored


def _typed(starter: Any, text: str) -> Any:
    """Authored text as the declaration holds that key: typed as its starter is.

    Raises:
        ConfigError: If a structured or whole-number value cannot be read.
    """
    if isinstance(starter, (dict, list)):
        try:
            parsed = json.loads(text or ("{}" if isinstance(starter, dict) else "[]"))
        except ValueError as exc:
            raise ConfigError(f"Transform: '{text}' is not readable as JSON.") from exc
        if not isinstance(parsed, type(starter)):
            shape = "object" if isinstance(starter, dict) else "list"
            raise ConfigError(f"Transform: '{text}' must be a JSON {shape}.")
        return parsed
    if isinstance(starter, int) and not isinstance(starter, bool):
        try:
            return int(text)
        except ValueError as exc:
            raise ConfigError(f"Transform: '{text}' is not a whole number.") from exc
    return text

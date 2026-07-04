"""
Attribute-access Namespace wrapper for config/context data.

Recursive dict wrapper used throughout rey_lib config/context construction.
Split out of ``config_utils`` (SGC_Rey_Lib_Config_Utils_Responsibility_Split);
behaviour is unchanged.
"""

from __future__ import annotations

from typing import Any

class Namespace:
    """
    Recursive attribute-access wrapper around a plain dict.

    Supports both attribute access (ctx.key) and item access (ctx["key"]).
    Nested dicts become child Namespace objects. Lists are preserved with
    any dict items inside them also wrapped as Namespace objects.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            object.__setattr__(self, key, _wrap_config_value(value))

    def __getitem__(self, key: str) -> Any:
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        try:
            object.__getattribute__(self, key)
            return True
        except AttributeError:
            return False

    def keys(self) -> list[str]:
        return [k for k in self.__dict__ if not k.startswith("_")]

    def values(self) -> list[Any]:
        return [object.__getattribute__(self, k) for k in self.keys()]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, object.__getattribute__(self, k)) for k in self.keys()]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            return default

    def __repr__(self) -> str:
        pairs = ", ".join(f"{k}={repr(v)}" for k, v in self.items())
        return f"Namespace({pairs})"


# ---------------------------------------------------------------------------
# PathResolver
# ---------------------------------------------------------------------------

def _wrap_config_value(value: Any) -> Any:
    """Wrap a config value for storage in a Namespace."""
    if isinstance(value, dict):
        return Namespace(value)
    if isinstance(value, list):
        return [_wrap_config_value(item) for item in value]
    return value

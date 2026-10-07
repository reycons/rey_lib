"""Stand a test double where DatabaseTable resolves its primitive (backlog 751).

A destination is written through DataLoader, which holds no adapter: every
write is a DatabaseTable behaviour, and DatabaseTable resolves the primitive
beneath it. A test that substitutes the database therefore substitutes it
there, once, bound to one double for the whole test.
"""

from __future__ import annotations

from typing import Any

import pytest

from rey_lib.db import database_objects


def stand_beneath_the_table(monkeypatch: pytest.MonkeyPatch, adapter: Any) -> Any:
    """Make ``adapter`` the primitive every DatabaseTable operation reaches.

    Args:
        monkeypatch: The test's monkeypatch, which undoes this afterwards.
        adapter: The double, bound once so every operation reaches the same one.

    Returns:
        The double, for a test that goes on to inspect it.
    """
    monkeypatch.setattr(database_objects, "_adapter", lambda: adapter)
    return adapter

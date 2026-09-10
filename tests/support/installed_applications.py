"""Standing in for what the environment has installed.

A Python application exists only through its installed distribution, so a test
that builds a context declaring one has to say which applications that synthetic
installation has. Otherwise the declaration is refused -- correctly, because a
declaration cannot invent an application.

This is the seam bootstrap uses: discovery answers, and what it answers is the
environment's business. A test says what its environment holds.
"""

from __future__ import annotations

from typing import Any

import pytest

__all__ = ["installed"]


def installed(monkeypatch: pytest.MonkeyPatch, *names: str, **published: Any) -> None:
    """Declare which applications this test's environment has installed.

    Args:
        monkeypatch: The patching fixture.
        names: Application names the environment registers.
        published: What one named application publishes, where a test needs it
            to publish anything beyond identity.
    """
    registrations = {name: {"name": name} for name in names}
    for name, registration in published.items():
        registrations[name] = {"name": name, **registration}
    monkeypatch.setattr(
        "rey_lib.config.applications.discover_registrations",
        lambda: dict(registrations),
    )

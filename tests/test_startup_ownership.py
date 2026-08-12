"""No application starts its own logging.

The shared bootstrap owns context acquisition, logging initialization and the
process error boundary. An application that calls setup_logging itself has taken
one of those back, and it does so invisibly: the process still starts, still
logs, and nothing fails until two apps disagree about where records go.

This walks the sibling application repositories, so it only runs where they are
checked out together. That is the layout the migration was performed against.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# rey_lib/tests/ -> rey_lib/ -> apps/
APPS_ROOT = Path(__file__).resolve().parents[2]

# Frozen. Its six call sites re-point logging per operation within one process —
# `trade ingest` runs transform then load in sequence and writes a run log for
# each — so migrating it would silently collapse two logs into one. Named here
# rather than skipped by pattern, so the day it thaws this test says so.
FROZEN = {"trade_analyzer"}

_CALL = re.compile(r"^\s*[^#\n]*\bsetup_logging\s*\(", re.MULTILINE)


def _application_sources() -> list[Path]:
    """Every production Python file in the sibling applications."""
    if not (APPS_ROOT / "rey_lib").is_dir():
        pytest.skip("sibling application repositories are not checked out here")
    found: list[Path] = []
    for app in sorted(p for p in APPS_ROOT.iterdir() if p.is_dir()):
        if app.name in FROZEN or app.name == "rey_lib":
            continue
        for path in app.rglob("*.py"):
            parts = set(path.parts)
            if parts & {".claude", "node_modules", "tests", ".venv", "build"}:
                continue
            found.append(path)
    return found


def test_no_application_initializes_logging_itself() -> None:
    """Logging is the bootstrap's to start, once, from the resolved context."""
    offenders = [
        str(path.relative_to(APPS_ROOT))
        for path in _application_sources()
        if _CALL.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], (
        "these call setup_logging directly instead of starting through "
        f"build_ctx_for_app: {offenders}"
    )


def test_the_frozen_exception_is_still_real() -> None:
    """An exception that stops being true is worse than no exception.

    If trade_analyzer no longer calls setup_logging, it has either been migrated
    or removed, and either way this entry should go rather than sit here
    implying a debt that no longer exists.
    """
    frozen_app = APPS_ROOT / "trade_analyzer"
    if not frozen_app.is_dir():
        pytest.skip("trade_analyzer is not checked out here")
    calls = [
        path for path in frozen_app.rglob("*.py")
        if not {".claude", "node_modules", "tests", ".venv"} & set(path.parts)
        and _CALL.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert calls, (
        "trade_analyzer no longer starts its own logging — remove it from FROZEN"
    )


def test_the_bootstrap_is_the_only_place_logging_starts() -> None:
    """One initializer, in the module that owns startup."""
    bootstrap = (APPS_ROOT / "rey_lib/rey_lib/config/bootstrap.py").read_text(encoding="utf-8")
    assert bootstrap.count("setup_logging(") == 1
    assert bootstrap.count("install_process_error_boundary(") == 1

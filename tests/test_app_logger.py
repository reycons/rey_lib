"""AppLogger is the application logging object (backlog logging.app_logger.s1_object).

It wraps the stdlib logger of its name and exposes only debug, info, warning and
isEnabledFor. Each record reaches the stdlib pipeline with its level, logger
name, formatted arguments and extra intact, attributed to the caller's line.
Nothing on it emits an ERROR record or configures the pipeline.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from rey_lib.errors.error_utils import ConfigError
from rey_lib.logs import AppLogger

_NAME = "rey_lib.tests.app_logger"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == _NAME]


class TestPublicSurface:
    @pytest.mark.parametrize(
        ("method", "level"),
        [("debug", logging.DEBUG), ("info", logging.INFO), ("warning", logging.WARNING)],
    )
    def test_a_record_reaches_the_stdlib_pipeline_intact(
        self, caplog: pytest.LogCaptureFixture, method: str, level: int,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=_NAME)

        getattr(AppLogger(_NAME), method)("loaded %s rows from %s", 3, "feed", extra={"step": "load"})

        (record,) = _records(caplog)
        assert record.levelno == level
        assert record.name == _NAME
        assert record.getMessage() == "loaded 3 rows from feed"
        assert record.step == "load"

    def test_a_record_is_attributed_to_the_caller(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger=_NAME)

        line = inspect.currentframe().f_lineno + 1
        AppLogger(_NAME).info("attributed")

        (record,) = _records(caplog)
        assert record.funcName == "test_a_record_is_attributed_to_the_caller"
        assert record.lineno == line
        assert record.pathname == __file__

    def test_is_enabled_for_answers_from_the_wrapped_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=_NAME)

        app_logger = AppLogger(_NAME)

        assert app_logger.isEnabledFor(logging.WARNING)
        assert not app_logger.isEnabledFor(logging.INFO)

    @pytest.mark.parametrize("name", ["error", "exception", "critical", "setLevel", "log"])
    def test_it_offers_no_error_or_configuration_method(self, name: str) -> None:
        assert not hasattr(AppLogger(_NAME), name)


class TestErrorPath:
    """S2: AppError.report emits through the private AppLogger._error (rule 79)."""

    def test_the_error_path_is_private(self) -> None:
        assert callable(getattr(AppLogger, "_error"))
        assert "_error" not in [name for name in dir(AppLogger) if not name.startswith("_")]

    def test_app_error_report_emits_one_error_record_through_app_logger(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        error = ConfigError("the secret detail")

        error.report("loading the feed", source=_NAME)

        (record,) = [r for r in _records(caplog) if r.levelno >= logging.ERROR]
        assert record.levelno == logging.ERROR
        assert record.getMessage() == "loading the feed"
        assert record.exc_info is not None and record.exc_info[1] is error
        # Attributed to AppError.report, as before the move: _error raises
        # stacklevel past its own frame.
        assert record.funcName == "report"
        assert record.pathname.endswith("rey_lib/errors/error_utils.py")

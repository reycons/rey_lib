"""AppError.report is the one way a failure becomes an ERROR record (backlog 703).

Rule 79: AppError owns ERROR logging. ``report(context, source=...)`` logs the
error once at ERROR, the message being the operational context alone and the
error itself travelling as ``exc_info`` for the handler to sanitize.
``app_runtime`` is the application error boundary: an AppError escaping the
application is reported there and re-raised; any other Exception is normalised
into an AppError with its cause kept, reported, and raised in its place.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from rey_lib.config import bootstrap
from rey_lib.errors.error_utils import AppError, ConfigError


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


class TestReport:
    def test_it_logs_the_error_once_with_its_context_and_provenance(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        error = ConfigError("the secret detail")

        error.report("loading the feed", source="file_operator.feeds")

        (record,) = _errors(caplog)
        assert record.levelno == logging.ERROR
        assert record.getMessage() == "loading the feed"
        assert record.name == "file_operator.feeds"
        assert record.exc_info is not None and record.exc_info[1] is error

    def test_the_error_text_is_not_in_the_message(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        AppError("the secret detail").report("loading the feed", source=__name__)

        (record,) = _errors(caplog)
        assert "the secret detail" not in record.getMessage()

    def test_a_handled_error_that_was_never_raised_can_be_reported(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        AppError("handled").report("skipping one file", source=__name__)

        (record,) = _errors(caplog)
        assert record.exc_info is not None and record.exc_info[0] is AppError


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(run_id="00000000-0000-4000-8000-000000000001",
                           run_timestamp="20260822_000000")


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(bootstrap, "build_ctx_for_app", lambda *_a, **_k: _ctx())
    # What the real build_ctx_for_app leaves for the run log: the logging
    # writer its configuration assembled (a database output with no Control).
    from rey_lib.logs.app_logger import AppLogger
    from rey_lib.logs.log_record_builder import LogRecordBuilder
    from rey_lib.logs.log_writer import DatabaseLogWriter, LogWriter

    monkeypatch.setattr(AppLogger, "_writer",
                        LogWriter("db", database=DatabaseLogWriter(control=None)))
    monkeypatch.setattr(AppLogger, "_builder",
                        LogRecordBuilder(run_id=1, run_timestamp="20260822_000000"))
    return bootstrap.app_runtime


class TestTheApplicationErrorBoundary:
    def test_an_escaping_app_error_is_reported_once_and_re_raised(
        self, runtime: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        error = ConfigError("bad config")

        with pytest.raises(ConfigError) as raised:
            with runtime("cfg", "app", "run"):
                raise error

        assert raised.value is error
        (record,) = [r for r in _errors(caplog) if r.exc_info and r.exc_info[1] is error]
        assert record.name == "rey_lib.config.bootstrap"
        assert "bad config" not in record.getMessage()

    def test_a_non_app_error_is_normalised_with_its_cause_kept(
        self, runtime: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with pytest.raises(AppError, match="unexpected KeyError") as raised:
            with runtime("cfg", "app", "run"):
                raise KeyError("k")

        assert type(raised.value) is AppError
        assert isinstance(raised.value.__cause__, KeyError)
        assert raised.value.__cause__.__traceback__ is not None
        reported = [r for r in _errors(caplog) if r.exc_info and r.exc_info[1] is raised.value]
        assert len(reported) == 1

    @pytest.mark.parametrize("exit_", [SystemExit(2), KeyboardInterrupt()])
    def test_a_non_exception_exit_is_not_an_error_on_this_path(
        self, runtime: Any, caplog: pytest.LogCaptureFixture, exit_: BaseException,
    ) -> None:
        with pytest.raises(type(exit_)):
            with runtime("cfg", "app", "run"):
                raise exit_

        assert not [r for r in _errors(caplog) if r.exc_info and r.exc_info[1] is exit_]

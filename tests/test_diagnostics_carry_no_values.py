"""Diagnostic records describe a failure without the data that caused it (#1357).

Every producer named here used to put a live value into an ERROR or WARNING
message, a stored exception text or a stored traceback. Each test drives the
producer with a recognisable value and asserts it reaches neither the
exception's stored evidence (``build_safe_error_payload``, what the run log
persists) nor any log record, while the non-sensitive diagnostics remain.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from rey_lib.data import column_transform as ct
from rey_lib.data.errors import TransformError
from rey_lib.db import duckdb_utils, postgres_utils
from rey_lib.errors.error_utils import AppError, DatabaseError, build_safe_error_payload
from rey_lib.files import file_utils, onboarding
from rey_lib.logs import app_logger
from rey_lib.files.parse_utils import parse_float, parse_iso_date
from rey_lib.redaction.redactors import redact_fixed_width
from rey_lib.redaction.registry import RedactionRegistry

#: A value no message, format or column name could contain by accident.
SECRET = "Q7SECRETX9"


def _stored(exc: BaseException) -> str:
    """Return everything the run log stores for an exception.

    Args:
        exc: The raised exception, with its traceback.

    Returns:
        The stored payload rendered as text.
    """
    return repr(build_safe_error_payload(exc))


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    """Return the formatted messages and raw arguments of every captured record.

    Args:
        caplog: The capture fixture.

    Returns:
        All captured log text.
    """
    return " ".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)


@pytest.mark.parametrize(
    ("transform", "fmt"),
    [(ct._transform_date, "yyyy-MM-dd"), (ct._transform_datetime, "yyyy-MM-dd HH:mm:ss"),
     (ct._transform_time, "HH:mm:ss")],
)
def test_an_unparseable_temporal_value_is_not_stored(transform: object, fmt: str) -> None:
    """The refusal keeps the format and drops the value.

    Args:
        transform: The temporal transform under test.
        fmt: The configured input format.
    """
    with pytest.raises(TransformError) as raised:
        transform(SECRET, {"format": fmt})

    assert SECRET not in _stored(raised.value)


def test_an_unparseable_extracted_date_is_not_stored() -> None:
    """The regex date refusal keeps the format and drops the extracted value."""
    with pytest.raises(TransformError) as raised:
        ct._transform_regex_date({"A": SECRET}, {"source": "A", "pattern": "(Q7.*)"})

    assert SECRET not in _stored(raised.value)


def test_a_pattern_without_its_group_does_not_store_the_value() -> None:
    """The missing-group refusal keeps the pattern and the group, not the value."""
    with pytest.raises(TransformError) as raised:
        ct._transform_regex_date({"A": SECRET}, {"source": "A", "pattern": "Q7", "group": 3})

    stored = _stored(raised.value)
    assert SECRET not in stored and "group 3" in stored


def test_value_level_debug_lines_carry_no_value(caplog: pytest.LogCaptureFixture) -> None:
    """Numeric, regex-extract and prefix-map fall-throughs log no value."""
    with caplog.at_level(logging.DEBUG):
        ct._transform_numeric(SECRET, {})
        ct._transform_regex_extract({}, {"pattern": "NOMATCH"}, SECRET)
        ct._transform_regex_extract({}, {"pattern": "(Q7.*)", "cast_to": "float"}, SECRET)
        ct._transform_regex_extract({}, {"pattern": "(Q7.*)", "cast_to": "int"}, SECRET)
        ct._transform_prefix_map("action", {}, {"A": SECRET}, {"source": "A", "prefixes": {}})

    assert caplog.records
    assert SECRET not in _logged(caplog)


@pytest.mark.parametrize("parse", [parse_iso_date, parse_float])
def test_the_row_parsers_do_not_quote_the_value(parse: object) -> None:
    """A row value that does not parse is refused without its text or a chained cause.

    Args:
        parse: The parse helper under test.
    """
    with pytest.raises(ValueError) as raised:
        parse(SECRET)

    assert SECRET not in _stored(raised.value)


def test_a_nonnumeric_control_total_is_not_stored(tmp_path: Path) -> None:
    """The control-total refusal names the column and the file, never the value."""
    source = tmp_path / "feed.csv"
    source.write_text(f"Name,Principal Cash\nrow,{SECRET}\n", encoding="utf-8")

    with pytest.raises(onboarding.InputError) as raised:
        onboarding._sum_source_column(source, "Principal Cash", ",", "utf-8")

    stored = _stored(raised.value)
    assert SECRET not in stored and "Principal Cash" in stored and "feed.csv" in stored


def test_an_unparseable_date_range_value_is_not_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skipping an unparseable movement date logs the column and file only."""
    source = tmp_path / "feed.csv"
    source.write_text(f"Trade Date\n{SECRET}\n2024-01-02\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        file_utils._read_date_range_from_column(source, "Trade Date", "%Y-%m-%d")

    assert SECRET not in _logged(caplog) and "Trade Date" in _logged(caplog)


def test_a_truncated_fixed_width_replacement_logs_neither_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The truncation warning keeps the column and the widths, not the values."""
    registry = RedactionRegistry(["EMAIL"], mask_types={"EMAIL": "email"})
    line = f"{SECRET}  "

    with caplog.at_level(logging.DEBUG):
        redacted = redact_fixed_width([line], [(1, len(SECRET))], registry, ["EMAIL"])

    replacement = registry.redact("EMAIL", SECRET)
    logged = _logged(caplog)
    assert len(replacement) > len(SECRET)
    assert SECRET not in redacted[0]
    assert SECRET not in logged and replacement not in logged and "EMAIL" in logged


def _logged_with_exceptions(caplog: pytest.LogCaptureFixture) -> str:
    """Return captured messages, arguments and any exception text the records carry.

    Args:
        caplog: The capture fixture.

    Returns:
        All captured log text, exceptions included.
    """
    formatter = logging.Formatter()
    return " ".join(
        f"{r.getMessage()} {r.args!r} "
        f"{formatter.formatException(r.exc_info) if r.exc_info else ''}"
        for r in caplog.records
    )


def test_a_driver_failure_is_stored_as_its_category_not_its_message() -> None:
    """A real DuckDB conversion failure quotes the value; the stored DatabaseError does not."""
    conn = duckdb.connect()
    statement = f"SELECT CAST('{SECRET}' AS INTEGER)"
    with pytest.raises(duckdb.Error) as native:
        conn.execute(statement)
    assert SECRET in str(native.value)

    with pytest.raises(DatabaseError) as raised:
        duckdb_utils.execute_statement(conn, statement)

    stored = _stored(raised.value)
    assert SECRET not in stored and "ConversionException" in stored
    assert raised.value.__cause__ is None


def test_a_postgres_failure_keeps_its_sqlstate_and_named_objects() -> None:
    """The SQLSTATE and the objects the driver names remain; message and detail do not."""
    diag = SimpleNamespace(schema_name="control", table_name="feed", column_name="amount",
                           datatype_name=None, constraint_name="feed_amount_key")
    original = type("UniqueViolation", (Exception,), {})(
        f"duplicate key value violates unique constraint\nDETAIL: Key (amount)=({SECRET})")
    original.sqlstate = "23505"
    original.diag = diag
    wrapped = type("IntegrityError", (Exception,), {})("wrapped")
    wrapped.orig = original

    reason = postgres_utils._reason(wrapped)

    assert SECRET not in reason
    assert reason == ("UniqueViolation; SQLSTATE 23505; schema control; table feed; "
                      "column amount; constraint feed_amount_key")


def test_rey_libs_own_error_passes_through_the_reason_unchanged() -> None:
    """A governed rey_lib message is not reduced to its class."""
    governed = DatabaseError("postgres_utils: execution failed: UniqueViolation")

    assert postgres_utils._reason(governed) == str(governed)
    assert duckdb_utils._reason(governed) == str(governed)


def test_a_filename_group_value_is_not_stored() -> None:
    """A value taken from a filename is data: the refusal does not name it."""
    unsafe = "§¶§"

    with pytest.raises(onboarding.InputError) as raised:
        onboarding._slug(unsafe)

    assert unsafe not in _stored(raised.value)


def test_ungoverned_stderr_is_reported_without_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fact of an ungoverned write is recorded; the third-party line is not."""
    stderr = app_logger._UngovernedStderr(sys.__stderr__)

    with caplog.at_level(logging.DEBUG):
        stderr.write(f"third-party warning about {SECRET}\n")

    logged = _logged_with_exceptions(caplog)
    assert "ungoverned stderr write" in logged
    assert SECRET not in logged
    assert all(r.exc_info is None or isinstance(r.exc_info[1], AppError)
               for r in caplog.records)

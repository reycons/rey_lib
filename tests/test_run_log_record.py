"""A persisted run-log record owns its payload (#1173).

Control retrieves the row; RunLogRecord says what it means. Its payload is read
where the writer put it -- the column ``payload_column`` names for its type --
so a reader never holds the record type to column mapping itself.
"""

from __future__ import annotations

import pytest

from rey_lib.logs import RunLogRecord
from rey_lib.logs.run_log import FAILURE_RECORD_TYPES, TYPE_PAYLOAD_COLUMNS

#: The error object a failure record carries, as the logger writes it.
ERROR_OBJECT = {"message": "Run not found: 7", "error_type": "ConsoleError"}


@pytest.mark.parametrize("record_type", sorted(TYPE_PAYLOAD_COLUMNS))
def test_a_typed_record_answers_with_the_column_its_writer_used(record_type: str) -> None:
    column = TYPE_PAYLOAD_COLUMNS[record_type]
    record = RunLogRecord({"record_type": record_type, column: {"held": record_type}})

    assert record.carries_payload
    assert record.payload() == {"held": record_type}


@pytest.mark.parametrize("record_type", sorted(FAILURE_RECORD_TYPES))
def test_a_failure_record_answers_with_its_error_object(record_type: str) -> None:
    record = RunLogRecord({"record_type": record_type, "error_message": ERROR_OBJECT})

    assert record.payload() == ERROR_OBJECT


def test_the_type_is_read_in_any_case() -> None:
    record = RunLogRecord({"record_type": "row_count", "row_count": {"count": 3}})

    assert record.record_type == "ROW_COUNT"
    assert record.payload() == {"count": 3}


def test_only_its_own_column_is_its_payload() -> None:
    # Another type's column, filled, is not this record's payload.
    record = RunLogRecord({"record_type": "ROW_COUNT", "info": {"stray": True}})

    assert record.carries_payload
    assert record.payload() is None


def test_a_type_with_no_payload_says_so() -> None:
    record = RunLogRecord({"record_type": "MYSTERY", "info": {"stray": True}})

    assert not record.carries_payload
    assert record.payload() is None


def test_a_typeless_row_carries_nothing() -> None:
    record = RunLogRecord({})

    assert record.record_type == ""
    assert not record.carries_payload
    assert record.payload() is None


def test_the_row_is_held_as_answered_not_shared() -> None:
    row = {"record_type": "INFO", "info": {"message": "before"}}
    record = RunLogRecord(row)
    row["info"] = {"message": "after"}

    assert record.payload() == {"message": "before"}

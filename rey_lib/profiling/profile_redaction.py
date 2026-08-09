"""Profile-only redaction for explicitly governed value-bearing fields."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from copy import deepcopy
from typing import Any, Mapping

__all__ = ["redact_profile"]

_TOKEN_LIST_FIELDS = ("distinct_sample", "sample_values", "null_like_values")
_TOKEN_SCALAR_FIELDS = ("constant_value",)
_EXACT_RANGE_FIELDS = ("min_numeric", "max_numeric", "min_date", "max_date")


def redact_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted copy without traversing arbitrary profile strings.

    Only explicitly listed value-bearing fields on column objects are touched.
    A random secret exists only for this call and is never persisted, while the
    column identity is part of each token input. Equal values therefore remain
    equal within one column and cannot correlate across columns.
    """
    redacted = deepcopy(dict(profile))
    secret = secrets.token_bytes(32)
    columns = redacted.get("columns")
    if not isinstance(columns, list):
        return redacted

    for position, column in enumerate(columns, start=1):
        if not isinstance(column, dict):
            continue
        identity = _column_identity(column, position)
        for field in _TOKEN_LIST_FIELDS:
            values = column.get(field)
            if isinstance(values, list):
                column[field] = [
                    _token(secret, identity, value) for value in values
                ]
        for field in _TOKEN_SCALAR_FIELDS:
            if field in column:
                column[field] = _token(secret, identity, column[field])
        # Exact ranges disclose source values. Existing non-value-bearing
        # precision, scale, sign, pattern, and date-format facts remain.
        for field in _EXACT_RANGE_FIELDS:
            column.pop(field, None)
        column["redacted"] = True

    redacted["redacted_columns"] = [
        str(column.get("name", ""))
        for column in columns
        if isinstance(column, dict)
    ]
    return redacted


def _column_identity(column: Mapping[str, Any], position: int) -> str:
    ordinal = column.get("ordinal", position)
    name = column.get("raw_name", column.get("name", ""))
    return f"{ordinal}:{name}"


def _token(secret: bytes, column_identity: str, value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    digest = hmac.new(
        secret,
        f"{column_identity}\0{canonical}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12].upper()
    return f"TXT_{digest}"

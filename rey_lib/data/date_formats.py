"""Date format tokens, as configuration writes them.

Configuration states a date format the way Excel and Java do -- ``MM/dd/yyyy``
-- and Python parses ``%m/%d/%Y``. One converter, here rather than beside
either caller, because BOTH ends need it: a transform rule parsing a value,
and the file boundary parsing a date out of a file NAME.

It has no home in either of those. Put with the transform rules it would make
the file boundary import the transform object; put with the file boundary it
would make the transform object import files. Both are cycles, and neither
direction is what a format token is about.

Nothing here imports anything of the estate's, which is what keeps it usable
from both sides.
"""

from __future__ import annotations

import re

__all__ = ["to_strptime_format"]


_DATE_TOKEN_RE: re.Pattern[str] = re.compile(
    r"yyyy|yy|MMMM|MMM|MM|M|dd|d|HH|hh|H|h|mm|ss|SSS|SS|S|a|Z"
)

_DATE_TOKEN_MAP: dict[str, str] = {
    # Year
    "yyyy": "%Y",   # 2026
    "yy":   "%y",   # 26
    # Month — uppercase
    "MMMM": "%B",   # January
    "MMM":  "%b",   # Jan
    "MM":   "%m",   # 05
    "M":    "%m",   # 5
    # Day
    "dd":   "%d",   # 14
    "d":    "%d",   # 4
    # Hour
    "HH":   "%H",   # 19  (24-hour)
    "hh":   "%I",   # 07  (12-hour)
    "H":    "%H",
    "h":    "%I",
    # Minute — lowercase
    "mm":   "%M",   # 20
    # Second
    "ss":   "%S",   # 52
    # Fractional seconds
    "SSS":  "%f",   # microseconds
    "SS":   "%f",
    "S":    "%f",
    # AM/PM
    "a":    "%p",   # AM / PM
    # Timezone offset
    "Z":    "%z",   # +0000 / -0500
}

def to_strptime_format(fmt: str) -> str:
    """Convert an Excel/Java-style date format string to a Python strptime format.

    Accepts both double-token (``MM``, ``dd``) and single-token (``M``, ``d``)
    variants. If ``fmt`` already contains ``%`` it is returned unchanged so
    existing strptime strings in config continue to work.

    Follows Java/Excel standard: ``MM`` = months (uppercase), ``mm`` = minutes
    (lowercase). Existing strptime strings (containing ``%``) pass through unchanged.

    Examples
    --------
    ``MM/dd/yyyy``           → ``%m/%d/%Y``
    ``M/d/yy``               → ``%m/%d/%y``
    ``yyyyMMdd``             → ``%Y%m%d``
    ``yyyy-MM-dd``           → ``%Y-%m-%d``
    ``dd-MMM-yyyy``          → ``%d-%b-%Y``
    ``yyyy-MM-ddTHH:mm:ss``  → ``%Y-%m-%dT%H:%M:%S``
    """
    if "%" in fmt:
        return fmt
    return _DATE_TOKEN_RE.sub(lambda tok: _DATE_TOKEN_MAP[tok.group()], fmt)

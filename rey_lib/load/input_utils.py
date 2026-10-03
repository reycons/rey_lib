"""Input helpers the Loader's profile path uses (row 589, step 5).

Copied from the legacy file_operator ``input_utils`` module, which stays
untouched as the reference. Only what the profile path calls is copied:
``parse_column_list``. Layout resolution and input enumeration join this module
when a step's production path reaches them.
"""

from __future__ import annotations

__all__: list[str] = [
    "parse_column_list",
]


def parse_column_list(raw: str | None) -> list[str]:
    """Parse a comma-separated column name string into a list of stripped names.

    Parameters
    ----------
    raw : str | None
        Raw CLI value, e.g. ``'ACCOUNT NUMBER,MASTER ACCOUNT'``.

    Returns
    -------
    list[str]
        Stripped, non-empty column names.  Empty list when ``raw`` is
        ``None`` or blank.
    """
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]

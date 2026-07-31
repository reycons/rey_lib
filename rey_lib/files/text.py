"""Application-neutral text normalization shared by every file format.

One authoritative implementation, so a value that arrives from a spreadsheet
cell, a delimited field, or a fixed-width column is cleaned identically.

Scope is deliberately narrow: this operates on an already-decoded, already-
separated value. It performs no I/O, no parsing, no delimiter detection, and
no format interpretation, and it is never applied to raw file text — control
characters carry a file's structure before it is parsed, and removing them
from whole text would destroy the line and field boundaries the parser
depends on.
"""

from __future__ import annotations

import unicodedata

__all__ = ["clean_text_value"]

# Cc is the Unicode control category — NUL, TAB, CR, LF and their kin. Cf is
# the format category — the byte-order mark, zero-width joiners, directional
# marks. Inside a value both are invisible noise a source should not carry.
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf"})


def clean_text_value(text: str) -> str:
    """Return ``text`` with Unicode Cc and Cf characters removed.

    Applied to one value at a time — a spreadsheet cell, a parsed delimited
    field, a fixed-width column — after the format has already established
    where that value begins and ends.

    Parameters
    ----------
    text : str
        One decoded value.

    Returns
    -------
    str
        The value without control or format characters. Every other
        character, including ordinary and non-breaking spaces, is preserved
        exactly: collapsing or trimming whitespace is a semantic decision
        belonging to whichever format wants it, not to this function.
    """
    return "".join(
        character
        for character in text
        if unicodedata.category(character) not in _REMOVED_CATEGORIES
    )

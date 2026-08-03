"""Application-neutral text normalization shared by every file format.

One authoritative implementation for application-owned value cleaning.

Scope is deliberately narrow: this operates on an already-decoded value. It
performs no I/O, parsing, delimiter detection, or format interpretation. The
governed whole-file sanitizer uses its configured policy engine instead.
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

    Applied to one application-owned decoded value at a time.

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

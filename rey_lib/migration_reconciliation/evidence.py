"""
Resolve an evidence reference, and nothing more.

What this proves is that the reference points at something real: a file that
exists, and a named test or symbol inside it. What it does not prove -- and must
never be described as proving -- is that the named test exercises the capability
it is cited for. A test named for a behaviour it does not touch resolves
happily. That residue is recorded in the enforcement contract as the weakest
joint in the layer, and it is judgement, not a lookup.

Reference form:

    <path fragment> > <test or symbol name>

The fragment is matched against the tree rather than resolved as a path, so a
record does not carry a build-relative prefix it would have to keep in step with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvidenceResult:
    """Whether one reference resolved, and why not when it did not."""

    reference: str
    resolved: bool
    reason: str = ""
    located_in: str = ""


class EvidenceIndex:
    """The files a reference may resolve against, read once.

    Args:
        roots: Directories to search. Every readable text file beneath them is
            indexed; nothing is executed.
        suffixes: Which files may hold evidence.
    """

    def __init__(
        self,
        roots: tuple[Path, ...],
        suffixes: tuple[str, ...] = (".ts", ".tsx", ".py"),
    ) -> None:
        self._files: dict[str, str] = {}
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix not in suffixes:
                    continue
                if "node_modules" in path.parts or "__pycache__" in path.parts:
                    continue
                try:
                    self._files[path.as_posix()] = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

    def resolve(self, reference: str) -> EvidenceResult:
        """Return whether this reference points at something that exists.

        Args:
            reference: ``<path fragment> > <name>``, or a bare name.

        Returns:
            The result, carrying the file it was found in when it resolved.
        """
        fragment, _, name = (part.strip() for part in reference.partition(">"))
        if not name:
            fragment, name = "", fragment
        if not name:
            return EvidenceResult(reference, False, "names nothing to look for")

        candidates = [
            path for path in self._files
            if not fragment or fragment in path
        ]
        if not candidates:
            return EvidenceResult(reference, False, f"no file matches '{fragment}'")

        wanted = re.compile(
            r"(?:it|test|describe)\s*\(\s*[\"'`]" + re.escape(name)
            + r"|\b(?:function|const|class|def)\s+" + re.escape(name) + r"\b",
        )
        for path in candidates:
            if wanted.search(self._files[path]):
                return EvidenceResult(reference, True, located_in=path)
        return EvidenceResult(
            reference, False,
            f"'{name}' is not a test or symbol in any file matching '{fragment}'",
        )

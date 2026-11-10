"""A configured file selection executed through canonical load objects.

ConfiguredLoad owns discovery, caps and iteration. It holds the canonical
Transform and Target and creates a canonical Source for each selected file.
Its injected execution calls run_selected_load; only those objects resolve
DataFile, DataTransform and destination mechanics. No connection or resolved
loader is retained by this definition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from rey_lib.files.file_utils import input_files
from rey_lib.logs import AppLogger

if TYPE_CHECKING:
    from rey_lib.load.source import Source
    from rey_lib.load.transform import Transform
    from rey_lib.load.target import Target

__all__ = ["ConfiguredLoad", "LoadOneFile"]

_logger = AppLogger(__name__)

#: Executes one canonical Source / Transform / Target through run_selected_load.
#: The callback owns execution context, including file routing and run evidence.
#: This definition only selects files and passes the same canonical objects.
LoadOneFile = Callable[..., int]


class ConfiguredLoad:
    """One file selection, holding its canonical Transform and Target."""

    def __init__(
        self,
        *,
        load_one_file: LoadOneFile,
        source_dir: Path | None = None,
        pattern: str = "",
        transform: Transform,
        target: Target,
        file_type: str = "",
        encoding: str = "utf-8-sig",
        max_files: int | None = None,
        name: str = "",
        on_discovered: Callable[[Path], None] | None = None,
        explicit_files: list[Path] | None = None,
    ) -> None:
        """Hold what one load definition resolved to.

        Args:
            load_one_file: How one selected file is loaded.
            source_dir: Where this load picks files up, already resolved.
                Unused when explicit_files is given.
            pattern: The pickup pattern, already substituted. Unused when
                explicit_files is given.
            transform: What the produced records contain.
            target: The data object those records go to. The definition's,
                resolved once here -- so every file in the feed is written to
                one object rather than each re-reading where it lives.
            file_type: Declared format, or empty to infer from the suffix.
            encoding: How to decode the files.
            max_files: Cap on files taken in one run, or None for all.
            name: What to call this load in logs.
            on_discovered: Recorder for every file found, before the cap.
            explicit_files: Load exactly these instead of discovering any.
                A caller that named its file has already answered the
                question ``source_dir`` and ``pattern`` exist to answer.
        """
        self.source_dir = Path(source_dir) if source_dir else None
        self.pattern = pattern
        self.load_one_file = load_one_file
        self.transform = transform
        self.target = target
        self.file_type = file_type
        self.encoding = encoding
        self.max_files = max_files
        self.name = name
        self.on_discovered = on_discovered
        self.explicit_files = explicit_files

    def select_files(
        self,
        on_discovered: Callable[[Path], None] | None = None,
    ) -> list[Path]:
        """Return the files this load takes this run, in pickup order.

        The cap is applied HERE rather than by the caller, because "how many
        files this definition takes in one run" is part of the definition.

        Args:
            on_discovered: Called for every file FOUND, before the cap. What
                was discovered and what was taken are different facts, and the
                run log records the first -- a file left behind by
                max_files_per_run was still there.
        """
        # A caller that named its files has already answered this. Neither
        # the directory nor the pattern is consulted -- and the cap is not
        # applied either, since "how many of the files I found" means nothing
        # when the caller found them.
        if self.explicit_files is not None:
            if on_discovered is not None:
                for file_path in self.explicit_files:
                    on_discovered(file_path)
            return list(self.explicit_files)

        pending = input_files(self.source_dir, self.pattern)

        if on_discovered is not None:
            for file_path in pending:
                on_discovered(file_path)

        if self.max_files is not None:
            capped = pending[: int(self.max_files)]
            if len(capped) != len(pending):
                _logger.info(
                    "max_files_per_run=%d applied — %d file(s) eligible this run",
                    self.max_files, len(capped),
                )
            return capped

        return pending

    def source_for(self, path: Path) -> Source:
        """Return the canonical Source for one selected path.

        The format settings come from the definition, so every file in a feed
        is read the same way and nothing re-reads configuration per file.
        """
        from rey_lib.load.source import Source

        return Source({"file": str(path), "file-type": self.file_type,
                       "encoding": self.encoding}, selected="file")

    def load(self, run_log: Any) -> int:
        """Select this load's files and load each one.

        **No connection.** It only ever passed one through, and the per-file
        step now resolves its own from the target it is given. Taking one here
        would make a definition whose target is not a database demand a
        database handle to run.

        **A batch is an AGGREGATE that may partially complete.** A file that
        fails is one failed file: the ones before it stay loaded and the ones
        after it are still attempted. That is why the per-file step reports 0
        rather than raising for a file fault.

        **A RUN-LEVEL fault stops everything.** A missing destination, or any
        other configuration error, is not caught here -- every file would fail
        the same way, and 100 identical failures is one misconfiguration
        reported 100 times rather than a partial success.

        Args:
            run_log: The run's evidence recorder.

        Returns:
            Rows loaded across every file taken this run.
        """
        pending = self.select_files(self.on_discovered)
        if self.explicit_files is not None:
            # Named, not discovered. Reporting a directory and a pattern the
            # caller never gave would put a search that did not happen into
            # the evidence -- and both would read as 'None' and ''.
            _logger.info(
                "%s: %d file(s) named by the caller",
                self.name or "load", len(pending),
            )
        else:
            _logger.info(
                "%s: %d file(s) pending in %s matching '%s'",
                self.name or "load", len(pending), self.source_dir,
                self.pattern,
            )

        # Share the canonical Transform and Target. Each selected file has its
        # own canonical Source; run_selected_load resolves all three per call.
        return sum(
            self.load_one_file(
                self.source_for(file_path),
                self.transform,
                self.target,
                run_log=run_log,
            )
            for file_path in pending
        )

"""One load definition: which files it takes, and how they are loaded.

A ``DataFile`` is ONE PHYSICAL FILE. A configured feed is many files sharing
one transform and one destination policy, so the definition needs an object
too -- otherwise every caller re-derives the same things per file.

```
ConfiguredLoad        this feed / load definition   long-lived
  |- DataFile         this physical file            one per file
  |- DataTransform    what the records contain
  +- DataLoader       where the records go
```

This is where configuration is INTERPRETED and stops
-----------------------------------------------------
It replaces the whole ``ctx + data_source + load_cfg`` bundle that
``load_files`` took:

```
OLD   load_files(ctx, run_log, conn, data_source, load_cfg)
NEW   ConfiguredLoad(...).load(conn, run_log)
```

Everything a load DEFINITION needs is resolved once, at construction. What
varies per EXECUTION -- the connection and the run's log -- arrives at
``load``. Neither is held: the connection is shared and outlives any one
load, and a definition can outlive any one run.

Nothing below this object interprets configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.files.data_loader import DataLoader
from rey_lib.files.data_transform import DataTransform
from rey_lib.files.file_utils import input_files
from rey_lib.logs import get_logger

__all__ = ["ConfiguredLoad", "LoadOneFile"]

_logger = get_logger(__name__)

#: How one already-selected file is loaded.
#:
#: ``(conn, run_log, path) -> rows loaded``
#:
#: Injected rather than imported. The per-file step still owns movements, run
#: logging and the mapping of a database failure onto a file's routing --
#: application concerns that live in the caller. Reaching for them from here
#: would close an import cycle and would put this object in charge of things
#: it does not own.
#:
#: It must return 0 rather than raise for a FILE fault, so the batch
#: continues; a RUN-level fault must raise, so the batch stops.
LoadOneFile = Callable[[Any, Any, Path], int]


class ConfiguredLoad:
    """One load definition, ready to run against any connection."""

    def __init__(
        self,
        *,
        source_dir: Path,
        pattern: str,
        load_one_file: LoadOneFile,
        transform: DataTransform,
        loader: DataLoader,
        file_type: str = "",
        encoding: str = "utf-8-sig",
        max_files: int | None = None,
        name: str = "",
        on_discovered: Callable[[Path], None] | None = None,
    ) -> None:
        """Hold what one load definition resolved to.

        Args:
            source_dir: Where this load picks files up, already resolved.
            pattern: The pickup pattern, already substituted.
            load_one_file: How one selected file is loaded.
            transform: What the produced records contain.
            loader: Where those records go.
            file_type: Declared format, or empty to infer from the suffix.
            encoding: How to decode the files.
            max_files: Cap on files taken in one run, or None for all.
            name: What to call this load in logs.
            on_discovered: Recorder for every file found, before the cap.
        """
        self.source_dir = Path(source_dir)
        self.pattern = pattern
        self.load_one_file = load_one_file
        self.transform = transform
        self.loader = loader
        self.file_type = file_type
        self.encoding = encoding
        self.max_files = max_files
        self.name = name
        self.on_discovered = on_discovered

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

    def data_file_for(self, path: Path) -> DataFile:
        """Return the DataFile for one selected path.

        The format settings come from the definition, so every file in a feed
        is read the same way and nothing re-reads configuration per file.
        """
        return data_file_for(
            path, file_type=self.file_type, encoding=self.encoding
        )

    def load(self, conn: Any, run_log: Any) -> int:
        """Select this load's files and load each one.

        **A batch is an AGGREGATE that may partially complete.** A file that
        fails is one failed file: the ones before it stay loaded and the ones
        after it are still attempted. That is why the per-file step reports 0
        rather than raising for a file fault.

        **A RUN-LEVEL fault stops everything.** A missing destination, or any
        other configuration error, is not caught here -- every file would fail
        the same way, and 100 identical failures is one misconfiguration
        reported 100 times rather than a partial success.

        Args:
            conn: Open connection, owned by the caller.
            run_log: The run's evidence recorder.

        Returns:
            Rows loaded across every file taken this run.
        """
        pending = self.select_files(self.on_discovered)
        _logger.info(
            "%s: %d file(s) pending in %s matching '%s'",
            self.name or "load", len(pending), self.source_dir, self.pattern,
        )

        return sum(
            self.load_one_file(conn, run_log, file_path)
            for file_path in pending
        )

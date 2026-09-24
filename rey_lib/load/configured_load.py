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
NEW   ConfiguredLoad(...).load(run_log)
```

Everything a load DEFINITION needs is resolved once, at construction. What
varies per EXECUTION -- the run's log -- arrives at ``load``. It is not held:
a definition can outlive any one run.

The connection is neither held nor passed. The definition names its target,
the target names its configured connection, and the per-file step resolves
one where a database is first needed -- so a definition writing somewhere
that is not a database never sees one.

Nothing below this object interprets configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from rey_lib.data.data_transform import DataTransform
from rey_lib.db.data_loader import DataLoader
from rey_lib.files.data_file import DataFile, data_file_for
from rey_lib.files.file_utils import input_files
from rey_lib.logs import get_logger

__all__ = ["ConfiguredLoad", "LoadOneFile"]

_logger = get_logger(__name__)

#: How one already-selected file is loaded.
#:
#: ``(source, transform, target, *, run_log, loader, ...) -> rows``
#:
#: THE THREE DOMAIN INPUTS ARE THE CONTRACT. No connection crosses it: the
#: per-file step resolves one from ``target.connection`` where a database is
#: first needed, so a transfer whose target is not a database demands nothing
#: it has no use for.
#:
#: Injected rather than imported. The per-file step owns movements, run
#: logging and the mapping of a database failure onto a file's routing --
#: application concerns that live in the caller. Reaching for them from here
#: would close an import cycle and put this object in charge of things it
#: does not own.
#:
#: **It is HANDED the objects rather than building them.** One load
#: definition means one ``DataTransform``, one ``DataLoader`` and one rule for
#: building a ``DataFile``; a per-file step that re-read the same
#: configuration would be a second construction path, and the two could
#: disagree while looking identical.
#:
#: It must return 0 rather than raise for a FILE fault, so the batch
#: continues; a RUN-level fault must raise, so the batch stops.
LoadOneFile = Callable[..., int]


class ConfiguredLoad:
    """One load definition, ready to run against any connection."""

    def __init__(
        self,
        *,
        load_one_file: LoadOneFile,
        source_dir: Path | None = None,
        pattern: str = "",
        transform: DataTransform,
        loader: DataLoader,
        target: Any,
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
            loader: The destination mechanic and this load's policy.
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
        self.loader = loader
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

    def data_file_for(self, path: Path) -> DataFile:
        """Return the DataFile for one selected path.

        The format settings come from the definition, so every file in a feed
        is read the same way and nothing re-reads configuration per file.
        """
        return data_file_for(
            path, file_type=self.file_type, encoding=self.encoding
        )

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

        # The objects are THIS definition's, built once and handed to every
        # file. The per-file step wraps them in movements and evidence; it
        # does not reinterpret the configuration they came from.
        # The SOURCE OBJECT is built here, not the rule for building one.
        # This definition already owns how its files are read; handing the
        # per-file step a builder made it construct the endpoint it was
        # supposed to be given.
        return sum(
            self.load_one_file(
                self.data_file_for(file_path),
                self.transform,
                self.target,
                run_log=run_log,
                loader=self.loader,
            )
            for file_path in pending
        )

"""The load operation: one data object into another, through one transform.

    DataObject -> TransformObject -> DataObject

**Load is not a file concern.** It lived under ``rey_lib/files/`` because the
first thing that was ever loaded was a file, and the architecture said so in
as many words -- "shared file load / transform / validate helpers". That
placement made one endpoint family privileged: a database source could only be
added by widening a package whose name already asserted the answer.

So the families are peers, and the operation sits beside them:

    rey_lib/data/   the contracts both families answer
    rey_lib/files/  file-backed data objects, and the file pipeline
    rey_lib/db/     database-backed data objects, and the write mechanics
    rey_lib/load/   this -- coordination, and which execution path to take

The direction is one way. This package reaches into all three; none of them
reaches back. A file feed still discovers files through ``files``, and a
database destination is still written through ``db`` -- but neither of them
knows a load exists.

``source`` and ``target`` are ROLES in the operation, not types. Nothing here
asks which side a data object is on in order to know what it is.
"""

from __future__ import annotations

from rey_lib.load.configured_load import ConfiguredLoad, LoadOneFile

__all__ = ["ConfiguredLoad", "LoadOneFile"]

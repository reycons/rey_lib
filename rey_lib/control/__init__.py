"""
Rey control database access.

``Control`` is the sole runtime control API: batches, steps, log events, config
snapshots, artifacts and LLM contract runs against the optional Rey control
database. It owns the resolved ``control`` procedure map and the runtime batch
state; it does not own run identity, the connection lifecycle, logging
behaviour or launch decisions.

Usage
-----
from rey_lib.control import Control

control = Control(ctx)
control.start_batch(batch_name="my_run")
control.start_step(step_name="extract", step_sequence=1)
"""

from rey_lib.control.control import Control

__all__ = ["Control"]

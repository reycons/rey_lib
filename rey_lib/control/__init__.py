"""
Rey control database utilities.

Provides the public control API for registering batches, steps, log events,
config snapshots, artifacts, and LLM contract runs against the optional Rey
control database.

Usage
-----
from rey_lib.control import control_utils

control_utils.ensure_run_id(ctx)
control_utils.start_batch(ctx, batch_name="my_run")
"""

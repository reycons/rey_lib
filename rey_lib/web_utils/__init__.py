"""Outbound web utilities for Rey Apps.

Not ``rey_lib.web``, which is the Console's inbound HTTP server. This package
is how Rey reaches an HTTP endpoint: a named ``provider: http`` connection
declared in ``connections.yaml``, sending corehttp requests through one shared
corehttp client.
"""

from __future__ import annotations

from rey_lib.web_utils.connection import HttpConnection

__all__: list[str] = ["HttpConnection"]

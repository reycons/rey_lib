"""Outbound web utilities for Rey Apps.

Not ``rey_lib.web``, which is the Console's inbound HTTP server. This package
is how Rey reaches an HTTP endpoint: a named ``provider: http`` connection
declared in ``connections.yaml``, sending corehttp requests through one shared
corehttp client. ``HttpRequest`` and ``HttpResponse`` are corehttp's own,
re-exported unchanged so an application takes the whole HTTP API from here.
"""

from __future__ import annotations

from corehttp.rest import HttpRequest, HttpResponse

from rey_lib.web_utils.connection import HttpConnection

__all__: list[str] = ["HttpConnection", "HttpRequest", "HttpResponse"]

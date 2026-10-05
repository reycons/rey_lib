"""Provider adapters for HTTPTransform -- one module per provider (backlog 667).

Each module registers its adapter with ``@http_adapter("<name>")``.
``rey_lib.data.http_transform`` imports every module here once, on the first
adapter lookup; nothing else imports them, and this package imports nothing.
"""

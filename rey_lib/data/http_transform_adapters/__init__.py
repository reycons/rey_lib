"""Adapters for HTTPTransform, each implementing the generic HttpTransformAdapter contract.

Each module registers its adapter with ``@http_transform_adapter("<name>")``.
``rey_lib.data.http_transform`` imports every module here once, on the first
adapter lookup; nothing else imports them, and this package imports nothing.
"""

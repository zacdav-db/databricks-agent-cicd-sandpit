"""Deployment-owned Python startup hook; the author module stays unchanged."""

from _platform_tracing import configure_tracing

configure_tracing()

"""Patch provider SDKs at Python startup; the author module stays unchanged."""

from _platform_tracing import configure_autologging

configure_autologging()

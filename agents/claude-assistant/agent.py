"""Anthropic Claude example using the native SDK with Databricks authentication."""

from __future__ import annotations

import os
from functools import lru_cache

from anthropic import Anthropic
from databricks.sdk import WorkspaceClient


@lru_cache(maxsize=1)
def _workspace() -> WorkspaceClient:
    return WorkspaceClient()


def _client() -> Anthropic:
    workspace = _workspace()
    return Anthropic(
        api_key="unused",
        base_url=f"{workspace.config.host.rstrip('/')}/serving-endpoints/anthropic",
        default_headers=workspace.config.authenticate(),
    )


def invoke(message: str) -> str:
    """Answer one message with the manifest-selected Claude model."""
    response = _client().messages.create(
        model=os.environ["MODEL_ENDPOINT"],
        max_tokens=300,
        messages=[{"role": "user", "content": message}],
    )
    text = "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise RuntimeError("Claude returned no text.")
    return text

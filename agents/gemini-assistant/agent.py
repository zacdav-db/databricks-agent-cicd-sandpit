"""Google Gemini example using the native SDK with Databricks authentication."""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache

from databricks.sdk import WorkspaceClient
from google import genai
from google.genai import types


@lru_cache(maxsize=1)
def _workspace() -> WorkspaceClient:
    return WorkspaceClient()


def _client() -> genai.Client:
    workspace = _workspace()
    return genai.Client(
        api_key="databricks",
        http_options=types.HttpOptions(
            base_url=f"{workspace.config.host.rstrip('/')}/serving-endpoints/gemini",
            headers=workspace.config.authenticate(),
        ),
    )


def invoke(message: str) -> str:
    """Answer one message with the manifest-selected Gemini model."""
    client = _client()
    try:
        response = client.models.generate_content(
            model=os.environ["MODEL_ENDPOINT"],
            contents=message,
            config=types.GenerateContentConfig(max_output_tokens=300),
        )
    finally:
        client.close()
    text = response.text
    if not text:
        raise RuntimeError("Gemini returned no text.")
    return text


def invoke_stream(message: str) -> Iterator[str]:
    """Stream text from the native Gemini-compatible endpoint."""
    client = _client()
    try:
        response = client.models.generate_content_stream(
            model=os.environ["MODEL_ENDPOINT"],
            contents=message,
            config=types.GenerateContentConfig(max_output_tokens=300),
        )
        for chunk in response:
            if chunk.text:
                yield chunk.text
    finally:
        client.close()

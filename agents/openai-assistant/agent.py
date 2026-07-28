"""OpenAI example using the native SDK surface with Databricks authentication."""

from __future__ import annotations

import os
from functools import lru_cache

from databricks.sdk import WorkspaceClient
from databricks_openai import DatabricksOpenAI


@lru_cache(maxsize=1)
def _workspace() -> WorkspaceClient:
    return WorkspaceClient()


@lru_cache(maxsize=1)
def _client() -> DatabricksOpenAI:
    return DatabricksOpenAI(workspace_client=_workspace())


def invoke(message: str) -> str:
    """Answer one message with the manifest-selected OpenAI model."""
    response = _client().chat.completions.create(
        model=os.environ["MODEL_ENDPOINT"],
        max_tokens=300,
        messages=[{"role": "user", "content": message}],
    )
    text = response.choices[0].message.content
    if not text:
        raise RuntimeError("OpenAI returned no text.")
    return text

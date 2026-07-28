"""An ordinary OpenAI agent with no Databricks or MLflow imports."""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache

from openai import OpenAI


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI()


def invoke(message: str) -> str:
    """Answer one message using an externally hosted OpenAI model."""
    response = _client().chat.completions.create(
        model=os.environ["OPENAI_MODEL"],
        messages=[{"role": "user", "content": message}],
    )
    text = response.choices[0].message.content
    if not text:
        raise RuntimeError("OpenAI returned no text.")
    return text


def invoke_stream(message: str) -> Iterator[str]:
    """Stream text without importing the deployment platform."""
    response = _client().chat.completions.create(
        model=os.environ["OPENAI_MODEL"],
        messages=[{"role": "user", "content": message}],
        stream=True,
    )
    for chunk in response:
        if not chunk.choices:
            continue
        text = chunk.choices[0].delta.content
        if text:
            yield text

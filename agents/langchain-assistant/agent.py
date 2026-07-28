"""LangChain ChatDatabricks example for the folder-defined agent contract."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from databricks_langchain import ChatDatabricks


@lru_cache(maxsize=1)
def _model() -> ChatDatabricks:
    return ChatDatabricks(
        endpoint=os.environ["MODEL_ENDPOINT"],
        temperature=0.1,
        max_tokens=300,
    )


def _text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return str(content)


async def invoke(message: str) -> str:
    """Answer one message using the centrally approved model binding."""
    response = await _model().ainvoke(
        [
            {
                "role": "system",
                "content": "Be concise and helpful. State when you are uncertain.",
            },
            {"role": "user", "content": message},
        ],
    )
    return _text(response)


async def invoke_stream(message: str) -> AsyncIterator[str]:
    """Stream the same agent response without a platform-specific import."""
    async for chunk in _model().astream(
        [
            {
                "role": "system",
                "content": "Be concise and helpful. State when you are uncertain.",
            },
            {"role": "user", "content": message},
        ],
    ):
        text = _text(chunk)
        if text:
            yield text

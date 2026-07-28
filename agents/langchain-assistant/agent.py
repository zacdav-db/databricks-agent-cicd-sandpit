"""LangChain example using only the folder-defined agent contract."""

from __future__ import annotations

import os
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
    return content if isinstance(content, str) else str(content)


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

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock


class FakeChatClient:
    """Small async chat client that records every invocation."""

    def __init__(self, content: str, **message_fields: object) -> None:
        self.content = content
        self.message_fields = message_fields
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def ainvoke(self, *args: object, **kwargs: object) -> SimpleNamespace:
        self.calls.append((args, kwargs))
        return SimpleNamespace(content=self.content, **self.message_fields)


class ExplodingLLM:
    """LLM double for code paths that must short-circuit before inference."""

    async def ainvoke(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("LLM must not be called")


def fake_llm(content: str, **message_fields: object) -> FakeChatClient:
    return FakeChatClient(content, **message_fields)


def failing_llm(error: Exception) -> AsyncMock:
    client = AsyncMock()
    client.ainvoke = AsyncMock(side_effect=error)
    return client

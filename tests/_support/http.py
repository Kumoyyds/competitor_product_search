from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class FakeAsyncClient:
    """Scriptable httpx-style async client with a shared response queue."""

    queue: list[Any] = []
    calls: list[tuple[str, str]] = []
    requests: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.closed = False

    @classmethod
    def script(cls, responses: Iterable[Any]) -> None:
        cls.queue = list(responses)
        cls.calls = []
        cls.requests = []

    reset = script

    @classmethod
    def set_shared_queue(cls, responses: list[Any]) -> None:
        cls.queue = responses

    @classmethod
    def set_shared_tracker(cls, tracker: list[tuple[str, str]]) -> None:
        cls._shared_tracker = tracker

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self.closed = True

    async def _request(
        self, method: str, *args: object, **kwargs: object
    ) -> Any:
        url = str(args[0]) if args else ""
        type(self).calls.append((method, url))
        type(self).requests.append((method, args, kwargs))
        tracker = getattr(type(self), "_shared_tracker", None)
        if tracker is not None:
            tracker.append((method, url))
        if self.closed:
            raise RuntimeError("Client already closed")
        if not type(self).queue:
            raise RuntimeError("Unexpected request: response queue empty")
        response = type(self).queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def get(self, *args: object, **kwargs: object) -> Any:
        return await self._request("GET", *args, **kwargs)

    async def post(self, *args: object, **kwargs: object) -> Any:
        return await self._request("POST", *args, **kwargs)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.sleep_calls = 0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1
        self.sleeps.append(seconds)
        self.now += seconds

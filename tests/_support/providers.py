from __future__ import annotations

from collections.abc import Iterable

from src.search.models import RawCandidate
from src.search.providers.base import SearchProvider


class FakeSearchProvider(SearchProvider):
    def __init__(
        self,
        results: Iterable[RawCandidate] | None = None,
        error: Exception | None = None,
        name: str = "fake",
    ) -> None:
        self.results = list(results or [])
        self.error = error
        self.name = name
        self.calls = 0
        self.closed = False

    async def search(
        self, query: str, k: int = 10, country: str = "uk"
    ) -> list[RawCandidate]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.results)

    def calls_made(self) -> int:
        return self.calls

    async def aclose(self) -> None:
        self.closed = True

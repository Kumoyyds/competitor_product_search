from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import RawCandidate


class SearchProviderError(Exception):
    pass


class BudgetExhausted(SearchProviderError):
    pass


class SearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def search(self, query: str, k: int = 10, country: str = "uk") -> list[RawCandidate]:
        ...

    def calls_made(self) -> int:
        return 0

    async def aclose(self) -> None:
        return None

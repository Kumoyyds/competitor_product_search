from __future__ import annotations

from typing import Any


class FakeScraper:
    """Configurable scraper double for router and orchestration tests."""

    def __init__(
        self,
        result: Any = None,
        raises: Exception | None = None,
        *,
        site: str = "test",
        order: int = 1,
    ) -> None:
        self.result = result
        self.raises = raises
        self.site = site
        self._order = order
        self.calls: list[str] = []

    async def scrape(self, url: str) -> Any:
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.result

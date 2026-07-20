from __future__ import annotations

from abc import ABC, abstractmethod

from ..models.enums import SourceType
from ..models.results import ScrapeOutcome


class BaseScraper(ABC):
    """Abstract scraper contract (spec §4.2).

    Subclasses must set `site` and `source_type` class attributes
    and implement `async scrape(url)`.
    Terminal failure raises ScrapeFailed (carries signature + snapshot + failed stage).
    """

    site: str
    source_type: SourceType

    _order: int = 1

    def _success_path(self) -> str:
        """Return the scrape_runs.path label for a direct-success outcome.

        order=1 (primary scraper) → "fast"
        order=2 (first backup)  → "backup_1"
        order=3 (second backup) → "backup_2"
        """
        order = getattr(self, "_order", 1)
        return "fast" if order <= 1 else f"backup_{order - 1}"

    @abstractmethod
    async def scrape(self, url: str) -> ScrapeOutcome:
        ...

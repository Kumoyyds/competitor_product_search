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

    @abstractmethod
    async def scrape(self, url: str) -> ScrapeOutcome:
        ...

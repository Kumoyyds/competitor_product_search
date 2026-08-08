"""Scraping module — extracts structured product data from marketplace pages.

Public API:
    - scrape(url) -> ScrapeOutcome  (via router)
    - ProductData                    (Pydantic model)
    - InvalidTargetResult            (not-a-product sentinel)
    - ScrapeFailed                   (terminal failure exception)
"""

from .exceptions import BrightDataInfraError, ScrapeFailed
from .models import InvalidTargetResult, ProductData, ScrapeOutcome
from .router import scrape

# Trigger site scraper registration
from .scrapers import sites as _sites  # noqa: F401

__all__ = [
    "scrape",
    "ProductData",
    "InvalidTargetResult",
    "ScrapeOutcome",
    "ScrapeFailed",
    "BrightDataInfraError",
]

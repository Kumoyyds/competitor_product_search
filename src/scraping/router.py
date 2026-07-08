from __future__ import annotations

import logging
from urllib.parse import urlparse

from .exceptions import BrightDataInfraError, ScrapeFailed
from .models.results import ScrapeOutcome
from .registry import get_scrapers

logger = logging.getLogger(__name__)

HOST_TO_SITE: dict[str, str] = {
    "tesco.com": "tesco",
    "www.tesco.com": "tesco",
    "argos.co.uk": "argos",
    "www.argos.co.uk": "argos",
    "amazon.co.uk": "amazon",
    "www.amazon.co.uk": "amazon",
    "amazon.de": "amazon",
    "www.amazon.de": "amazon",
    "amazon.fr": "amazon",
    "www.amazon.fr": "amazon",
}


def resolve_site(url: str) -> str:
    host = urlparse(url).hostname or ""
    site = HOST_TO_SITE.get(host)
    if site is None:
        raise ValueError(f"Unknown host: {host} (url={url})")
    return site


async def scrape(url: str) -> ScrapeOutcome:
    """Two-hop dispatch + scraper-level fallback (spec §4.1, §5.13).

    hop1: host -> site
    hop2: site -> ordered scraper list
    Tries each scraper in order; ScrapeFailed triggers fallback to next.
    BrightDataInfraError bypasses fallback (D21).
    """
    site = resolve_site(url)
    scraper_classes = get_scrapers(site)
    if not scraper_classes:
        raise ValueError(f"No scrapers registered for site: {site}")

    failures: list[ScrapeFailed] = []

    for scraper_cls in scraper_classes:
        scraper = scraper_cls()
        try:
            result = await scraper.scrape(url)
            return result
        except BrightDataInfraError:
            raise
        except ScrapeFailed as e:
            logger.warning(
                "Scraper %s failed for %s at stage=%s, trying next",
                scraper_cls.__name__,
                url,
                e.failed_stage,
            )
            failures.append(e)

    last = failures[-1] if failures else None
    raise ScrapeFailed(
        site=site,
        url=url,
        scraper_name="all_exhausted",
        failed_stage="scraper_fallback_exhausted",
        errors=[str(f) for f in failures],
        snapshot=last.snapshot if last else None,
    )

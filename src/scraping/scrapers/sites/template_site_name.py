from __future__ import annotations

from ...extraction import BrightDataUnlocker
from ...registry import register_scraper
from ..html_scraper import HTMLScraper


@register_scraper("YOUR_SITE_NAME", order=1)
class YOUR_SITE_NAMEScraper(HTMLScraper):
    """YOUR_SITE_NAME HTML primary route.

    Uses Scraping Browser / CDP to handle Akamai TLS fingerprinting.
    Parser list will be implemented in M6.
    """

    def _get_unlocker(self) -> BrightDataUnlocker:
        return BrightDataUnlocker(zone="web_unlocker1", country="gb")

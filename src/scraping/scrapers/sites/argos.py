from __future__ import annotations

from ...extraction import BrightDataUnlocker
from ...registry import register_scraper
from ..html_scraper import HTMLScraper


@register_scraper("argos", order=1)
class ArgosScraper(HTMLScraper):
    """Argos HTML route (plain Web Unlocker).

    Parser list will be implemented in M6.
    """

    def _get_unlocker(self) -> BrightDataUnlocker:
        return BrightDataUnlocker(zone="web_unlocker1", country="gb")

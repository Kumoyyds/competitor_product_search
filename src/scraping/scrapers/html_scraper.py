"""HTMLScraper — Template Method for HTML-based scraping (spec §4.3).

Shared logic: extraction retry → invalid target pre-detection → ordered parser list
→ repair ladder. Site subclasses only fill: site identifier, extraction config.

M5 scope: extraction + detection. Parser list (M6) and repair ladder (M8) are placeholders.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from typing import Any, Optional
from urllib.parse import urlparse

from ..config import get_config
from ..detection import detect_invalid_page
from ..exceptions import BrightDataInfraError, ScrapeFailed
from ..extraction import BrightDataUnlocker, with_extraction_retry
from ..models.product_data import ProductData
from ..models.results import InvalidTargetResult, ScrapeOutcome
from ..storage import PhraseStore, ResultStore, RunStore, ScrapeDB
from ..validation import validate
from .base import BaseScraper

logger = logging.getLogger(__name__)


class HTMLScraper(BaseScraper):
    """Template Method for all HTML-route scrapers.

    Subclasses must implement _get_unlocker() to provide site-specific
    BrightData configuration.
    """

    source_type = "html"

    @abstractmethod
    def _get_unlocker(self) -> BrightDataUnlocker:
        ...

    async def scrape(self, url: str) -> ScrapeOutcome:
        host = urlparse(url).hostname or ""
        start = time.monotonic()

        # Step 1: Extraction (with retry, D7)
        try:
            status_code, html = await with_extraction_retry(
                self._get_unlocker().fetch, url
            )
        except BrightDataInfraError:
            raise
        except Exception as e:
            raise ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="extraction",
                errors=[str(e)],
            )

        # Step 2: Invalid target pre-detection (§5.15)
        phrases = self._load_phrases()
        signal = detect_invalid_page(html, status_code, self.site, phrases)
        if signal is not None:
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "invalid_target", "invalid_target", latency=latency)
            return InvalidTargetResult(
                url=url,
                site=self.site,
                reason_signal=f"{signal.signal_type}: {signal.detail}",
            )

        # Step 3: Parser list (M6 placeholder — will iterate ordered parsers)
        parsed = self._run_parsers(html, url)
        if parsed is None:
            raise ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="no_parser",
                errors=["No parser available (M6 not yet implemented)"],
                snapshot=html[:2000],
            )

        # Step 4: Two gates (public checkpoint)
        product, errors = validate(parsed)
        if product is not None:
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "success", "fast", latency=latency)
            self._store_result(product)
            return product

        # Step 5: Repair ladder (M8 placeholder)
        raise ScrapeFailed(
            site=self.site,
            url=url,
            scraper_name=self.__class__.__name__,
            failed_stage="gate_validation",
            errors=errors,
            snapshot=html[:2000],
        )

    def _run_parsers(self, html: str, url: str) -> Optional[dict[str, Any]]:
        """Run ordered parser list against HTML. Returns dict or None.

        M6 will implement actual parser iteration. For now returns None
        (triggers ScrapeFailed, which enables scraper-level fallback to DCA).
        """
        return None

    def _load_phrases(self) -> list[str]:
        try:
            cfg = get_config()
            db = ScrapeDB(cfg.db_path)
            db.init_db()
            phrases = PhraseStore(db).get_phrases(self.site)
            db.close()
            return phrases
        except Exception:
            return []

    def _record_run(
        self,
        url: str,
        host: str,
        outcome: str,
        path: str,
        latency: Optional[int] = None,
    ) -> None:
        try:
            cfg = get_config()
            db = ScrapeDB(cfg.db_path)
            db.init_db()
            store = RunStore(db, cfg.scrape_runs_dedup_window_seconds)
            if not store.is_duplicate(url):
                store.record(
                    url=url,
                    host=host,
                    site=self.site,
                    scraper=self.__class__.__name__,
                    outcome=outcome,
                    path=path,
                    latency_ms=latency,
                )
            db.close()
        except Exception:
            logger.exception("Failed to record scrape run")

    def _store_result(self, product: ProductData) -> None:
        try:
            cfg = get_config()
            db = ScrapeDB(cfg.db_path)
            db.init_db()
            ResultStore(db).append(product)
            db.close()
        except Exception:
            logger.exception("Failed to store result")

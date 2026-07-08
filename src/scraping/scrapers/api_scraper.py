from __future__ import annotations

import logging
import time
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from ..config import get_config
from ..exceptions import BrightDataInfraError, ScrapeFailed
from ..models.product_data import ProductData
from ..models.results import InvalidTargetResult, ScrapeOutcome
from ..storage import ResultStore, RunStore, ScrapeDB
from ..validation import validate
from .base import BaseScraper

logger = logging.getLogger(__name__)


class DirectAPIScraper(BaseScraper):
    """API route scraper (spec §4.4).

    Subclasses implement:
      - _fetch_json(url): call BrightData API and return raw JSON dict
      - _map_fields(json_data): map API JSON to ProductData-compatible dict
      - _is_not_found(json_data): detect "product not found/delisted" response
    """

    source_type = "api"

    @abstractmethod
    async def _fetch_json(self, url: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def _map_fields(self, json_data: dict[str, Any], url: str) -> dict[str, Any]:
        ...

    def _is_not_found(self, json_data: dict[str, Any]) -> bool:
        return False

    async def scrape(self, url: str) -> ScrapeOutcome:
        host = urlparse(url).hostname or ""
        start = time.monotonic()

        try:
            json_data = await self._fetch_json(url)
        except BrightDataInfraError:
            raise
        except Exception as e:
            raise ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="api_fetch",
                errors=[str(e)],
            )

        if self._is_not_found(json_data):
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "invalid_target", "invalid_target", latency=latency)
            return InvalidTargetResult(
                url=url, site=self.site, reason_signal="api_not_found"
            )

        mapped = self._map_fields(json_data, url)
        product, errors = validate(mapped)

        if product is not None:
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "success", "fast", latency=latency)
            self._store_result(product)
            return product

        # Gate failure -> terminal for API route (JSON self-healing placeholder for M8)
        raise ScrapeFailed(
            site=self.site,
            url=url,
            scraper_name=self.__class__.__name__,
            failed_stage="gate_validation",
            errors=errors,
        )

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

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from typing import Any, ClassVar, Optional
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
      - _map_fields(json_data, url): map API JSON to ProductData-compatible dict
      - _is_not_found(json_data): detect "product not found/delisted" response (D25)

    Includes restricted JSON self-healing (§5.14, D25) on gate failure.
    """

    source_type = "api"

    # Class-level in-memory cache of {site: {target_field: source_dotted_path}}
    # from a successful heal. Applied at top of scrape() before validation.
    # In-memory only for Phase 0 — see coldstart doc.
    _json_heal_cache: ClassVar[dict[str, dict[str, str]]] = {}

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
        except BrightDataInfraError as e:
            # Same reasoning as HTMLScraper: this API scraper's BrightData
            # channel is unhealthy, but sibling scrapers in the router chain
            # use independent channels. Convert to scraper-scoped ScrapeFailed
            # so router can try the next scraper. If all scrapers' channels
            # are down, router's _derive_reason promotes back to infra_failure.
            raise ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="api_fetch",
                signature=(self.site, "api_infra", ""),
                errors=[f"BrightDataInfraError: {e}"],
            )
        except Exception as e:
            raise ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="api_fetch",
                signature=(self.site, "api_fetch", ""),
                errors=[str(e)],
            )

        if self._is_not_found(json_data):
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "invalid_target", "invalid_target", latency=latency)
            return InvalidTargetResult(
                url=url, site=self.site, reason_signal="api_not_found"
            )

        mapped = self._map_fields(json_data, url)
        mapped = self._apply_heal_cache(json_data, mapped)
        product, errors = validate(mapped)

        if product is not None:
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "success", self._success_path(), latency=latency)
            self._store_result(product)
            return product

        # Gate failure -> attempt restricted JSON self-healing (M8, spec §5.14)
        from ..repair.json_healer import heal_json

        healed = await heal_json(json_data, mapped, errors, self.site)
        if healed is not None:
            product, errors2 = validate(healed)
            if product is not None:
                self._cache_heal(json_data, mapped, healed)
                latency = int((time.monotonic() - start) * 1000)
                self._record_run(url, host, "success", "agent_repaired",
                                 model_used="qwen-3.7-plus", latency=latency)
                self._store_result(product)
                return product
            errors.extend(errors2)

        raise ScrapeFailed(
            site=self.site,
            url=url,
            scraper_name=self.__class__.__name__,
            failed_stage="api_malformed",
            signature=(self.site, "gate_validation", ""),
            errors=errors,
        )

    def _apply_heal_cache(
        self, json_data: dict[str, Any], mapped: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply any previously-healed field mapping for this site."""
        cache = self._json_heal_cache.get(self.site)
        if not cache:
            return mapped

        from ..repair.json_healer import _lookup_path

        out = dict(mapped)
        for target, source_path in cache.items():
            if out.get(target) is None:
                value = _lookup_path(json_data, source_path)
                if value is not None:
                    out[target] = value
        return out

    def _cache_heal(
        self,
        json_data: dict[str, Any],
        pre_heal: dict[str, Any],
        healed: dict[str, Any],
    ) -> None:
        """Extract the field mapping that made the heal succeed and cache it for future scrapes."""
        # For each field that was None pre-heal and non-None post-heal, we don't know the
        # exact source path used by the LLM, but we can search JSON for that value.
        # Simpler: skip caching for Phase 0 and let each heal recompute.
        # (Kept as a hook — actual caching implementation deferred to Phase 1.)
        pass

    def _record_run(
        self,
        url: str,
        host: str,
        outcome: str,
        path: str,
        model_used: Optional[str] = None,
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
                    model_used=model_used,
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

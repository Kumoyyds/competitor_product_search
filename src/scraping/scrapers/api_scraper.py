from __future__ import annotations

import json
import logging
import time
from abc import abstractmethod
from typing import Any, ClassVar
from urllib.parse import urlparse

from ..config import get_config
from ..exceptions import BrightDataInfraError, ScrapeFailed
from ..models.results import InvalidTargetResult, ScrapeOutcome
from ..validation import validate
from .base import BaseScraper
from .price_fields import normalize_price_fields

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
            failure = ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="api_fetch",
                signature=(self.site, "api_infra", ""),
                errors=[f"BrightDataInfraError: {e}"],
            )
            self._record_failure(
                url, host, failure, int((time.monotonic() - start) * 1000)
            )
            raise failure
        except Exception as e:
            failure = ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="api_fetch",
                signature=(self.site, "api_fetch", ""),
                errors=[str(e)],
            )
            self._record_failure(
                url, host, failure, int((time.monotonic() - start) * 1000)
            )
            raise failure

        if self._is_not_found(json_data):
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "invalid_target", "invalid_target", latency=latency)
            return InvalidTargetResult(
                url=url, site=self.site, reason_signal="api_not_found"
            )

        mapped = self._map_fields(json_data, url)
        mapped = self._apply_heal_cache(json_data, mapped)
        mapped, dropped = normalize_price_fields(mapped)
        if dropped:
            logger.info("price contract dropped %s (url=%s)", dropped, url)
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
                cfg = get_config()
                self._record_run(
                    url,
                    host,
                    "success",
                    "agent_repaired",
                    model_used=cfg.repair_model_ladder[0],
                    latency=latency,
                )
                self._store_result(product)
                return product
            errors.extend(errors2)

        failure = ScrapeFailed(
            site=self.site,
            url=url,
            scraper_name=self.__class__.__name__,
            failed_stage="api_malformed",
            signature=(self.site, "gate_validation", ""),
            errors=errors,
            snapshot=json.dumps(json_data, default=str)[:8000],
        )
        self._record_failure(
            url, host, failure, int((time.monotonic() - start) * 1000)
        )
        raise failure

    def _apply_heal_cache(
        self, json_data: dict[str, Any], mapped: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply any previously-healed field mapping for this site."""
        cache = self._json_heal_cache.get(self.site)
        if not cache:
            return mapped

        from ..repair.json_healer import _is_unit_price_source, _lookup_path

        out = dict(mapped)
        for target, source_path in cache.items():
            if out.get(target) is None:
                value = _lookup_path(json_data, source_path)
                if value is not None and not _is_unit_price_source(
                    target, source_path, value
                ):
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

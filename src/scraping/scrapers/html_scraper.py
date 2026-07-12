"""HTMLScraper — Template Method for HTML-based scraping (spec §4.3).

Shared logic: extraction retry → invalid target pre-detection → ordered parser list
→ two gates → repair ladder (M8). Site subclasses only fill: site identifier, extraction config.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from ..config import get_config
from ..detection import detect_invalid_page
from ..exceptions import BrightDataInfraError, ScrapeFailed
from ..extraction import BrightDataUnlocker, with_extraction_retry
from ..models.product_data import ProductData
from ..models.results import InvalidTargetResult, ScrapeOutcome
from ..repair import (
    SandboxException,
    SandboxTimeout,
    SandboxViolation,
    run_in_sandbox,
)
from ..storage import (
    EscalationStore,
    ParserStore,
    PhraseStore,
    ResultStore,
    RunStore,
    ScrapeDB,
)
from ..validation import validate
from .base import BaseScraper

logger = logging.getLogger(__name__)


class HTMLScraper(BaseScraper):
    """Template Method for all HTML-route scrapers.

    Subclasses must implement `_get_unlocker()` to provide site-specific
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
        except BrightDataInfraError as e:
            # This scraper's BrightData channel (Web Unlocker) is unhealthy for
            # this URL, but sibling scrapers in the router chain use independent
            # channels (e.g. TescoDCAScraper hits the DCA API). Convert to a
            # scraper-scoped ScrapeFailed so the router can try the next scraper
            # instead of escalating immediately. If every scraper's channel is
            # also down, _derive_reason() at the router promotes this back to
            # `infra_failure` on the final escalation.
            raise ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="extraction",
                signature=(self.site, "extraction_infra", ""),
                errors=[f"BrightDataInfraError: {e}"],
            )
        except Exception as e:
            raise ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="extraction",
                signature=(self.site, "extraction", ""),
                errors=[str(e)],
            )

        # Step 2: Invalid target pre-detection (§5.15)
        phrases = self._load_phrases()
        signal = detect_invalid_page(html, status_code, self.site, phrases)
        if signal is not None:
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "invalid_target", "invalid_target", latency=latency)
            self._check_mass_invalid_target(host, url)
            return InvalidTargetResult(
                url=url,
                site=self.site,
                reason_signal=f"{signal.signal_type}: {signal.detail}",
            )

        # Step 3: Parser list (M6)
        parser_run = await self._run_parsers(html, url)

        if parser_run is not None:
            parsed_dict, parser_id, parser_version, parser_errors = parser_run
            # Step 4: Two gates (public checkpoint)
            product, errors = validate(parsed_dict)
            if product is not None:
                latency = int((time.monotonic() - start) * 1000)
                self._record_run(
                    url, host, "success", "fast",
                    winning_parser_id=parser_id,
                    latency=latency,
                )
                self._store_result(product)
                self._on_success(html, product)
                return product
            parser_errors.extend(errors)
        else:
            parser_errors = ["no active parsers or none succeeded"]

        # Step 5: Repair ladder (M8) — imported lazily to avoid circular import
        from ..repair.agent import run_repair_ladder

        outcome = await run_repair_ladder(
            scraper=self,
            url=url,
            html=html,
            initial_errors=parser_errors,
        )

        if isinstance(outcome, ProductData):
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(
                url, host, "success", "agent_repaired",
                model_used=outcome.parser_version,
                latency=latency,
            )
            self._store_result(outcome)
            self._on_success(html, outcome)
            return outcome

        if isinstance(outcome, InvalidTargetResult):
            latency = int((time.monotonic() - start) * 1000)
            self._record_run(url, host, "invalid_target", "invalid_target", latency=latency)
            self._check_mass_invalid_target(host, url)
            return outcome

        # outcome is ScrapeFailed
        raise outcome

    # ------------------------------------------------------------------
    # M6 — Ordered parser list
    # ------------------------------------------------------------------

    async def _run_parsers(
        self, html: str, url: str
    ) -> Optional[tuple[dict[str, Any], int, str, list[str]]]:
        """Iterate active parsers ordered by hit rate; return first successful result.

        Returns (parsed_dict, parser_id, parser_version, accumulated_errors) if a parser
        produced a dict that passes both gates. Returns None if list is empty.
        If parsers ran but none passed gates, still returns the last attempt's dict
        for repair ladder context, along with all accumulated errors.
        """
        cfg = get_config()
        errors: list[str] = []

        db = ScrapeDB(cfg.db_path)
        db.init_db()
        parsers = ParserStore(db).get_active_ordered_by_hits(self.site)
        db.close()

        if not parsers:
            return None

        for parser in parsers:
            code = parser["code"]
            result = await run_in_sandbox(code, html, url)

            if isinstance(result, dict):
                wrapped = self._wrap_parser_output(result, parser["version"], url)
                product, gate_errors = validate(wrapped)
                if product is not None:
                    return wrapped, parser["id"], parser["version"], errors
                errors.append(
                    f"parser {parser['version']} (id={parser['id']}) gate fail: {gate_errors}"
                )
                continue

            if isinstance(result, SandboxViolation):
                errors.append(
                    f"parser {parser['version']} (id={parser['id']}) sandbox violation: {result.reason}"
                )
            elif isinstance(result, SandboxTimeout):
                errors.append(
                    f"parser {parser['version']} (id={parser['id']}) sandbox timeout ({result.timeout}s)"
                )
            elif isinstance(result, SandboxException):
                errors.append(
                    f"parser {parser['version']} (id={parser['id']}) raised {result.type_name}: {result.message}"
                )

        # All parsers ran but none returned a valid dict
        return {}, 0, "", errors

    def _wrap_parser_output(
        self, raw: dict[str, Any], parser_version: str, url: str
    ) -> dict[str, Any]:
        """Fill in tracing fields on the parser's raw dict (parser only produces extraction)."""
        wrapped = dict(raw)
        wrapped.setdefault("url", url)
        wrapped["website"] = self.site
        wrapped["source_type"] = "html"
        wrapped["scraped_at"] = datetime.now(timezone.utc)
        wrapped["parser_version"] = parser_version
        return wrapped

    # ------------------------------------------------------------------
    # M9 — Golden seeding + prune on success path
    # ------------------------------------------------------------------

    def _on_success(self, html: str, product: ProductData) -> None:
        """Opportunistic golden seeding + prune (best-effort, swallows exceptions)."""
        try:
            from ..repair.golden import maybe_seed_golden, prune_stale

            maybe_seed_golden(self.site, html, product)
            prune_stale(self.site)
        except Exception:
            logger.exception("_on_success hook failed (non-fatal)")

    # ------------------------------------------------------------------
    # M10 — Mass invalid_target detection
    # ------------------------------------------------------------------

    def _check_mass_invalid_target(self, host: str, url: str) -> None:
        try:
            cfg = get_config()
            db = ScrapeDB(cfg.db_path)
            db.init_db()
            runs = RunStore(db, cfg.scrape_runs_dedup_window_seconds)
            count = runs.count_invalid_targets(self.site, window_hours=24)
            total = runs.count_total_runs(self.site, window_hours=24)

            trigger = False
            if total >= 5:
                ratio = count / total
                if ratio > cfg.mass_invalid_target_ratio:
                    trigger = True
                if count > cfg.mass_invalid_target_absolute:
                    trigger = True

            if trigger:
                EscalationStore(db).upsert(
                    signature=f"{self.site}|invalid_target_surge|",
                    reason="mass_invalid_target",
                    snapshot={
                        "site": self.site,
                        "invalid_target_count": count,
                        "total_runs": total,
                        "window_hours": 24,
                        "sample_url": url,
                    },
                )
                logger.warning(
                    "mass_invalid_target: site=%s count=%d total=%d",
                    self.site, count, total,
                )
            db.close()
        except Exception:
            logger.exception("mass_invalid_target check failed (non-fatal)")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
        winning_parser_id: Optional[int] = None,
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
                    winning_parser_id=winning_parser_id,
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

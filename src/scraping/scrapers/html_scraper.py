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
from ..exceptions import BrightDataInfraError, SandboxSpawnError, ScrapeFailed
from ..extraction import BrightDataUnlocker, with_extraction_retry
from ..models.product_data import ProductData
from ..models.results import InvalidTargetResult, ScrapeOutcome
from ..repair import (
    SandboxException,
    SandboxTimeout,
    SandboxViolation,
    run_in_sandbox,
)
from ..storage import EscalationStore, ParserStore, PhraseStore, RunStore, ScrapeDB
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
            failure = ScrapeFailed(
                site=self.site,
                url=url,
                scraper_name=self.__class__.__name__,
                failed_stage="extraction",
                signature=(self.site, "extraction_infra", ""),
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
                failed_stage="extraction",
                signature=(self.site, "extraction", ""),
                errors=[str(e)],
            )
            self._record_failure(
                url, host, failure, int((time.monotonic() - start) * 1000)
            )
            raise failure

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
        try:
            parser_run = await self._run_parsers(html, url)
        except (SandboxSpawnError, OSError) as exc:
            failure = self._sandbox_spawn_failure(url, "parser_list", exc, html)
            self._record_failure(
                url, host, failure, int((time.monotonic() - start) * 1000)
            )
            raise failure from exc

        if parser_run is not None:
            parsed_dict, parser_id, parser_version, parser_errors = parser_run
            # Step 4: Two gates (public checkpoint)
            product, errors = validate(parsed_dict)
            if product is not None:
                latency = int((time.monotonic() - start) * 1000)
                self._record_run(
                    url, host, "success", self._success_path(),
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

        try:
            outcome = await run_repair_ladder(
                scraper=self,
                url=url,
                html=html,
                initial_errors=parser_errors,
            )
        except (SandboxSpawnError, OSError) as exc:
            failure = self._sandbox_spawn_failure(url, "repair", exc, html)
            self._record_failure(
                url, host, failure, int((time.monotonic() - start) * 1000)
            )
            raise failure from exc

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
        self._record_failure(
            url, host, outcome, int((time.monotonic() - start) * 1000)
        )
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

        db: ScrapeDB | None = None
        try:
            db = ScrapeDB(cfg.db_path)
            db.init_db()
            parsers = ParserStore(db).get_active_ordered_by_hits(self.site)
        finally:
            if db is not None:
                db.close()

        if not parsers:
            return None

        # --- Fast-path distrust: parse HTML once for structural sanity checks ---
        # A parser saved from a different page type may emit blobs or miss a
        # second price that is clearly visible on *this* page.  We run
        # lightweight structural checks and distrust the parser if they fail —
        # this causes a fall-through to the repair ladder which self-heals
        # to a better vN+1 parser.
        from bs4 import BeautifulSoup as _Bs

        _soup: Any = None

        def _ensure_soup() -> Any:
            nonlocal _soup
            if _soup is None:
                _soup = _Bs(html, "lxml")
            return _soup

        for parser in parsers:
            code = parser["code"]
            result = await run_in_sandbox(code, html, url)

            if isinstance(result, dict):
                wrapped = self._wrap_parser_output(result, parser["version"], url)
                product, gate_errors = validate(wrapped)
                if product is not None:
                    # --- fast-path distrust guard (M15) ---
                    distrust_reason = _fast_path_sane(
                        result, product, _ensure_soup(), self.site
                    )
                    if distrust_reason is not None:
                        errors.append(
                            f"parser {parser['version']} (id={parser['id']}) "
                            f"distrusted: {distrust_reason}"
                        )
                        continue  # skip — repair ladder will generate a better parser
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
        db: ScrapeDB | None = None
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
        except Exception:
            logger.exception("mass_invalid_target check failed (non-fatal)")
        finally:
            if db is not None:
                db.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_phrases(self) -> list[str]:
        db: ScrapeDB | None = None
        try:
            cfg = get_config()
            db = ScrapeDB(cfg.db_path)
            db.init_db()
            phrases = PhraseStore(db).get_phrases(self.site)
            return phrases
        except Exception:
            return []
        finally:
            if db is not None:
                db.close()

    def _sandbox_spawn_failure(
        self, url: str, stage: str, exc: OSError, html: str
    ) -> ScrapeFailed:
        return ScrapeFailed(
            site=self.site,
            url=url,
            scraper_name=self.__class__.__name__,
            failed_stage=stage,
            signature=(self.site, "sandbox_spawn", ""),
            snapshot=html[:2000],
            errors=[f"{type(exc).__name__}: {exc}"],
        )

# ---------------------------------------------------------------------------
# Fast-path distrust guard (M15)
# ---------------------------------------------------------------------------


def _fast_path_sane(
    raw_result: dict[str, Any],
    product: Any,
    soup: Any,
    site: str | None = None,
) -> Optional[str]:
    """Check whether a fast-path (reused-parser) output is structurally credible.

    Returns None if the output appears sane, or a reason string if it should
    be distrusted — causing fall-through to the repair ladder which self-heals
    to a better parser.

    Checks (all generic, no site-hard-coded strings):
      1. availability_raw is a JSON blob (entire JSON-LD script assigned instead
         of the short human label).
      2. A promotion signal (discount or membership) is structurally detected on
         the page, but the parser returned neither list_price nor membership_price
         — a reused parser from a single-price page that missed the second price.
    """
    # 1. availability blob — a JSON-object string at any length is a blob
    avail = raw_result.get("availability_raw")
    if isinstance(avail, str):
        looks_like_json = ("{" in avail or "[" in avail) and (
            "@context" in avail or "schema.org" in avail
            or '"@type"' in avail or '"@graph"' in avail
        )
        if looks_like_json or (len(avail) > 80 and ("{" in avail or "@context" in avail)):
            return "availability_raw is a JSON blob (not a short label)"

    # 2. Promotion signal present but parser missed it
    try:
        from src.scraping.repair.prepass import detect_promotion  # type: ignore[import]

        trusted = {
            str(value)
            for value in (
                product.price,
                product.list_price,
                product.membership_price,
            )
            if value is not None
        }
        signal = detect_promotion(soup, trusted, site=site)
        if signal and signal.get("kind") in ("discount", "membership"):
            has_list = (
                product.list_price is not None
                and product.list_price != product.price
            )
            has_member = (
                product.membership_price is not None
                and product.membership_price != product.price
            )
            if not has_list and not has_member:
                return (
                    f"promotion signal '{signal['kind']}' detected on page "
                    f"(evidence: {signal.get('evidence_text', '')[:120]}), "
                    f"but parser returned neither list_price nor membership_price"
                )
    except Exception:
        # Non-fatal — if promotion detection fails, trust the parser, but leave
        # enough evidence for production diagnosis.
        logger.debug("fast-path promotion detection failed", exc_info=True)

    return None  # sane

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from ..config import get_config
from ..exceptions import ScrapeFailed, format_scrape_signature
from ..models.enums import SourceType
from ..models.product_data import ProductData
from ..models.results import ScrapeOutcome
from ..storage import ResultStore, RunStore, ScrapeDB

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract scraper contract (spec §4.2).

    Subclasses must set `site` and `source_type` class attributes
    and implement `async scrape(url)`.
    Terminal failure raises ScrapeFailed (carries signature + snapshot + failed stage).
    """

    site: str
    source_type: SourceType

    _order: int = 1

    def _success_path(self) -> str:
        """Return the scrape_runs.path label for a direct-success outcome.

        order=1 (primary scraper) → "fast"
        order=2 (first backup)  → "backup_1"
        order=3 (second backup) → "backup_2"
        """
        order = getattr(self, "_order", 1)
        return "fast" if order <= 1 else f"backup_{order - 1}"

    def _record_run(
        self,
        url: str,
        host: str,
        outcome: str,
        path: str,
        winning_parser_id: Optional[int] = None,
        repair_model: Optional[str] = None,
        latency: Optional[int] = None,
        signature: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Optional[int]:
        """Best-effort execution logging; return the canonical run id."""
        db: ScrapeDB | None = None
        try:
            cfg = get_config()
            db = ScrapeDB(cfg.db_path)
            db.init_db()
            return RunStore(db).record(
                url=url,
                host=host,
                site=self.site,
                scraper=self.__class__.__name__,
                outcome=outcome,
                path=path,
                winning_parser_id=winning_parser_id,
                repair_model=repair_model,
                latency_ms=latency,
                signature=signature,
                error=error,
            )
        except Exception:
            logger.exception("Failed to record scrape run")
            return None
        finally:
            if db is not None:
                db.close()

    def _record_failure(
        self,
        url: str,
        host: str,
        failure: ScrapeFailed,
        latency: Optional[int] = None,
    ) -> Optional[int]:
        error = " | ".join(str(item) for item in failure.errors)[:2000]
        run_id = self._record_run(
            url,
            host,
            "escalated",
            "escalated",
            latency=latency,
            signature=format_scrape_signature(
                self.site, failure.signature, failure.failed_stage
            ),
            error=error or failure.failed_stage,
        )
        failure.run_id = run_id
        return run_id

    def _store_result(
        self,
        product: ProductData,
        run_id: Optional[int] = None,
    ) -> None:
        db: ScrapeDB | None = None
        try:
            cfg = get_config()
            db = ScrapeDB(cfg.db_path)
            db.init_db()
            ResultStore(db).append(product, run_id=run_id)
        except Exception:
            logger.exception("Failed to store result")
        finally:
            if db is not None:
                db.close()

    @abstractmethod
    async def scrape(self, url: str) -> ScrapeOutcome:
        ...

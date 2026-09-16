from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from .database import ScrapeDB


_captured_run_ids: ContextVar[list[int] | None] = ContextVar(
    "captured_scrape_run_ids", default=None
)


@contextmanager
def capture_run_ids() -> Iterator[list[int]]:
    """Collect run IDs written by one scrape call, including scraper fallbacks."""
    run_ids: list[int] = []
    token = _captured_run_ids.set(run_ids)
    try:
        yield run_ids
    finally:
        _captured_run_ids.reset(token)


class RunStore:
    def __init__(self, db: ScrapeDB):
        self._db = db

    def record(
        self,
        url: str,
        host: str,
        site: str,
        scraper: str,
        outcome: str,
        path: str,
        winning_parser_id: Optional[int] = None,
        attempts: int = 1,
        repair_model: Optional[str] = None,
        latency_ms: Optional[int] = None,
        signature: Optional[str] = None,
        error: Optional[str] = None,
    ) -> int:
        cur = self._db.conn.execute(
            "INSERT INTO scrape_runs "
            "(url, host, site, scraper, outcome, path, winning_parser_id, attempts, "
            "repair_model, latency_ms, signature, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                url, host, site, scraper, outcome, path, winning_parser_id,
                attempts, repair_model, latency_ms, signature, error,
            ),
        )
        self._db.conn.commit()
        run_id = int(cur.lastrowid)
        captured = _captured_run_ids.get()
        if captured is not None:
            captured.append(run_id)
        return run_id

    def attach_escalation(
        self,
        run_ids: Sequence[int],
        escalation_id: int,
    ) -> int:
        """Attach an aggregate escalation ticket to its per-execution runs."""
        unique_ids = tuple(dict.fromkeys(run_ids))
        if not unique_ids:
            return 0
        placeholders = ", ".join("?" for _ in unique_ids)
        cur = self._db.conn.execute(
            f"UPDATE scrape_runs SET escalation_id = ? WHERE id IN ({placeholders})",
            (escalation_id, *unique_ids),
        )
        self._db.conn.commit()
        return cur.rowcount

    def get_by_escalation(self, escalation_id: int) -> list[dict[str, Any]]:
        rows = self._db.conn.execute(
            "SELECT * FROM scrape_runs WHERE escalation_id = ? ORDER BY id DESC",
            (escalation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_hit_rates(self, site: str) -> list[dict[str, Any]]:
        """Real-time hit rate aggregation (D17)."""
        rows = self._db.conn.execute(
            "SELECT winning_parser_id, COUNT(*) as hits "
            "FROM scrape_runs WHERE site = ? AND outcome = 'success' AND winning_parser_id IS NOT NULL "
            "GROUP BY winning_parser_id ORDER BY hits DESC",
            (site,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_invalid_targets(self, site: str, window_hours: int = 24) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=window_hours)
        ).isoformat()
        row = self._db.conn.execute(
            "SELECT COUNT(*) as cnt FROM scrape_runs "
            "WHERE site = ? AND outcome = 'invalid_target' AND scraped_at > ?",
            (site, cutoff),
        ).fetchone()
        return row["cnt"] if row else 0

    def count_total_runs(self, site: str, window_hours: int = 24) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=window_hours)
        ).isoformat()
        row = self._db.conn.execute(
            "SELECT COUNT(*) as cnt FROM scrape_runs WHERE site = ? AND scraped_at > ?",
            (site, cutoff),
        ).fetchone()
        return row["cnt"] if row else 0

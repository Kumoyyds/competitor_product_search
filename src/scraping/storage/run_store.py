from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .database import ScrapeDB


class RunStore:
    def __init__(self, db: ScrapeDB, dedup_window_seconds: int = 3600):
        self._db = db
        self._dedup_window = dedup_window_seconds

    def is_duplicate(self, url: str) -> bool:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self._dedup_window)
        ).isoformat()
        row = self._db.conn.execute(
            "SELECT 1 FROM scrape_runs "
            "WHERE url = ? AND outcome = 'success' AND scraped_at > ? LIMIT 1",
            (url, cutoff),
        ).fetchone()
        return row is not None

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
        model_used: Optional[str] = None,
        latency_ms: Optional[int] = None,
        cost: Optional[float] = None,
        signature: Optional[str] = None,
        error: Optional[str] = None,
    ) -> int:
        cur = self._db.conn.execute(
            "INSERT INTO scrape_runs "
            "(url, host, site, scraper, outcome, path, winning_parser_id, attempts, "
            "model_used, latency_ms, cost, signature, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                url, host, site, scraper, outcome, path, winning_parser_id,
                attempts, model_used, latency_ms, cost, signature, error,
            ),
        )
        self._db.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

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

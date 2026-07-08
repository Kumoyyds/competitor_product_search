from __future__ import annotations

import json
from typing import Any, Optional

from .database import ScrapeDB


class GoldenStore:
    def __init__(self, db: ScrapeDB):
        self._db = db

    def seed(
        self,
        site: str,
        page_type: str,
        html_snapshot: str,
        expected_output: dict[str, Any],
    ) -> int:
        cur = self._db.conn.execute(
            "INSERT INTO golden_samples (site, page_type, html_snapshot, expected_output) "
            "VALUES (?, ?, ?, ?)",
            (site, page_type, html_snapshot, json.dumps(expected_output, default=str)),
        )
        self._db.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_by_site_and_type(
        self, site: str, page_type: str, exclude_stale: bool = True
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM golden_samples WHERE site = ? AND page_type = ?"
        params: list[Any] = [site, page_type]
        if exclude_stale:
            query += " AND is_stale = 0"
        rows = self._db.conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["expected_output"] = json.loads(d["expected_output"])
            result.append(d)
        return result

    def mark_stale(self, sample_id: int) -> None:
        self._db.conn.execute(
            "UPDATE golden_samples SET is_stale = 1 WHERE id = ?", (sample_id,)
        )
        self._db.conn.commit()

    def get_page_type_coverage(self, site: str) -> dict[str, int]:
        rows = self._db.conn.execute(
            "SELECT page_type, COUNT(*) as cnt FROM golden_samples "
            "WHERE site = ? AND is_stale = 0 GROUP BY page_type",
            (site,),
        ).fetchall()
        return {r["page_type"]: r["cnt"] for r in rows}

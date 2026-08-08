from __future__ import annotations

import json
from typing import Any

from ..models.product_data import ProductData
from .database import ScrapeDB


class ResultStore:
    def __init__(self, db: ScrapeDB):
        self._db = db

    def append(self, product: ProductData) -> int:
        """Append-only write for qualified results (D24)."""
        cur = self._db.conn.execute(
            "INSERT INTO results (url, site, scraped_at, product_data) VALUES (?, ?, ?, ?)",
            (
                product.url,
                product.website,
                product.scraped_at.isoformat(),
                product.model_dump_json(),
            ),
        )
        self._db.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_by_url(self, url: str) -> list[dict[str, Any]]:
        rows = self._db.conn.execute(
            "SELECT * FROM results WHERE url = ? ORDER BY scraped_at DESC", (url,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["product_data"] = json.loads(d["product_data"])
            result.append(d)
        return result

    def get_by_site(self, site: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.conn.execute(
            "SELECT * FROM results WHERE site = ? ORDER BY scraped_at DESC LIMIT ?",
            (site, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["product_data"] = json.loads(d["product_data"])
            result.append(d)
        return result

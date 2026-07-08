from __future__ import annotations

from typing import Any, Optional

from .database import ScrapeDB


class ParserStore:
    def __init__(self, db: ScrapeDB):
        self._db = db

    def create(
        self,
        site: str,
        version: str,
        code: str,
        created_by: str = "initial",
        page_type_scope: Optional[str] = None,
    ) -> int:
        cur = self._db.conn.execute(
            "INSERT INTO parsers (site, version, code, created_by, page_type_scope) "
            "VALUES (?, ?, ?, ?, ?)",
            (site, version, code, created_by, page_type_scope),
        )
        self._db.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_active(self, site: str) -> list[dict[str, Any]]:
        rows = self._db.conn.execute(
            "SELECT * FROM parsers WHERE site = ? AND status = 'active' ORDER BY id DESC",
            (site,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_ordered_by_hits(self, site: str) -> list[dict[str, Any]]:
        """Return active parsers sorted by hit rate DESC, then id DESC (fresh-first tiebreak).

        Freshly promoted parsers (0 hits) sort ahead of older 0-hit parsers via id DESC,
        which reconciles spec's "命中率降序" with "promote 置于最前" (§5.4).
        """
        rows = self._db.conn.execute(
            """
            SELECT p.*, COALESCE(r.hits, 0) AS hits
            FROM parsers p
            LEFT JOIN (
                SELECT winning_parser_id, COUNT(*) AS hits
                FROM scrape_runs
                WHERE site = ? AND outcome = 'success' AND winning_parser_id IS NOT NULL
                GROUP BY winning_parser_id
            ) r ON r.winning_parser_id = p.id
            WHERE p.site = ? AND p.status = 'active'
            ORDER BY hits DESC, p.id DESC
            """,
            (site, site),
        ).fetchall()
        return [dict(r) for r in rows]

    def retire(self, parser_id: int) -> None:
        self._db.conn.execute(
            "UPDATE parsers SET status = 'retired' WHERE id = ?",
            (parser_id,),
        )
        self._db.conn.commit()

    def get_by_id(self, parser_id: int) -> Optional[dict[str, Any]]:
        row = self._db.conn.execute(
            "SELECT * FROM parsers WHERE id = ?", (parser_id,)
        ).fetchone()
        return dict(row) if row else None

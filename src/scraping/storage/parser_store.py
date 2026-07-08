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

from __future__ import annotations

from .database import ScrapeDB


class PhraseStore:
    def __init__(self, db: ScrapeDB):
        self._db = db

    def add(self, site: str, phrase: str, source: str = "agent_backfill") -> int:
        cur = self._db.conn.execute(
            "INSERT INTO invalid_target_phrases (site, phrase, source) VALUES (?, ?, ?)",
            (site, phrase, source),
        )
        self._db.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_phrases(self, site: str) -> list[str]:
        rows = self._db.conn.execute(
            "SELECT phrase FROM invalid_target_phrases WHERE site = ?", (site,)
        ).fetchall()
        return [r["phrase"] for r in rows]

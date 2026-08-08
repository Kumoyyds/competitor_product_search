from __future__ import annotations

import json
from typing import Any, Optional

from .database import ScrapeDB


class EscalationStore:
    def __init__(self, db: ScrapeDB):
        self._db = db

    def upsert(
        self,
        signature: str,
        reason: str,
        snapshot: Optional[dict[str, Any]] = None,
    ) -> int:
        """Signature-deduplicated escalation (D24).
        Same signature -> affected_count += 1, no new row.
        """
        existing = self._db.conn.execute(
            "SELECT id, affected_count FROM escalations WHERE signature = ?",
            (signature,),
        ).fetchone()

        if existing:
            self._db.conn.execute(
                "UPDATE escalations SET affected_count = ? WHERE id = ?",
                (existing["affected_count"] + 1, existing["id"]),
            )
            self._db.conn.commit()
            return existing["id"]

        snapshot_json = json.dumps(snapshot, default=str) if snapshot else None
        cur = self._db.conn.execute(
            "INSERT INTO escalations (signature, reason, snapshot) VALUES (?, ?, ?)",
            (signature, reason, snapshot_json),
        )
        self._db.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_open(self) -> list[dict[str, Any]]:
        rows = self._db.conn.execute(
            "SELECT * FROM escalations WHERE status = 'open' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve(self, escalation_id: int) -> None:
        self._db.conn.execute(
            "UPDATE escalations SET status = 'resolved' WHERE id = ?",
            (escalation_id,),
        )
        self._db.conn.commit()

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading

from . import config
from .models import BaseAttributes


class BaseExtractionCache:
    """Process-wide SQLite cache for base attribute extraction.

    Keyed on md5(title). Values are JSON blobs of {"brand": ..., "numerics": {...}}.
    Synchronous SQLite calls are cheap; the cache is exposed via sync .get/.set
    and base_match wraps them with asyncio.to_thread when needed.
    """

    def __init__(self, path: str | None = None):
        self._path = path or config.get("cache", "sqlite_path", default=".cache/base_extraction.sqlite")
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS base_cache ("
                "  hash TEXT PRIMARY KEY,"
                "  payload TEXT NOT NULL"
                ")"
            )

    @staticmethod
    def _key(title: str) -> str:
        return hashlib.md5(title.encode("utf-8")).hexdigest()

    def get(self, title: str) -> BaseAttributes | None:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT payload FROM base_cache WHERE hash = ?", (self._key(title),)
            )
            row = cur.fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        if "brands" not in data:
            return None
        brands_raw = data.get("brands") or []
        return BaseAttributes(
            brands=[str(b) for b in brands_raw if b],
            numerics={k: float(v) for k, v in (data.get("numerics") or {}).items()},
        )

    def set(self, title: str, attrs: BaseAttributes) -> None:
        payload = json.dumps({"brands": list(attrs.brands), "numerics": attrs.numerics})
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO base_cache (hash, payload) VALUES (?, ?)",
                (self._key(title), payload),
            )


_singleton: BaseExtractionCache | None = None


def get_cache() -> BaseExtractionCache:
    global _singleton
    if _singleton is None:
        _singleton = BaseExtractionCache()
    return _singleton

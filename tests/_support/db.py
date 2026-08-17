from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from src.scraping.storage import ScrapeDB
from src.search.db import SearchDB


@contextmanager
def temp_scrape_db() -> Iterator[tuple[Path, ScrapeDB]]:
    with tempfile.TemporaryDirectory(prefix="pricescope_scrape_test_") as tmp:
        path = Path(tmp) / "scraping.db"
        db = ScrapeDB(path)
        db.init_db()
        try:
            yield path, db
        finally:
            db.close()


@contextmanager
def temp_search_db() -> Iterator[tuple[Path, SearchDB]]:
    with tempfile.TemporaryDirectory(prefix="pricescope_search_test_") as tmp:
        path = Path(tmp) / "search.db"
        yield path, SearchDB(str(path))


def fetchall(path: str | Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()

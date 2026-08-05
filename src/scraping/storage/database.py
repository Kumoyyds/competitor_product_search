from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

_DDL = """
CREATE TABLE IF NOT EXISTS parsers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    version TEXT NOT NULL,
    code TEXT NOT NULL,
    page_type_scope TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'retired')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by TEXT NOT NULL DEFAULT 'initial' CHECK(created_by IN ('initial', 'agent'))
);

CREATE TABLE IF NOT EXISTS golden_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    page_type TEXT NOT NULL CHECK(page_type IN ('standard', 'out_of_stock', 'discounted', 'multipack', 'membership')),
    html_snapshot TEXT NOT NULL,
    expected_output TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    is_stale INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT 'auto' CHECK(created_by IN ('coldstart', 'auto'))
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    host TEXT NOT NULL,
    site TEXT NOT NULL,
    scraper TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'escalated', 'invalid_target')),
    path TEXT NOT NULL CHECK(path IN ('fast', 'retried', 'agent_repaired', 'backup_1', 'backup_2', 'escalated', 'invalid_target')),
    winning_parser_id INTEGER REFERENCES parsers(id),
    attempts INTEGER NOT NULL DEFAULT 1,
    model_used TEXT,
    latency_ms INTEGER,
    cost REAL
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    site TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    product_data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL CHECK(reason IN ('parser_broken', 'infra_failure', 'api_malformed', 'mass_invalid_target')),
    affected_count INTEGER NOT NULL DEFAULT 1,
    snapshot TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS invalid_target_phrases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    phrase TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent_backfill',
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_url_scraped ON scrape_runs(url, scraped_at);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_site ON scrape_runs(site);
CREATE INDEX IF NOT EXISTS idx_results_url ON results(url);
CREATE INDEX IF NOT EXISTS idx_results_site_scraped ON results(site, scraped_at);
CREATE INDEX IF NOT EXISTS idx_parsers_site_status ON parsers(site, status);
CREATE INDEX IF NOT EXISTS idx_golden_site_page ON golden_samples(site, page_type, is_stale);
CREATE INDEX IF NOT EXISTS idx_phrases_site ON invalid_target_phrases(site);
"""

# Incremental golden_samples columns. New databases get these from _DDL;
# init_db() adds them to historical databases automatically.
_GOLDEN_ADDED_COLUMNS = {
    "created_by": (
        "TEXT NOT NULL DEFAULT 'auto' "
        "CHECK(created_by IN ('coldstart', 'auto'))"
    ),
}


class ScrapeDB:
    """SQLite connection manager for the scraping module's 6 tables."""

    def __init__(self, db_path: str | Path = "scraping.db"):
        self._db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def init_db(self) -> None:
        self.conn.executescript(_DDL)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Idempotently add incremental columns missing from historical DBs."""
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(golden_samples)")
        }
        if _GOLDEN_ADDED_COLUMNS.keys() <= existing:
            return

        try:
            # Serialize the inspect-and-alter sequence so concurrent first starts
            # cannot both observe the same missing column.
            self.conn.execute("BEGIN IMMEDIATE")
            locked_existing = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(golden_samples)")
            }
            for column, ddl in _GOLDEN_ADDED_COLUMNS.items():
                if column not in locked_existing:
                    self.conn.execute(
                        f"ALTER TABLE golden_samples ADD COLUMN {column} {ddl}"
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def clear_site(self, site: str) -> dict[str, int]:
        """Hard-delete *site*'s rows from ``parsers`` and ``golden_samples``.

        Before the parser delete, ``scrape_runs.winning_parser_id`` is set
        to NULL for the site so the foreign-key constraint does not block
        the ``DELETE``.  Run-history rows are kept; the pointer is dropped.

        No other tables are touched.  No schema changes — only
        ``DELETE`` and ``UPDATE … SET … = NULL`` inside one transaction.
        """
        counts: dict[str, int] = {}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "UPDATE scrape_runs SET winning_parser_id = NULL "
                "WHERE site = ? AND winning_parser_id IS NOT NULL",
                (site,),
            )
            counts["scrape_runs_detached"] = cur.rowcount
            cur = self.conn.execute(
                "DELETE FROM parsers WHERE site = ?", (site,)
            )
            counts["parsers"] = cur.rowcount
            cur = self.conn.execute(
                "DELETE FROM golden_samples WHERE site = ?", (site,)
            )
            counts["golden_samples"] = cur.rowcount
            self.conn.commit()
            return counts
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> ScrapeDB:
        self.init_db()
        return self

    def __exit__(self, *args) -> None:
        self.close()

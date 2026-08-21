from __future__ import annotations

from collections.abc import Sequence
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

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL CHECK(reason IN ('parser_broken', 'infra_failure', 'api_malformed', 'mass_invalid_target')),
    affected_count INTEGER NOT NULL DEFAULT 1,
    snapshot TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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
    repair_model TEXT,
    latency_ms INTEGER,
    signature TEXT,
    error TEXT,
    escalation_id INTEGER REFERENCES escalations(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    site TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    product_data TEXT NOT NULL,
    run_id INTEGER REFERENCES scrape_runs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS invalid_target_phrases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    phrase TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent_backfill',
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

"""

_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_scrape_runs_url_scraped ON scrape_runs(url, scraped_at);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_site ON scrape_runs(site);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_site_outcome ON scrape_runs(site, outcome, scraped_at);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_escalation ON scrape_runs(escalation_id);
CREATE INDEX IF NOT EXISTS idx_results_url ON results(url);
CREATE INDEX IF NOT EXISTS idx_results_site_scraped ON results(site, scraped_at);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_parsers_site_status ON parsers(site, status);
CREATE INDEX IF NOT EXISTS idx_golden_site_page ON golden_samples(site, page_type, is_stale);
CREATE INDEX IF NOT EXISTS idx_phrases_site ON invalid_target_phrases(site);
"""

# Incremental columns. New databases get these from _DDL; init_db() adds them
# to historical databases automatically.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "golden_samples": {
        "created_by": (
            "TEXT NOT NULL DEFAULT 'auto' "
            "CHECK(created_by IN ('coldstart', 'auto'))"
        ),
    },
    "scrape_runs": {
        "signature": "TEXT",
        "error": "TEXT",
        "repair_model": "TEXT",
        "escalation_id": (
            "INTEGER REFERENCES escalations(id) ON DELETE SET NULL"
        ),
    },
    "results": {
        "run_id": "INTEGER REFERENCES scrape_runs(id) ON DELETE SET NULL",
    },
}

_RENAMED_COLUMNS: dict[str, dict[str, str]] = {
    "scrape_runs": {"model_used": "repair_model"},
}

_REMOVED_COLUMNS: dict[str, tuple[str, ...]] = {
    "scrape_runs": ("cost",),
}

CLEARABLE_TABLES = (
    "parsers",
    "golden_samples",
    "results",
    "escalations",
    "invalid_target_phrases",
    "scrape_runs",
)
DEFAULT_CLEAR_TABLES = ("parsers", "golden_samples")


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
        self.conn.executescript(_INDEX_DDL)

    def _ensure_columns(self) -> None:
        """Idempotently apply additive, rename, and removal migrations."""
        migration_tables = (
            set(_ADDED_COLUMNS) | set(_RENAMED_COLUMNS) | set(_REMOVED_COLUMNS)
        )
        existing_by_table = {
            table: {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for table in migration_tables
        }
        additions_done = all(
            columns.keys() <= existing_by_table[table]
            for table, columns in _ADDED_COLUMNS.items()
        )
        renames_done = all(
            old not in existing_by_table[table]
            and new in existing_by_table[table]
            for table, renames in _RENAMED_COLUMNS.items()
            for old, new in renames.items()
        )
        removals_done = all(
            column not in existing_by_table[table]
            for table, columns in _REMOVED_COLUMNS.items()
            for column in columns
        )
        if additions_done and renames_done and removals_done:
            return

        try:
            # Serialize the inspect-and-alter sequence so concurrent first starts
            # cannot both observe the same missing column.
            self.conn.execute("BEGIN IMMEDIATE")

            for table, renames in _RENAMED_COLUMNS.items():
                locked_existing = {
                    row["name"]
                    for row in self.conn.execute(f"PRAGMA table_info({table})")
                }
                for old, new in renames.items():
                    if old in locked_existing and new not in locked_existing:
                        self.conn.execute(
                            f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"
                        )
                        locked_existing.remove(old)
                        locked_existing.add(new)
                    elif old in locked_existing and new in locked_existing:
                        self.conn.execute(
                            f"UPDATE {table} SET {new} = COALESCE({new}, {old})"
                        )
                        self.conn.execute(
                            f"ALTER TABLE {table} DROP COLUMN {old}"
                        )
                        locked_existing.remove(old)

            for table, columns in _ADDED_COLUMNS.items():
                locked_existing = {
                    row["name"]
                    for row in self.conn.execute(f"PRAGMA table_info({table})")
                }
                for column, ddl in columns.items():
                    if column not in locked_existing:
                        self.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                        )

            for table, columns in _REMOVED_COLUMNS.items():
                locked_existing = {
                    row["name"]
                    for row in self.conn.execute(f"PRAGMA table_info({table})")
                }
                for column in columns:
                    if column in locked_existing:
                        self.conn.execute(
                            f"ALTER TABLE {table} DROP COLUMN {column}"
                        )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def clear_site(
        self,
        site: str,
        tables: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Hard-delete selected rows for *site* in one transaction.

        Omitting ``tables`` preserves the historical behavior: parsers and
        golden samples are deleted, while run-history rows are retained and
        any parser references on those rows are detached first.
        """
        if tables is None:
            selected = DEFAULT_CLEAR_TABLES
        elif isinstance(tables, str):
            selected = (tables,)
        else:
            selected = tuple(tables)
        unknown = [table for table in selected if table not in CLEARABLE_TABLES]
        if unknown:
            valid = ", ".join(CLEARABLE_TABLES)
            raise ValueError(
                f"Unknown clearable table(s): {unknown!r}. Valid tables: {valid}"
            )
        if not selected:
            return {}

        # Avoid executing the same DELETE twice if a caller repeats a table.
        selected = tuple(dict.fromkeys(selected))
        selected_set = set(selected)
        counts: dict[str, int] = {}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if "scrape_runs" in selected_set:
                cur = self.conn.execute(
                    "UPDATE results SET run_id = NULL "
                    "WHERE run_id IN "
                    "(SELECT id FROM scrape_runs WHERE site = ?)",
                    (site,),
                )
                counts["results_detached"] = cur.rowcount
                cur = self.conn.execute(
                    "DELETE FROM scrape_runs WHERE site = ?", (site,)
                )
                counts["scrape_runs"] = cur.rowcount

            if "parsers" in selected_set:
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

            for table in (
                "golden_samples",
                "results",
                "invalid_target_phrases",
            ):
                if table in selected_set:
                    cur = self.conn.execute(
                        f"DELETE FROM {table} WHERE site = ?", (site,)
                    )
                    counts[table] = cur.rowcount

            if "escalations" in selected_set:
                cur = self.conn.execute(
                    "UPDATE scrape_runs SET escalation_id = NULL "
                    "WHERE escalation_id IN ("
                    "SELECT id FROM escalations "
                    "WHERE substr(signature, 1, "
                    "instr(signature || '|', '|') - 1) = ?)",
                    (site,),
                )
                counts["scrape_runs_escalation_detached"] = cur.rowcount
                cur = self.conn.execute(
                    "DELETE FROM escalations "
                    "WHERE substr(signature, 1, "
                    "instr(signature || '|', '|') - 1) = ?",
                    (site,),
                )
                counts["escalations"] = cur.rowcount

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

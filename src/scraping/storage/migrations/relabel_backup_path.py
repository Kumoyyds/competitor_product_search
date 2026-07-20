"""One-time migration: replace 'fallback_scraper' with 'backup_1','backup_2' in the
scrape_runs.path CHECK constraint.

SQLite cannot ALTER a CHECK constraint in place, so we create a new table with the
updated CHECK, copy all rows, drop the old table, and rename.

Run once on deploy (idempotent — if 'backup_1' already in CHECK, does nothing):
    python -m src.scraping.storage.migrations.relabel_backup_path [db_path]

Default db_path: scraping.db (repo root).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def migration_needed(db_path: str) -> bool:
    """Return True if the scrape_runs CHECK constraint still contains 'fallback_scraper'."""
    conn = sqlite3.connect(db_path)
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='scrape_runs'"
        ).fetchone()
        if ddl is None:
            # Table doesn't exist yet — nothing to migrate
            conn.close()
            return False
        has_backup = "backup_1" in ddl[0]
        conn.close()
        return not has_backup
    except Exception:
        conn.close()
        return False


def run_migration(db_path: str) -> None:
    """Replace 'fallback_scraper' CHECK with 'backup_1','backup_2' via table recreation."""
    conn = sqlite3.connect(db_path)

    # 1. Create new table with updated CHECK
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scrape_runs_new (
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
    """)

    # 2. Copy rows from old table
    conn.execute(
        "INSERT INTO scrape_runs_new SELECT * FROM scrape_runs"
    )

    # 3. Drop old table
    conn.execute("DROP TABLE scrape_runs")

    # 4. Rename new table
    conn.execute("ALTER TABLE scrape_runs_new RENAME TO scrape_runs")

    # 5. Recreate indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrape_runs_url_scraped ON scrape_runs(url, scraped_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scrape_runs_site ON scrape_runs(site)"
    )

    conn.commit()
    conn.close()
    print(f"Migration complete: updated scrape_runs.path CHECK. db={db_path}")


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "scraping.db"
    if not Path(db_path).exists():
        print(f"Database not found at {db_path} — nothing to migrate.")
        return
    if not migration_needed(db_path):
        print(f"Migration not needed (backup_1 already in CHECK, or scrape_runs table does not exist). db={db_path}")
        return
    run_migration(db_path)


if __name__ == "__main__":
    main()

"""One-time migration: add 'membership' to the golden_samples.page_type CHECK constraint.

SQLite cannot ALTER a CHECK constraint in place, so we create a new table with the
expanded CHECK, copy all rows, drop the old table, and rename.

Run once on deploy (idempotent — if 'membership' already in CHECK, does nothing):
    python -m src.scraping.storage.migrations.add_membership_bucket [db_path]

Default db_path: scraping.db (repo root).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def migration_needed(db_path: str) -> bool:
    """Return True if the golden_samples CHECK constraint lacks 'membership'."""
    conn = sqlite3.connect(db_path)
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='golden_samples'"
        ).fetchone()
        if ddl is None:
            # Table doesn't exist yet — nothing to migrate
            conn.close()
            return False
        has_membership = "membership" in ddl[0]
        conn.close()
        return not has_membership
    except Exception:
        conn.close()
        return False


def run_migration(db_path: str) -> None:
    """Add 'membership' to golden_samples CHECK constraint via table recreation."""
    conn = sqlite3.connect(db_path)

    # 1. Create new table with expanded CHECK
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS golden_samples_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site TEXT NOT NULL,
            page_type TEXT NOT NULL CHECK(page_type IN ('standard', 'out_of_stock', 'discounted', 'multipack', 'membership')),
            html_snapshot TEXT NOT NULL,
            expected_output TEXT NOT NULL,
            captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            is_stale INTEGER NOT NULL DEFAULT 0
        );
    """)

    # 2. Copy rows from old table
    conn.execute(
        "INSERT INTO golden_samples_new SELECT * FROM golden_samples"
    )

    # 3. Drop old table
    conn.execute("DROP TABLE golden_samples")

    # 4. Rename new table
    conn.execute("ALTER TABLE golden_samples_new RENAME TO golden_samples")

    # 5. Recreate index
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_golden_site_page ON golden_samples(site, page_type, is_stale)"
    )

    conn.commit()
    conn.close()
    print(f"Migration complete: added 'membership' to golden_samples.page_type CHECK. db={db_path}")


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "scraping.db"
    if not Path(db_path).exists():
        print(f"Database not found at {db_path} — nothing to migrate.")
        return
    if not migration_needed(db_path):
        print(f"Migration not needed (or golden_samples table does not exist). db={db_path}")
        return
    run_migration(db_path)


if __name__ == "__main__":
    main()

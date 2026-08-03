"""Add provenance to golden_samples for deterministic, human-safe pruning.

Existing rows are backfilled to ``auto`` because their historical origin cannot
be determined. Safe to run repeatedly.

Run:
    python -m src.scraping.storage.migrations.add_golden_created_by [db_path]
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def migration_needed(db_path: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='golden_samples'"
        ).fetchone()
        if table is None:
            return False
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(golden_samples)").fetchall()
        }
        return "created_by" not in columns
    finally:
        conn.close()


def run_migration(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "ALTER TABLE golden_samples ADD COLUMN created_by TEXT NOT NULL "
            "DEFAULT 'auto' CHECK(created_by IN ('coldstart', 'auto'))"
        )
        conn.commit()
    finally:
        conn.close()
    print(
        "Migration complete: added golden_samples.created_by; "
        f"existing rows were classified as 'auto'. db={db_path}"
    )


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "scraping.db"
    if not Path(db_path).exists():
        print(f"Database not found at {db_path} — nothing to migrate.")
        return 0
    if not migration_needed(db_path):
        print(
            "Migration not needed (or golden_samples table does not exist). "
            f"db={db_path}"
        )
        return 0
    run_migration(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

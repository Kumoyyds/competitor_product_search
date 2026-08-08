"""One-time migration: mark all existing golden_samples stale so that
new parsers (which now extract membership_price via SCHEMA_HINT)
aren't falsely rejected during promotion against old goldens that
lack the field.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[3] / "scraping.db"


def main() -> int:
    if not DB_PATH.exists():
        print(f"No scraping.db at {DB_PATH} — nothing to migrate")
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    if "golden_samples" not in tables:
        print("golden_samples table not present — nothing to migrate")
        conn.close()
        return 0

    before = conn.execute("SELECT COUNT(*) FROM golden_samples WHERE is_stale = 0").fetchone()[0]
    if before == 0:
        print(f"No active goldens found — nothing to migrate (DB at {DB_PATH})")
        conn.close()
        return 0

    conn.execute("UPDATE golden_samples SET is_stale = 1 WHERE is_stale = 0")
    conn.commit()
    print(f"Marked {before} golden sample(s) as stale so new parsers with membership_price won't mismatch them at promotion.")
    print(f"Next successful scrape will re-seed fresh goldens that capture membership_price.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

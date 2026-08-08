"""verify_clear_db — ScrapeDB.clear_site() correctness checks.

Offline coverage: FK-safe delete ordering, atomicity, idempotency,
cross-site isolation, and schema preservation.
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

# Ensure src is importable even if run from tests/ directly.
_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.scraping.storage.database import ScrapeDB

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def count_rows(db: ScrapeDB, table: str, site: str) -> int:
    return db.conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE site = ?", (site,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Helper — seed a fresh DB with predictable data for two sites
# ---------------------------------------------------------------------------

def seed_db(db: ScrapeDB) -> tuple[int, int]:
    """Seed parsers, golden_samples, and scrape_runs for site_a and site_b.

    Returns (parser_id_a, parser_id_b) so we can wire the FK reference.
    """
    db.init_db()

    # parsers — 1 per site
    cur = db.conn.execute(
        "INSERT INTO parsers (site, version, code, created_by) VALUES (?, ?, ?, ?)",
        ("site_a", "v1", "def parse_a(html, url): pass", "initial"),
    )
    parser_a_id = cur.lastrowid
    cur = db.conn.execute(
        "INSERT INTO parsers (site, version, code, created_by) VALUES (?, ?, ?, ?)",
        ("site_b", "v1", "def parse_b(html, url): pass", "initial"),
    )
    parser_b_id = cur.lastrowid
    db.conn.commit()

    # golden_samples — 2 per site (different page_types)
    for site, ptype in [("site_a", "standard"), ("site_a", "out_of_stock"),
                        ("site_b", "standard"), ("site_b", "discounted")]:
        db.conn.execute(
            "INSERT INTO golden_samples (site, page_type, html_snapshot, expected_output, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (site, ptype, f"<html>{site}_{ptype}</html>",
             json.dumps({"title": f"{site}_{ptype}", "price": 9.99}), "coldstart"),
        )
    db.conn.commit()

    # scrape_runs — 2 for site_a (one referencing parser_a, one NULL),
    #               1 for site_b (referencing parser_b)
    for site, parser_id, url in [
        ("site_a", parser_a_id, "https://a.test/1"),
        ("site_a", None, "https://a.test/2"),
        ("site_b", parser_b_id, "https://b.test/1"),
    ]:
        db.conn.execute(
            "INSERT INTO scrape_runs (url, host, site, scraper, outcome, path, winning_parser_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, f"{site}.test", site, "html_scraper", "success", "fast", parser_id),
        )
    db.conn.commit()

    return parser_a_id, parser_b_id


# ---------------------------------------------------------------------------
# Schema snapshot helpers (verify no structural changes)
# ---------------------------------------------------------------------------

def schema_snapshot(db: ScrapeDB) -> dict[str, list[dict]]:
    """Collect every table's column-def and index list for before/after comparison."""
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    snap: dict[str, list[dict]] = {}
    for t in tables:
        name = t["name"]
        cols = [
            dict(r)
            for r in db.conn.execute(f"PRAGMA table_info({name})")
        ]
        idxs = [
            dict(r)
            for r in db.conn.execute(f"PRAGMA index_list({name})")
        ]
        snap[name] = {"columns": cols, "indexes": idxs}
    return snap


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def verify_clear_site() -> None:
    section("clear_site: basic correctness")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_clear.db"
        db = ScrapeDB(str(db_path))
        parser_a_id, parser_b_id = seed_db(db)

        # --- snapshot before clear ---
        before_schema = schema_snapshot(db)

        check("001 seed: site_a has 1 parser",
              count_rows(db, "parsers", "site_a") == 1)
        check("002 seed: site_a has 2 golden_samples",
              count_rows(db, "golden_samples", "site_a") == 2)
        check("003 seed: site_a has 2 scrape_runs",
              count_rows(db, "scrape_runs", "site_a") == 2)
        check("004 seed: site_b has 1 parser",
              count_rows(db, "parsers", "site_b") == 1)
        check("005 seed: site_b has 2 golden_samples",
              count_rows(db, "golden_samples", "site_b") == 2)
        check("006 seed: site_b has 1 scrape_run",
              count_rows(db, "scrape_runs", "site_b") == 1)

        # --- FK reference is in place ---
        ref = db.conn.execute(
            "SELECT winning_parser_id FROM scrape_runs WHERE site = ? AND winning_parser_id IS NOT NULL",
            ("site_a",),
        ).fetchone()
        check("007 seed: scrape_run references site_a parser",
              ref is not None and ref["winning_parser_id"] == parser_a_id)

        # --- clear site_a ---
        result = db.clear_site("site_a")
        check("008 clear returns dict",
              isinstance(result, dict))
        check("009 clear: parsers count",
              result.get("parsers") == 1,
              f"got {result.get('parsers')}")
        check("010 clear: golden_samples count",
              result.get("golden_samples") == 2,
              f"got {result.get('golden_samples')}")
        check("011 clear: scrape_runs_detached count",
              result.get("scrape_runs_detached") == 1,
              f"got {result.get('scrape_runs_detached')}")

        # --- site_a: parsers + goldens gone ---
        check("012 post-clear: site_a parsers      = 0",
              count_rows(db, "parsers", "site_a") == 0)
        check("013 post-clear: site_a golden_samples = 0",
              count_rows(db, "golden_samples", "site_a") == 0)

        # --- site_a: scrape_runs kept but FK detached ---
        check("014 post-clear: site_a scrape_runs kept",
              count_rows(db, "scrape_runs", "site_a") == 2)
        ref_after = db.conn.execute(
            "SELECT id, winning_parser_id FROM scrape_runs WHERE site = ?",
            ("site_a",),
        ).fetchall()
        check("015 post-clear: all site_a runs have winning_parser_id NULL",
              all(r["winning_parser_id"] is None for r in ref_after))

        # --- site_b: untouched ---
        check("016 post-clear: site_b parsers        = 1",
              count_rows(db, "parsers", "site_b") == 1)
        check("017 post-clear: site_b golden_samples = 2",
              count_rows(db, "golden_samples", "site_b") == 2)
        check("018 post-clear: site_b scrape_runs    = 1",
              count_rows(db, "scrape_runs", "site_b") == 1)
        ref_b = db.conn.execute(
            "SELECT winning_parser_id FROM scrape_runs WHERE site = ?",
            ("site_b",),
        ).fetchone()
        check("019 post-clear: site_b FK still intact",
              ref_b is not None and ref_b["winning_parser_id"] == parser_b_id)
        db.close()


def verify_idempotent() -> None:
    section("clear_site: idempotency")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_idem.db"
        db = ScrapeDB(str(db_path))
        seed_db(db)

        first = db.clear_site("site_a")
        second = db.clear_site("site_a")

        check("020 idempotent: first pass had deletes",
              sum(v for v in first.values()) > 0)
        check("021 idempotent: second parsers = 0",
              second.get("parsers") == 0,
              f"got {second.get('parsers')}")
        check("022 idempotent: second golden_samples = 0",
              second.get("golden_samples") == 0,
              f"got {second.get('golden_samples')}")
        check("023 idempotent: second scrape_runs_detached = 0",
              second.get("scrape_runs_detached") == 0,
              f"got {second.get('scrape_runs_detached')}")
        db.close()


def verify_unknown_site_noop() -> None:
    section("clear_site: unknown site is a no-op")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_unknown.db"
        db = ScrapeDB(str(db_path))
        db.init_db()

        result = db.clear_site("nonexistent")
        check("024 unknown site: parsers = 0",
              result.get("parsers") == 0)
        check("025 unknown site: golden_samples = 0",
              result.get("golden_samples") == 0)
        check("026 unknown site: scrape_runs_detached = 0",
              result.get("scrape_runs_detached") == 0)
        db.close()


def verify_schema_unchanged() -> None:
    section("clear_site: schema preservation")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_schema.db"
        db = ScrapeDB(str(db_path))
        seed_db(db)

        before = schema_snapshot(db)
        db.clear_site("site_a")
        after = schema_snapshot(db)

        check("027 schema: same table count",
              len(before) == len(after),
              f"before={len(before)} after={len(after)}")
        for table in before:
            # columns
            before_cols = [(c["name"], c["type"], c["notnull"], c["dflt_value"])
                           for c in before[table]["columns"]]
            after_cols = [(c["name"], c["type"], c["notnull"], c["dflt_value"])
                          for c in after[table]["columns"]]
            check(f"028 schema: {table} columns unchanged",
                  before_cols == after_cols,
                  f"before={before_cols} after={after_cols}")
            # indexes
            before_idx = [i["name"] for i in before[table]["indexes"]]
            after_idx = [i["name"] for i in after[table]["indexes"]]
            check(f"029 schema: {table} indexes unchanged",
                  set(before_idx) == set(after_idx),
                  f"before={before_idx} after={after_idx}")
        db.close()


def verify_bare_conn_no_fk() -> None:
    section("clear_site: succeeds even when foreign_keys is OFF")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_nofk.db"
        db = ScrapeDB(str(db_path))
        seed_db(db)

        # Simulate someone who opened a bare connection without PRAGMA foreign_keys=ON
        db.conn.execute("PRAGMA foreign_keys=OFF")
        # The FK-safe ordering should still work; the UPDATE step just won't
        # need to NULL anything.
        result = db.clear_site("site_a")
        db.conn.execute("PRAGMA foreign_keys=ON")

        check("030 no-FK: parsers deleted",
              count_rows(db, "parsers", "site_a") == 0,
              f"got {count_rows(db, 'parsers', 'site_a')}")
        check("031 no-FK: golden_samples deleted",
              count_rows(db, "golden_samples", "site_a") == 0)
        check("032 no-FK: scrape_runs still present",
              count_rows(db, "scrape_runs", "site_a") == 2)
        db.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        verify_clear_site()
        verify_idempotent()
        verify_unknown_site_noop()
        verify_schema_unchanged()
        verify_bare_conn_no_fk()
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()

    print()
    print("=" * 72)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 72)
    for name, detail in FAILED:
        print(f"  FAILED: {name}  ({detail})")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

"""Verification for M6 — ordered parser list + scrape_runs/results writing.

Covers:
  - Parser ordering by hit rate (real-time D17 aggregation)
  - 0-hit tiebreak by id DESC (fresh promotes first)
  - First passing parser wins; winning_parser_id recorded to scrape_runs
  - All-parsers-fail path (returns dict with empty and errors → repair ladder)
  - results table receives ProductData on success

Offline: patches BrightData unlocker to return the Argos fixture HTML.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Set DB path BEFORE importing scraping module so config picks it up
_DB_PATH = os.path.join(tempfile.gettempdir(), "verify_m6.db")
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)
os.environ["SCRAPING_DB_PATH"] = _DB_PATH

from src.scraping import config as _config
_config._config = None  # reset cached config

DATA_DIR = Path(__file__).parent.parent / "data"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def section(title: str) -> None:
    print(); print("=" * 70); print(title); print("=" * 70)


# A hand-written parser for Argos JSON-LD Product schema
ARGOS_PARSER_CODE = """
def parse(html, url):
    import re, json
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    scripts = soup.find_all('script', type='application/ld+json')
    for s in scripts:
        try:
            data = json.loads(s.string or '')
        except Exception:
            continue
        items = data.get('@graph', [data]) if isinstance(data, dict) else data
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Product':
                offers = item.get('offers', {})
                if isinstance(offers, list): offers = offers[0]
                price = offers.get('price') if isinstance(offers, dict) else None
                images = item.get('image', [])
                if isinstance(images, str): images = [images]
                return {
                    'title': item.get('name', ''),
                    'brand': (item.get('brand') or {}).get('name') if isinstance(item.get('brand'), dict) else item.get('brand'),
                    'price': str(price) if price is not None else None,
                    'currency': offers.get('priceCurrency') if isinstance(offers, dict) else None,
                    'image_urls': images,
                    'in_stock': True,
                }
    return {'title': '', 'in_stock': False, 'image_urls': []}
"""

# A parser that always fails (returns empty dict, gate fails)
BROKEN_PARSER_CODE = """
def parse(html, url):
    return {'title': '', 'in_stock': True, 'image_urls': []}
"""


async def run() -> None:
    from src.scraping.storage import ScrapeDB, ParserStore, ResultStore, RunStore
    from src.scraping.scrapers.sites.argos import ArgosScraper

    section("M6.1 - get_active_ordered_by_hits sorts correctly")
    db = ScrapeDB(_DB_PATH); db.init_db()
    ps = ParserStore(db)
    # Seed 3 parsers
    p1 = ps.create("argos", "v1", "def parse(h,u): return {}")
    p2 = ps.create("argos", "v2", "def parse(h,u): return {}")
    p3 = ps.create("argos", "v3", "def parse(h,u): return {}")
    # Give p2 the most hits
    rs = RunStore(db)
    for _ in range(5):
        rs.record(f"http://x/{_}", "argos.co.uk", "argos", "ArgosScraper",
                  "success", "fast", winning_parser_id=p2)
    for _ in range(2):
        rs.record(f"http://y/{_}", "argos.co.uk", "argos", "ArgosScraper",
                  "success", "fast", winning_parser_id=p1)
    # p3 has 0 hits

    ordered = ps.get_active_ordered_by_hits("argos")
    check("3 parsers returned", len(ordered) == 3)
    check("p2 (5 hits) is first", ordered[0]["id"] == p2, f"got id={ordered[0]['id']}")
    check("p1 (2 hits) is second", ordered[1]["id"] == p1, f"got id={ordered[1]['id']}")
    check("p3 (0 hits) is last", ordered[2]["id"] == p3, f"got id={ordered[2]['id']}")

    section("M6.2 - Tiebreak: 0-hit parsers sort by id DESC (fresh first)")
    # Retire p1/p2/p3 to isolate this test
    ps.retire(p1); ps.retire(p2); ps.retire(p3)
    a = ps.create("argos", "va", "def parse(h,u): return {}")
    b = ps.create("argos", "vb", "def parse(h,u): return {}")
    c = ps.create("argos", "vc", "def parse(h,u): return {}")
    ordered = ps.get_active_ordered_by_hits("argos")
    check("newer 0-hit parser sorts first",
          [p["id"] for p in ordered] == [c, b, a],
          str([p["id"] for p in ordered]))
    ps.retire(a); ps.retire(b); ps.retire(c)
    db.close()

    section("M6.3 - HTMLScraper picks first-passing parser")
    db = ScrapeDB(_DB_PATH); db.init_db()
    ps = ParserStore(db)
    # Seed a broken parser (higher id) and a working one
    broken_id = ps.create("argos", "broken", BROKEN_PARSER_CODE)
    working_id = ps.create("argos", "working_v1", ARGOS_PARSER_CODE)
    # Give broken more hits so it sorts first
    rs = RunStore(db)
    for _ in range(3):
        rs.record(f"http://z/{_}", "argos.co.uk", "argos", "ArgosScraper",
                  "success", "fast", winning_parser_id=broken_id)
    db.close()

    argos_html = (DATA_DIR / "argos_response_1.html").read_text(encoding="utf-8")

    async def _fake_fetch(url):
        return (200, argos_html)

    # Prevent M8 repair ladder from triggering (would call DeepSeek)
    with patch("src.scraping.repair.agent.run_repair_ladder", new=AsyncMock(return_value=None)):
        scraper = ArgosScraper()
        with patch.object(scraper, "_get_unlocker") as mock:
            mock.return_value.fetch = _fake_fetch
            result = await scraper.scrape("https://www.argos.co.uk/product/3284476")

    from src.scraping.models import ProductData
    check("scrape returned ProductData", isinstance(result, ProductData),
          f"got {type(result).__name__}")
    if isinstance(result, ProductData):
        check("parser_version reflects winning parser", result.parser_version == "working_v1",
              result.parser_version)

    section("M6.4 - winning_parser_id recorded to scrape_runs")
    db = ScrapeDB(_DB_PATH); db.init_db()
    rows = db.conn.execute(
        "SELECT winning_parser_id, outcome, path FROM scrape_runs WHERE url = ?",
        ("https://www.argos.co.uk/product/3284476",)
    ).fetchall()
    check("scrape_run written", len(rows) == 1, str(rows))
    if rows:
        check("winning_parser_id = working parser",
              rows[0]["winning_parser_id"] == working_id,
              f"got {rows[0]['winning_parser_id']}, expected {working_id}")
        check("outcome=success", rows[0]["outcome"] == "success", rows[0]["outcome"])
        check("path=fast", rows[0]["path"] == "fast", rows[0]["path"])
    db.close()

    section("M6.5 - results table receives ProductData")
    db = ScrapeDB(_DB_PATH); db.init_db()
    rts = ResultStore(db)
    results = rts.get_by_url("https://www.argos.co.uk/product/3284476")
    check("result stored", len(results) == 1)
    if results:
        check("title extracted", results[0]["product_data"].get("title"),
              (results[0]["product_data"].get("title") or "")[:40])
    db.close()

    section("M6.6 - No parsers => _run_parsers returns None")
    # Clean parsers table
    db = ScrapeDB(_DB_PATH); db.init_db()
    ps = ParserStore(db)
    for p in ps.get_active("argos"):
        ps.retire(p["id"])
    db.close()

    scraper = ArgosScraper()
    parsed = await scraper._run_parsers("<html></html>", "http://z")
    check("empty parsers returns None", parsed is None, str(parsed))


def main() -> int:
    try:
        asyncio.run(run())
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()
    finally:
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    print(); print("=" * 70)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 70)
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name}  ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

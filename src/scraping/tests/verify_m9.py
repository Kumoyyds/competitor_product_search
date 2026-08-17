"""Verification for M9 — golden set + promote/prune.

Covers:
  - classify_page_type for all 4 buckets
  - maybe_seed_golden: distinct URLs grow to cap; duplicate URL is skipped
  - promote_candidate: candidate matching all goldens → promoted
  - promote_candidate: candidate failing a golden → rejected
  - _prune_hard_cap: >4 parsers → oldest lowest-hit retired
  - prune_stale: parsers with 0 hits in last N runs retired

Offline; no LLM.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from decimal import Decimal
from pathlib import Path
from ._harness import FAILED, PASSED, check, run_main, section, use_temp_scrape_db
from tests._support.factories import product_data

_DB_PATH = use_temp_scrape_db("verify_m9")

from src.scraping import config as _config
_config._config = None

DATA_DIR = Path(__file__).parent.parent / "data"







def make_product(**kwargs):
    defaults = {"url": "http://x/1", "website": "argos"}
    defaults.update(kwargs)
    return product_data(**defaults)


async def run() -> None:
    from src.scraping.repair.golden import (
        classify_page_type,
        maybe_seed_golden,
        promote_candidate,
        prune_stale,
    )
    from src.scraping.storage import GoldenStore, ParserStore, ScrapeDB, RunStore

    section("M9.1 - classify_page_type: 5 buckets")
    check("in_stock=False -> out_of_stock",
          classify_page_type(make_product(in_stock=False, price=None)) == "out_of_stock")
    check("membership_price > 0 -> membership",
          classify_page_type(make_product(price=Decimal("129.99"),
                                          membership_price=Decimal("99.99"))) == "membership")
    check("membership trumps discounted",
          classify_page_type(make_product(price=Decimal("99.99"),
                                          list_price=Decimal("129.99"),
                                          membership_price=Decimal("99.99"))) == "membership")
    check("list_price > price -> discounted",
          classify_page_type(make_product(price=Decimal("15.00"),
                                          list_price=Decimal("20.00"))) == "discounted")
    check("variant.pack_qty > 1 -> multipack",
          classify_page_type(make_product(variant={"pack_qty": 3})) == "multipack")
    check("standard -> standard",
          classify_page_type(make_product()) == "standard")
    check("list_price == price is NOT discount",
          classify_page_type(make_product(price=Decimal("15.00"),
                                          list_price=Decimal("15.00"))) == "standard")
    check("out_of_stock takes priority over discount",
          classify_page_type(make_product(in_stock=False, price=Decimal("15.00"),
                                          list_price=Decimal("20.00"))) == "out_of_stock")
    check("out_of_stock takes priority over membership",
          classify_page_type(make_product(in_stock=False, price=None,
                                          membership_price=Decimal("2.00"))) == "out_of_stock")

    section("M9.2 - maybe_seed_golden: first product seeded, duplicate URL skipped")
    p1 = make_product(title="Product A")
    seed_id_1 = maybe_seed_golden("argos", "<html>a</html>", p1)
    check("first product of 'standard' bucket seeded", seed_id_1 is not None,
          f"seed_id={seed_id_1}")

    p2 = make_product(title="Product B")
    seed_id_2 = maybe_seed_golden("argos", "<html>b</html>", p2)
    check("same URL in same bucket NOT re-seeded", seed_id_2 is None,
          f"seed_id={seed_id_2}")

    # Different page_type is seeded
    p3 = make_product(title="Product C", in_stock=False, price=None)
    seed_id_3 = maybe_seed_golden("argos", "<html>c</html>", p3)
    check("different page_type (out_of_stock) seeded", seed_id_3 is not None,
          f"seed_id={seed_id_3}")

    db = ScrapeDB(_DB_PATH); db.init_db()
    coverage = GoldenStore(db).get_page_type_coverage("argos")
    check("coverage: standard=1, out_of_stock=1",
          coverage.get("standard") == 1 and coverage.get("out_of_stock") == 1,
          str(coverage))
    db.close()

    section("M9.3 - promote_candidate: correct candidate promoted")
    good_parser = """
def parse(html, url):
    return {'title': 'Product A', 'in_stock': True, 'price': '19.99',
            'currency': 'GBP', 'image_urls': [], 'availability_raw': 'In stock'}
"""
    ooo_product = make_product(title="Product C", in_stock=False, price=None)
    # This candidate should match both goldens (standard: 'Product A', out_of_stock: 'Product C')
    # Actually it can't match both — but the golden test allows staleness.
    # For a cleaner test: reset goldens with just one, then check
    db = ScrapeDB(_DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM golden_samples WHERE site='argos'")
    db.conn.execute("DELETE FROM parsers WHERE site='argos'")
    db.conn.commit()
    # Seed one golden that good_parser can reproduce
    from src.scraping.repair.golden import maybe_seed_golden as _seed
    _seed("argos", "<html>a</html>", make_product(title="Product A"))
    db.close()

    parser_id = await promote_candidate(
        site="argos",
        code=good_parser,
        current_product=make_product(title="Product A"),
        current_html="<html>a</html>",
    )
    check("good candidate promoted", isinstance(parser_id, int), f"parser_id={parser_id}")

    section("M9.4 - promote_candidate: wrong candidate rejected")
    bad_parser = """
def parse(html, url):
    return {'title': 'WRONG', 'in_stock': True, 'price': '19.99',
            'currency': 'GBP', 'image_urls': []}
"""
    from src.scraping.repair.golden import GoldenRejection
    rejected = await promote_candidate(
        site="argos",
        code=bad_parser,
        current_product=make_product(title="WRONG"),
        current_html="<html>a</html>",
    )
    check("bad candidate rejected", isinstance(rejected, GoldenRejection), f"got {type(rejected).__name__}={rejected}")

    section("M9.5 - _prune_hard_cap: >4 active parsers retires lowest-hit oldest")
    db = ScrapeDB(_DB_PATH); db.init_db()
    ps = ParserStore(db)
    # Clean slate
    db.conn.execute("DELETE FROM parsers WHERE site='tesco'")
    db.conn.execute("DELETE FROM scrape_runs WHERE site='tesco'")
    db.conn.commit()
    ids = [ps.create("tesco", f"v{i}", f"# parser {i}") for i in range(4)]
    # Give the newest (ids[3]) most hits and ids[0] fewest
    rs = RunStore(db)
    for _ in range(10):
        rs.record(f"http://n1/{_}", "tesco.com", "tesco", "TescoScraper",
                  "success", "fast", winning_parser_id=ids[3])
    for _ in range(3):
        rs.record(f"http://n2/{_}", "tesco.com", "tesco", "TescoScraper",
                  "success", "fast", winning_parser_id=ids[1])
    # ids[0] and ids[2] have 0 hits
    db.close()

    # Trigger hard cap by promoting a 5th
    good_parser_v2 = """
def parse(html, url):
    return {'title': 'X', 'in_stock': True, 'price': '9.99',
            'currency': 'GBP', 'image_urls': [], 'availability_raw': 'In stock'}
"""
    db = ScrapeDB(_DB_PATH); db.init_db()
    from src.scraping.repair.golden import maybe_seed_golden as _seed
    _seed("tesco", "<html>x</html>", make_product(website="tesco", title="X",
                                                   price=Decimal("9.99")))
    db.close()

    new_id = await promote_candidate(
        site="tesco",
        code=good_parser_v2,
        current_product=make_product(website="tesco", title="X", price=Decimal("9.99")),
        current_html="<html>x</html>",
    )
    check("5th candidate promoted after hard cap prune", isinstance(new_id, int),
          f"new_id={new_id}")

    db = ScrapeDB(_DB_PATH); db.init_db()
    actives = ParserStore(db).get_active("tesco")
    active_ids = {p["id"] for p in actives}
    check("still 4 active parsers after hard-cap prune",
          len(actives) == 4, str(len(actives)))
    # ids[0] was oldest 0-hit → should be retired
    check("oldest 0-hit (ids[0]) retired", ids[0] not in active_ids,
          f"ids[0]={ids[0]}, actives={active_ids}")
    db.close()

    section("M9.6 - prune_stale: parser with 0 hits in last N runs retired")
    db = ScrapeDB(_DB_PATH); db.init_db()
    # Clean slate for one more test
    db.conn.execute("DELETE FROM parsers WHERE site='stale_site'")
    db.conn.execute("DELETE FROM scrape_runs WHERE site='stale_site'")
    db.conn.commit()
    ps = ParserStore(db)
    active_id = ps.create("stale_site", "active", "# active")
    stale_id = ps.create("stale_site", "stale", "# stale")
    rs = RunStore(db)
    # 60 runs (> window of 50), stale never wins
    for i in range(60):
        rs.record(f"http://s/{i}", "stale_site.com", "stale_site",
                  "SS", "success", "fast", winning_parser_id=active_id)
    # Give stale exactly 1 hit outside the window (but the query looks at recent 50)
    # Actually we want it to have ever won at least once so total_ever > 0
    rs.record("http://s/init", "stale_site.com", "stale_site",
              "SS", "success", "fast", winning_parser_id=stale_id)
    # Now add another 60 runs won by active so stale falls out of the window
    for i in range(60, 120):
        rs.record(f"http://s/{i}", "stale_site.com", "stale_site",
                  "SS", "success", "fast", winning_parser_id=active_id)
    db.close()

    retired = prune_stale("stale_site")
    check("stale parser retired", stale_id in retired, f"retired={retired}")
    check("active parser NOT retired", active_id not in retired, f"retired={retired}")


def main() -> int:
    return run_main(run, width=70)


if __name__ == "__main__":
    sys.exit(main())

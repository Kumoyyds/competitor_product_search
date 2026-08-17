"""Verification script for M1 (ProductData + gates), M2 (Router + Registry), M3 (SQLite).

Run from repo root:
    python -m src.scraping.tests.verify_m1_m3
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime
from decimal import Decimal

from ._harness import FAILED, PASSED, SKIPPED, check, section, skip, run_main






# ---------------------------------------------------------------------------
# M1 - ProductData + two gates
# ---------------------------------------------------------------------------

def verify_m1() -> None:
    section("M1 - ProductData schema + two gates (validation)")

    from src.scraping.models import InvalidTargetResult, ProductData
    from src.scraping.validation import validate

    base = {
        "url": "https://example.com/p/1",
        "website": "test",
        "scraped_at": datetime.now(),
        "source_type": "html",
        "title": "Test Product",
        "in_stock": True,
        "price": Decimal("19.99"),
        "currency": "GBP",
        "image_urls": [],
    }

    # in_stock=True + price present -> pass
    pd, errs = validate(base)
    check("in_stock=True + price present -> passes both gates",
          pd is not None and not errs, str(errs))
    check("price stays as Decimal after validation",
          isinstance(pd.price, Decimal), type(pd.price).__name__)

    # in_stock=True + price=None -> Gate 2 fault
    pd2, errs2 = validate({**base, "price": None})
    check("in_stock=True + price=None -> Gate 2 catches fault",
          pd2 is None, str(errs2))
    check("Gate 2 error message mentions price",
          errs2 and "price" in errs2[0].lower(), str(errs2))

    # in_stock=True + only membership_price > 0 -> price is still required
    pd5, errs5 = validate({**base, "price": None, "membership_price": Decimal("14.99")})
    check("in_stock=True + only membership_price -> Gate 2 requires price",
          pd5 is None and errs5 and "price" in errs5[0].lower(), str(errs5))

    # in_stock=False + no images + membership_price present -> passes (product signal)
    pd6, errs6 = validate({
        **base,
        "in_stock": False,
        "price": None,
        "image_urls": [],
        "membership_price": Decimal("8.99"),
    })
    check("in_stock=False + only membership_price -> passes (product signal present)",
          pd6 is not None and not errs6, str(errs6))

    # in_stock=False + price=None but has image -> legal (product signal present)
    pd3, errs3 = validate({
        **base,
        "in_stock": False,
        "price": None,
        "image_urls": ["https://img.example.com/p.jpg"],
    })
    check("in_stock=False + price=None + image -> legal (product signal present)",
          pd3 is not None and not errs3, str(errs3))

    # in_stock=False + price present -> legal (some sites keep last price)
    pd4, errs4 = validate({**base, "in_stock": False})
    check("in_stock=False + price present -> legal",
          pd4 is not None and not errs4, str(errs4))

    # image_urls=[] should pass (spec 5.1)
    check("image_urls=[] accepted", pd.image_urls == [])

    # InvalidTargetResult is separate from ProductData
    itr = InvalidTargetResult(url="x", site="test", reason_signal="http_404")
    check("InvalidTargetResult constructed", itr.site == "test")


# ---------------------------------------------------------------------------
# M2 - Router + Registry
# ---------------------------------------------------------------------------

def verify_m2() -> None:
    section("M2 - BaseScraper + Router two-hop + registry")

    # Trigger site registrations
    from src.scraping.registry import get_all_sites, get_scrapers
    from src.scraping.router import resolve_site
    from src.scraping.scrapers import sites  # noqa: F401

    sites_found = set(get_all_sites())
    check("amazon registered", "amazon" in sites_found)
    check("tesco registered", "tesco" in sites_found)
    check("argos registered", "argos" in sites_found)

    tesco_scrapers = get_scrapers("tesco")
    check("tesco has 2 scrapers (HTML primary + DCA backup)",
          len(tesco_scrapers) == 2, str([s.__name__ for s in tesco_scrapers]))
    check("Tesco order: HTMLScraper first",
          tesco_scrapers[0].__name__ == "TescoScraper")
    check("Tesco order: DCA second (backup)",
          tesco_scrapers[1].__name__ == "TescoDCAScraper")

    argos_scrapers = get_scrapers("argos")
    check("argos has 2 scrapers (HTML primary + DCA backup)",
          len(argos_scrapers) == 2, str([s.__name__ for s in argos_scrapers]))
    check("Argos order: HTMLScraper first",
          argos_scrapers[0].__name__ == "ArgosScraper")
    check("Argos order: DCA second (backup)",
          argos_scrapers[1].__name__ == "ArgosDCAScraper")

    # host -> site resolution (loaded from hosts.yaml)
    cases = [
        ("https://www.tesco.com/shop/en-GB/products/123", "tesco"),
        ("https://tesco.com/x", "tesco"),
        ("https://www.argos.co.uk/product/3284476", "argos"),
        ("https://www.amazon.co.uk/dp/XYZ", "amazon"),
        ("https://www.amazon.de/dp/ABC", "amazon"),
        ("https://www.amazon.fr/dp/DEF", "amazon"),
        ("https://WWW.TESCO.COM/x", "tesco"),  # case-insensitive
    ]
    for url, expected in cases:
        got = resolve_site(url)
        check(f"resolve_site({url}) -> {expected}", got == expected, f"got {got}")

    # Unknown host raises helpful ValueError
    try:
        resolve_site("https://unknown-site.example.com/x")
        check("unknown host raises ValueError", False, "no exception raised")
    except ValueError as e:
        check("unknown host raises ValueError with hint",
              "hosts.yaml" in str(e), str(e)[:80])


# ---------------------------------------------------------------------------
# M3 - SQLite 6 tables + stores
# ---------------------------------------------------------------------------

def verify_m3() -> None:
    section("M3 - SQLite 6 tables + stores")

    from src.scraping.models import ProductData
    from src.scraping.storage import (
        EscalationStore,
        GoldenStore,
        ParserStore,
        PhraseStore,
        ResultStore,
        RunStore,
        ScrapeDB,
    )

    db_path = os.path.join(tempfile.gettempdir(), "verify_m3.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    with ScrapeDB(db_path) as db:
        tables = {
            r["name"]
            for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected_tables = {
            "parsers", "golden_samples", "scrape_runs",
            "results", "escalations", "invalid_target_phrases",
        }
        missing = expected_tables - tables
        check(f"All 6 tables created", not missing,
              f"missing: {missing}" if missing else f"found: {sorted(expected_tables)}")

        # ParserStore
        ps = ParserStore(db)
        pid = ps.create("tesco", "v1", "def parse(html): pass")
        check("Parser insert + lookup", ps.get_by_id(pid) is not None)
        check("Parser status defaults to active",
              ps.get_active("tesco")[0]["status"] == "active")
        ps.retire(pid)
        check("Parser retire flips status", len(ps.get_active("tesco")) == 0)

        # RunStore + dedup window
        rs = RunStore(db, dedup_window_seconds=3600)
        rs.record("https://tesco.com/p/1", "tesco.com", "tesco", "TescoScraper", "success", "fast")
        check("Recent URL flagged as duplicate", rs.is_duplicate("https://tesco.com/p/1"))
        check("Unseen URL NOT duplicate", not rs.is_duplicate("https://tesco.com/p/2"))

        # ResultStore append-only
        pd = ProductData(
            url="https://tesco.com/p/1", website="tesco",
            scraped_at=datetime.now(), source_type="html",
            title="Test", in_stock=True, price=Decimal("9.99"),
        )
        rts = ResultStore(db)
        rts.append(pd)
        rts.append(pd)  # append again (time series preserved, D24)
        check("Results append-only (2 rows for same URL)",
              len(rts.get_by_url("https://tesco.com/p/1")) == 2)

        # EscalationStore signature dedup
        es = EscalationStore(db)
        es.upsert("(tesco,price,v1)", "parser_broken", snapshot={"a": 1})
        es.upsert("(tesco,price,v1)", "parser_broken")
        es.upsert("(tesco,price,v1)", "parser_broken")
        open_esc = es.get_open()
        check("Signature dedup: 3 upserts -> 1 row",
              len(open_esc) == 1, f"got {len(open_esc)} rows")
        check("affected_count increments to 3",
              open_esc[0]["affected_count"] == 3, str(open_esc[0]["affected_count"]))

        # PhraseStore
        phs = PhraseStore(db)
        phs.add("tesco", "Oops, that didn't go to plan")
        check("Phrase stored and retrievable",
              phs.get_phrases("tesco") == ["Oops, that didn't go to plan"])

        # GoldenStore
        gs = GoldenStore(db)
        gs.seed("tesco", "standard", "<html>x</html>", {"title": "x"})
        gs.seed("tesco", "out_of_stock", "<html>y</html>", {"title": "y"})
        cov = gs.get_page_type_coverage("tesco")
        check("page_type coverage counts correct",
              cov == {"standard": 1, "out_of_stock": 1}, str(cov))

    if os.path.exists(db_path):
        os.remove(db_path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    return run_main(
        verify_m1,
        verify_m2,
        verify_m3,
        title="Verification script for M1 + M2 + M3",
        width=70,
    )


if __name__ == "__main__":
    sys.exit(main())

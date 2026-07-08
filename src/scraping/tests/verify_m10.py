"""Verification for M10 — escalation writing + mass_invalid_target detection.

Covers:
  - parser_broken escalation on HTML route exhaustion
  - api_malformed escalation on API route exhaustion
  - infra_failure escalation + immediate raise (no fallback to next scraper)
  - Signature dedup (multiple same failures -> affected_count increments)
  - mass_invalid_target escalation triggered at threshold
  - mass_invalid_target NOT triggered below threshold
  - INFRA ALERT logged

Offline; no LLM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import traceback
from unittest.mock import AsyncMock, patch

_DB_PATH = os.path.join(tempfile.gettempdir(), "verify_m10.db")
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)
os.environ["SCRAPING_DB_PATH"] = _DB_PATH

from src.scraping import config as _config
_config._config = None

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


async def run() -> None:
    from src.scraping import router as router_mod
    from src.scraping.exceptions import BrightDataInfraError, ScrapeFailed
    from src.scraping.storage import EscalationStore, RunStore, ScrapeDB
    from src.scraping.scrapers.sites.argos import ArgosScraper
    from src.scraping.scrapers.sites.amazon_uk import AmazonUKScraper

    # Trigger imports so registry populates
    from src.scraping.scrapers import sites  # noqa: F401

    section("M10.1 - parser_broken escalation after HTML route exhausted")
    db = ScrapeDB(_DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM escalations")
    db.conn.commit()
    db.close()

    async def _always_fail(self, url):
        raise ScrapeFailed(
            site="argos", url=url, scraper_name="ArgosScraper",
            failed_stage="parser_broken", signature=("argos", "title", "v1"),
            errors=["gate fail"], snapshot="<html>x</html>",
        )

    with patch.object(ArgosScraper, "scrape", new=_always_fail):
        try:
            await router_mod.scrape("https://www.argos.co.uk/product/3284476")
        except ScrapeFailed as e:
            check("Router raises ScrapeFailed on exhaustion",
                  e.failed_stage == "scraper_fallback_exhausted",
                  e.failed_stage)

    db = ScrapeDB(_DB_PATH); db.init_db()
    escs = EscalationStore(db).get_open()
    check("escalation row written", len(escs) == 1, str(len(escs)))
    if escs:
        check("reason=parser_broken", escs[0]["reason"] == "parser_broken",
              escs[0]["reason"])
        check("signature contains site + rule",
              "argos" in escs[0]["signature"] and "title" in escs[0]["signature"],
              escs[0]["signature"])
    db.close()

    section("M10.2 - Signature dedup: repeated failures increment affected_count")
    with patch.object(ArgosScraper, "scrape", new=_always_fail):
        try:
            await router_mod.scrape("https://www.argos.co.uk/product/9999")
        except ScrapeFailed:
            pass

    db = ScrapeDB(_DB_PATH); db.init_db()
    escs = EscalationStore(db).get_open()
    check("still 1 escalation row (dedup)", len(escs) == 1, str(len(escs)))
    if escs:
        check("affected_count = 2", escs[0]["affected_count"] == 2,
              str(escs[0]["affected_count"]))
    db.close()

    section("M10.3 - infra_failure bypasses fallback + INFRA ALERT logged")
    db = ScrapeDB(_DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM escalations")
    db.conn.commit()
    db.close()

    tesco_scraper_1_calls = []
    tesco_scraper_2_calls = []

    async def _raise_infra(self, url):
        tesco_scraper_1_calls.append(url)
        raise BrightDataInfraError("quota exhausted", status_code=407)

    async def _should_not_be_called(self, url):
        tesco_scraper_2_calls.append(url)
        return None

    # capture logs
    log_records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: log_records.append(record.getMessage())
    logging.getLogger("src.scraping.router").addHandler(handler)
    logging.getLogger("src.scraping.router").setLevel(logging.INFO)

    from src.scraping.scrapers.sites.tesco import TescoScraper
    from src.scraping.scrapers.sites.tesco_dca import TescoDCAScraper

    with patch.object(TescoScraper, "scrape", new=_raise_infra), \
         patch.object(TescoDCAScraper, "scrape", new=_should_not_be_called):
        try:
            await router_mod.scrape("https://www.tesco.com/shop/en-GB/products/123")
        except BrightDataInfraError:
            pass  # expected

    check("TescoScraper (order=1) called", len(tesco_scraper_1_calls) == 1,
          str(len(tesco_scraper_1_calls)))
    check("TescoDCAScraper NOT called (D21 bypass)",
          len(tesco_scraper_2_calls) == 0, str(len(tesco_scraper_2_calls)))
    check("INFRA ALERT log emitted",
          any("INFRA ALERT" in msg for msg in log_records),
          f"logs: {log_records[-3:]}")

    db = ScrapeDB(_DB_PATH); db.init_db()
    escs = EscalationStore(db).get_open()
    check("infra_failure escalation written", any(e["reason"] == "infra_failure" for e in escs),
          str([e["reason"] for e in escs]))
    db.close()

    section("M10.4 - api_malformed escalation on API route exhaustion")
    db = ScrapeDB(_DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM escalations")
    db.conn.commit()
    db.close()

    async def _api_gate_fail(self, url):
        raise ScrapeFailed(
            site="amazon", url=url, scraper_name="AmazonUKScraper",
            failed_stage="api_malformed", signature=("amazon", "price", ""),
            errors=["gate2 failed"],
        )

    with patch.object(AmazonUKScraper, "scrape", new=_api_gate_fail):
        try:
            await router_mod.scrape("https://www.amazon.co.uk/dp/B0XYZ")
        except ScrapeFailed:
            pass

    db = ScrapeDB(_DB_PATH); db.init_db()
    escs = EscalationStore(db).get_open()
    check("api_malformed escalation written",
          any(e["reason"] == "api_malformed" for e in escs),
          str([e["reason"] for e in escs]))
    db.close()

    section("M10.5 - mass_invalid_target triggers when count exceeds threshold")
    db = ScrapeDB(_DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM escalations")
    db.conn.execute("DELETE FROM scrape_runs")
    db.conn.commit()
    rs = RunStore(db)
    # Seed >20 invalid_target runs for a site within 24h to blow past absolute threshold
    for i in range(25):
        rs.record(f"http://massive/{i}", "argos.co.uk", "argos", "ArgosScraper",
                  "invalid_target", "invalid_target")
    # 5 normal runs so total is 30
    for i in range(5):
        rs.record(f"http://ok/{i}", "argos.co.uk", "argos", "ArgosScraper",
                  "success", "fast")
    db.close()

    scraper = ArgosScraper()
    scraper._check_mass_invalid_target("argos.co.uk", "http://trigger")

    db = ScrapeDB(_DB_PATH); db.init_db()
    escs = EscalationStore(db).get_open()
    mass = [e for e in escs if e["reason"] == "mass_invalid_target"]
    check("mass_invalid_target escalation triggered",
          len(mass) == 1, str(len(mass)))
    if mass:
        check("mass signature uses invalid_target_surge",
              "invalid_target_surge" in mass[0]["signature"], mass[0]["signature"])
    db.close()

    section("M10.6 - mass_invalid_target does NOT trigger below threshold")
    db = ScrapeDB(_DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM escalations WHERE reason='mass_invalid_target'")
    db.conn.execute("DELETE FROM scrape_runs")
    db.conn.commit()
    rs = RunStore(db)
    # Only 2 invalid_target in 100 runs -> ratio 2% < 30%, count 2 < 20
    for i in range(2):
        rs.record(f"http://few/{i}", "argos.co.uk", "argos", "ArgosScraper",
                  "invalid_target", "invalid_target")
    for i in range(100):
        rs.record(f"http://many/{i}", "argos.co.uk", "argos", "ArgosScraper",
                  "success", "fast")
    db.close()

    scraper._check_mass_invalid_target("argos.co.uk", "http://noise")
    db = ScrapeDB(_DB_PATH); db.init_db()
    escs = EscalationStore(db).get_open()
    mass = [e for e in escs if e["reason"] == "mass_invalid_target"]
    check("no mass_invalid_target below threshold", len(mass) == 0, str(len(mass)))
    db.close()


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

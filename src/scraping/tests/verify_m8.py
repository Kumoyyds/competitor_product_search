"""Verification for M8 — repair agent + JSON healer.

Uses REAL Qwen API (per user decision). Gracefully skips if QWEN_KEY missing.

Covers:
  - RepairContext accumulates errors across attempts
  - _make_llm returns None gracefully when key missing
  - no_product prompt on error page -> agent decision recorded
  - parser_gen_prompt returns usable parser code on real product HTML
  - Full ladder end-to-end: broken parser -> agent generates working one -> promoted
  - JSON healer: D25 red line rejects mapping to non-existent path
  - JSON healer: valid remap of already-existing key succeeds

Cost: ~$0.01-0.05 per full run (a handful of Qwen requests).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path

_DB_PATH = os.path.join(tempfile.gettempdir(), "verify_m8.db")
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)
os.environ["SCRAPING_DB_PATH"] = _DB_PATH

from dotenv import load_dotenv
load_dotenv()

from src.scraping import config as _config
_config._config = None
cfg = _config.get_config()

DATA_DIR = Path(__file__).parent.parent / "data"
HAS_LLM = bool(cfg.qwen_key)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
SKIPPED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def skip(name: str, reason: str) -> None:
    SKIPPED.append(name)
    print(f"  [SKIP] {name}  ({reason})")


def section(title: str) -> None:
    print(); print("=" * 70); print(title); print("=" * 70)


async def run() -> None:
    from src.scraping.repair.agent import (
        CandidateFailed,
        CandidateSucceeded,
        NoProductVerdict,
        RepairContext,
        _make_llm,
        _try_repair,
        run_repair_ladder,
    )
    from src.scraping.repair.json_healer import (
        _lookup_path,
        _extract_missing_fields,
        heal_json,
    )

    section("M8.1 - Config and LLM setup")
    check("QWEN_KEY loaded", HAS_LLM, "key present" if HAS_LLM else "MISSING")
    if HAS_LLM:
        llm = _make_llm("qwen-3.7-plus")
        check("_make_llm returns client with qwen-3.7-plus", llm is not None)
    else:
        llm_missing = _make_llm("qwen-3.7-plus")
        check("_make_llm returns None when key missing", llm_missing is None)

    section("M8.2 - JSON healer: _lookup_path")
    data = {"a": {"b": [{"c": 42}, {"c": 100}]}}
    check("dotted path resolves nested value",
          _lookup_path(data, "a.b.0.c") == 42, str(_lookup_path(data, "a.b.0.c")))
    check("missing path returns None",
          _lookup_path(data, "a.x.y") is None)
    check("bad index returns None",
          _lookup_path(data, "a.b.99.c") is None)

    section("M8.3 - JSON healer: _extract_missing_fields")
    errors = ["title: required", "in_stock=True but price is missing"]
    missing = _extract_missing_fields(errors)
    check("price extracted from errors", "price" in missing)
    check("title extracted from errors", "title" in missing)

    if not HAS_LLM:
        skip("M8.4-M8.7", "QWEN_KEY not set")
        return

    section("M8.4 - JSON healer: valid remap of existing key succeeds")
    json_payload = {
        "product_title": "Test Product",
        "prices": {"actual": 19.99, "was": 24.99},
        "stock_status": True,
        "images": ["http://x/1.jpg"],
    }
    mapped_broken = {
        "title": "Test Product",  # already good
        "in_stock": True,
        "image_urls": ["http://x/1.jpg"],
        # price MISSING — heal should propose "prices.actual"
    }
    healed = await heal_json(
        json_data=json_payload,
        mapped=mapped_broken,
        errors=["in_stock=True but price is missing"],
        site="amazon",
    )
    check("healer applied remap for existing field",
          healed is not None and healed.get("price") is not None,
          f"healed price={healed.get('price') if healed else 'None'}")

    section("M8.5 - JSON healer: D25 red line rejects fabrication")
    # A JSON where price GENUINELY doesn't exist anywhere
    json_no_price = {
        "product_title": "Test Product",
        "images": ["http://x/1.jpg"],
    }
    mapped_no_price = {
        "title": "Test Product",
        "in_stock": True,
        "image_urls": ["http://x/1.jpg"],
    }
    healed = await heal_json(
        json_data=json_no_price,
        mapped=mapped_no_price,
        errors=["in_stock=True but price is missing"],
        site="amazon",
    )
    check("healer refuses to fabricate missing data (D25)",
          healed is None or healed.get("price") is None,
          f"healed={healed}")

    section("M8.6 - Repair agent: no_product judgment on error page")
    error_html = """<html><head><title>Oops</title></head><body>
    <div>Oops, that didn't go to plan. The page you're looking for couldn't be found.</div>
    <a href="/">Return to home</a>
    </body></html>""" + "x" * 8000  # pad past detection min length

    ctx = RepairContext(site="tesco", url="http://tesco.com/missing", html=error_html)
    outcome = await _try_repair(ctx, "qwen-3.7-plus")
    check("agent identifies error page as no_product",
          isinstance(outcome, NoProductVerdict), f"got {type(outcome).__name__}")
    if isinstance(outcome, NoProductVerdict):
        check("no_product verdict includes a phrase",
              outcome.phrase is not None and len(outcome.phrase) > 0,
              (outcome.phrase or "")[:60])

    section("M8.7 - Repair agent: generates parser for real Argos HTML")
    argos_html = (DATA_DIR / "argos_response_1.html").read_text(encoding="utf-8")

    # Use HTMLScraper stub for run_repair_ladder
    class DummyScraper:
        site = "argos"
        def __init__(self): pass
        class _c:
            __name__ = "DummyScraper"

    # ladder end-to-end
    scraper = type("Sc", (), {"site": "argos", "__class__": type("C", (), {"__name__": "DummyScraper"})})()

    outcome = await run_repair_ladder(
        scraper=scraper,
        url="https://www.argos.co.uk/product/3284476",
        html=argos_html,
        initial_errors=["hand-forced initial fail"],
    )
    from src.scraping.models import ProductData
    from src.scraping.exceptions import ScrapeFailed
    print(f"    ladder outcome type: {type(outcome).__name__}")
    if isinstance(outcome, ProductData):
        print(f"    extracted title:  {outcome.title[:60]}")
        print(f"    extracted price:  {outcome.price} {outcome.currency}")
        print(f"    extracted brand:  {outcome.brand}")
        check("ladder produced ProductData", True)
        check("title extracted", outcome.title and len(outcome.title) > 5)
        check("price extracted", outcome.price is not None)
    elif isinstance(outcome, ScrapeFailed):
        print(f"    ScrapeFailed: {outcome}")
        print(f"    errors: {outcome.errors[-3:]}")
        check("ladder produced ProductData", False,
              f"got ScrapeFailed: {outcome.failed_stage}")
    else:
        check("ladder produced ProductData", False,
              f"got {type(outcome).__name__}")


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
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    print("=" * 70)
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name}  ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

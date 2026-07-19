"""Verification script for M4 (DirectAPIScraper) and M5 (HTMLScraper + detection).

Run from repo root:
    python -m src.scraping.tests.verify_m4_m5

Uses only offline sample data (src/scraping/data/), no real network calls.
"""

from __future__ import annotations

import ast
import sys
import traceback
from decimal import Decimal
from pathlib import Path

# Track results
PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []

DATA_DIR = Path(__file__).parent.parent / "data"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------------
# M4.1 — Amazon field mapping against real sample response
# ---------------------------------------------------------------------------

def verify_amazon_mapping() -> None:
    section("M4.1 - AmazonUKScraper._map_fields on data/amazon_response.json")

    from src.scraping.scrapers.sites.amazon_uk import AmazonUKScraper
    from src.scraping.validation import validate

    with open(DATA_DIR / "amazon_response.json", "r", encoding="utf-8") as f:
        amazon_data = ast.literal_eval(f.read())

    scraper = AmazonUKScraper()
    mapped = scraper._map_fields(amazon_data, "https://www.amazon.de/dp/B0C62DWSDL")

    print(f"  Sample: Amazon.de ASIN B0C62DWSDL (Enzymedica Lypo Gold)")
    print(f"  Mapped fields:")
    print(f"    title       = {mapped['title'][:60]}...")
    print(f"    brand       = {mapped['brand']}")
    print(f"    gtin (upc)  = {mapped['gtin']}")
    print(f"    price       = {mapped['price']} {mapped['currency']}  ({type(mapped['price']).__name__})")
    print(f"    list_price  = {mapped['list_price']} {mapped['currency']}")
    print(f"    unit_price  = {mapped['unit_price']} per {mapped['unit']}")
    print(f"    in_stock    = {mapped['in_stock']}")
    print(f"    avail_raw   = {mapped['availability_raw']}")
    print(f"    image_urls  = {len(mapped['image_urls'])} URLs")
    print(f"    variant     = {mapped['variant']}")
    print()

    check("brand extracted", mapped["brand"] == "Enzymedica", mapped["brand"])
    check("gtin extracted (from upc)", mapped["gtin"] == "670480600016", mapped["gtin"])
    check("price is Decimal", isinstance(mapped["price"], Decimal), type(mapped["price"]).__name__)
    check("price value correct", mapped["price"] == Decimal("23.79"), str(mapped["price"]))
    check("list_price is Decimal", isinstance(mapped["list_price"], Decimal))
    check("list_price value correct", mapped["list_price"] == Decimal("27.99"), str(mapped["list_price"]))
    check("currency EUR", mapped["currency"] == "EUR", mapped["currency"])
    check("unit_price parsed", mapped["unit_price"] == Decimal("668.26"), str(mapped["unit_price"]))
    check("unit parsed (kg)", mapped["unit"] == "kg", mapped["unit"])
    check("in_stock True", mapped["in_stock"] is True)
    check("6 image URLs", len(mapped["image_urls"]) == 6, str(len(mapped["image_urls"])))
    check("variant size captured", mapped["variant"] == {"size": "60 Count"}, str(mapped["variant"]))

    # Run through the two gates
    product, errors = validate(mapped)
    check("Gate 1 + Gate 2 pass", product is not None, str(errors))

    print()
    print(f"  Final ProductData: url={product.url[:50]}...")
    print(f"                     price={product.price} {product.currency}, in_stock={product.in_stock}")


# ---------------------------------------------------------------------------
# M4.2 — TescoDCA mapping (uses payload structure from playground notebook)
# ---------------------------------------------------------------------------

def verify_tesco_dca_mapping() -> None:
    section("M4.2 - TescoDCAScraper._map_fields on DCA sample payload")

    from src.scraping.scrapers.sites.tesco_dca import TescoDCAScraper
    from src.scraping.validation import validate

    # Payload structure taken verbatim from src/scraping/playground.ipynb output
    dca_payload = {
        "product_name": "Yaheetech 2.19-2.79M Height-Adjustable Basketball Hoop System Black",
        "current_price": {"value": 54.99, "currency": "GBP", "symbol": "£"},
        "original_price": {"value": 80.99, "currency": "GBP", "symbol": "£"},
        "currency": "GBP",
        "on_sale": True,
        "product_image": "https://digitalcontent.api.tesco.com/img.jpeg",
        "in_stock": True,
        "input": {"url": "https://www.tesco.com/shop/en-GB/products/326093310"},
    }

    scraper = TescoDCAScraper()
    mapped = scraper._map_fields(dca_payload, dca_payload["input"]["url"])

    print(f"  Sample: Tesco DCA payload for basketball hoop")
    print(f"  Mapped fields:")
    print(f"    title      = {mapped['title'][:60]}...")
    print(f"    price      = {mapped['price']} {mapped['currency']}")
    print(f"    list_price = {mapped['list_price']} (was on sale)")
    print(f"    in_stock   = {mapped['in_stock']}")
    print()

    check("title extracted", "Yaheetech" in mapped["title"])
    check("price is Decimal 54.99", mapped["price"] == Decimal("54.99"), str(mapped["price"]))
    check("list_price 80.99 (from dict)", mapped["list_price"] == Decimal("80.99"), str(mapped["list_price"]))
    check("currency GBP", mapped["currency"] == "GBP")
    check("in_stock True", mapped["in_stock"] is True)
    check("image_urls populated", len(mapped["image_urls"]) == 1)

    product, errors = validate(mapped)
    check("Gate 1 + Gate 2 pass", product is not None, str(errors))


def verify_argos_dca_mapping() -> None:
    section("M4.3 - ArgosDCAScraper._map_fields on DCA sample payload")

    from src.scraping.scrapers.sites.argos_dca import ArgosDCAScraper
    from src.scraping.validation import validate

    dca_payload = {
        "product_title": "McGregor 30cm Electric Hover Collect Lawnmower - 1700W",
        "image_urls": ["https://media.4rgos.it/i/Argos/4490582_R_Z001A?w=134&h=134&qlt=50", "https://media.4rgos.it/i/Argos/4490582_R_Z006A?w=134&h=134&qlt=50", "https://media.4rgos.it/i/Argos/4490582_R_Z008A?w=134&h=134&qlt=50", "https://media.4rgos.it/i/Argos/4490582_R_Z009A?w=134&h=134&qlt=50"],
        "price": {"value": 85, "currency": "GBP", "symbol": "?"},
        "list_price": {"value": 95, "currency": "GBP", "symbol": "?"},
        "currency": "?",
        "discount": True,
        "in_stock": True,
        "input": {"url": "https://www.argos.co.uk/product/4490582"},
    }

    scraper = ArgosDCAScraper()
    mapped = scraper._map_fields(dca_payload, dca_payload["input"]["url"])

    check("ArgosDCA title extracted", "McGregor" in mapped["title"])
    check("ArgosDCA price is Decimal 85", mapped["price"] == Decimal("85"), str(mapped["price"]))
    check("ArgosDCA list_price is Decimal 95", mapped["list_price"] == Decimal("95"), str(mapped["list_price"]))
    check("ArgosDCA currency GBP", mapped["currency"] == "GBP")
    check("ArgosDCA in_stock True", mapped["in_stock"] is True)
    check("ArgosDCA image_urls populated", bool(mapped["image_urls"]))

    product, errors = validate(mapped)
    check("ArgosDCA Gate 1 + Gate 2 pass", product is not None, str(errors))

# ---------------------------------------------------------------------------
# M4.3 — is_not_found detection (invalid_target routing on API side)
# ---------------------------------------------------------------------------

def verify_api_not_found() -> None:
    section("M4.3 - DirectAPIScraper._is_not_found detection")

    from src.scraping.scrapers.sites.amazon_uk import AmazonUKScraper
    from src.scraping.scrapers.sites.tesco_dca import TescoDCAScraper
    from src.scraping.scrapers.sites.argos_dca import ArgosDCAScraper

    amz = AmazonUKScraper()
    check("Amazon: real product NOT flagged",
          not amz._is_not_found({"title": "Real Product"}))
    check("Amazon: empty dict flagged as not_found",
          amz._is_not_found({}))
    check("Amazon: error key flagged as not_found",
          amz._is_not_found({"error": "not found", "title": ""}))
    check("Amazon: missing title flagged",
          amz._is_not_found({"brand": "X"}))

    tdca = TescoDCAScraper()
    check("TescoDCA: product_name present NOT flagged",
          not tdca._is_not_found({"product_name": "Real Product"}))
    check("TescoDCA: missing product_name flagged",
          tdca._is_not_found({"in_stock": True}))
    adca = ArgosDCAScraper()
    check("ArgosDCA: product_title present NOT flagged",
          not adca._is_not_found({"product_title": "Real Product"}))
    check("ArgosDCA: missing product_title flagged",
          adca._is_not_found({"in_stock": True}))



# ---------------------------------------------------------------------------
# M5.1 — Invalid page detection on all 3 real sample HTML files
# ---------------------------------------------------------------------------

def verify_detection_on_real_html() -> None:
    section("M5.1 - detect_invalid_page on real sample HTML files")

    from src.scraping.detection import _has_jsonld_product, detect_invalid_page

    samples = [
        ("argos_response_1.html", "argos", "Argos product 3284476 (shed base)"),
        ("argos_response_2.html", "argos", "Argos product 8747437 (GTA VI)"),
        ("tesco_response.html", "tesco", "Tesco product 297568023 (blouse)"),
    ]

    for filename, site, description in samples:
        path = DATA_DIR / filename
        html = path.read_text(encoding="utf-8")
        has_jsonld = _has_jsonld_product(html)
        signal = detect_invalid_page(html, 200, site)

        print(f"  {filename}  ({len(html):,} chars)  - {description}")
        print(f"    has JSON-LD Product schema: {has_jsonld}")
        print(f"    detect_invalid_page result: {signal}")

        check(f"{filename}: JSON-LD Product schema present", has_jsonld)
        check(f"{filename}: valid product page NOT flagged", signal is None,
              str(signal) if signal else "")
        print()


# ---------------------------------------------------------------------------
# M5.2 — Detection signal layers (each in isolation)
# ---------------------------------------------------------------------------

def verify_detection_signals() -> None:
    section("M5.2 - detect_invalid_page signal layers (isolated)")

    from src.scraping.detection import detect_invalid_page

    # Signal 2: HTTP status
    for code in (404, 410, 403, 451):
        r = detect_invalid_page("<html>x</html>", code, "test")
        check(f"HTTP {code} triggers http_status signal",
              r is not None and r.signal_type == "http_status", str(r))

    # Signal 4: page length anomaly
    r = detect_invalid_page("<html><body>Oops</body></html>", 200, "test")
    check("Short page (<5000 chars) triggers page_length signal",
          r is not None and r.signal_type == "page_length", str(r))

    # Signal 3: multi-absence (page long enough but no product signals)
    fake_no_product = "<html><body>" + "x" * 10000 + "<p>No product content.</p></body></html>"
    r = detect_invalid_page(fake_no_product, 200, "test")
    check("Long page missing 2+ signals triggers multi_absence",
          r is not None and r.signal_type == "multi_absence", str(r))

    # Long page WITH all signals (should pass — no JSON-LD but title+price+cart present)
    fake_valid = ("<html><body>" + "x" * 10000
                  + '<h1>Product Title</h1><span>£19.99</span>'
                  + '<button>Add to basket</button></body></html>')
    r = detect_invalid_page(fake_valid, 200, "test")
    check("Long page with title+price+cart passes (no JSON-LD needed)",
          r is None, str(r))

    # Signal 5: keyword match (page structurally OK, but contains a known phrase)
    phrase = "Oops, that didn't go to plan"
    html_with_phrase = ("<html><body>" + "x" * 10000
                       + f'<h1>Test</h1><span>£10</span><p>{phrase}</p>'
                       + '<button>Add to basket</button></body></html>')
    r = detect_invalid_page(html_with_phrase, 200, "tesco", phrases=[phrase])
    check("Known invalid-target phrase triggers keyword_match",
          r is not None and r.signal_type == "keyword_match", str(r))

    # Same page, no phrase list -> should pass
    r = detect_invalid_page(html_with_phrase, 200, "tesco", phrases=None)
    check("Without phrase list, keyword layer doesn't fire",
          r is None, str(r))


# ---------------------------------------------------------------------------
# M5.3 — HTMLScraper integration (extraction stubbed, detection real)
# ---------------------------------------------------------------------------

def verify_html_scraper_integration() -> None:
    section("M5.3 - HTMLScraper end-to-end with stubbed extraction")

    import asyncio
    import os
    import tempfile
    from unittest.mock import patch

    os.environ["SCRAPING_DB_PATH"] = os.path.join(tempfile.gettempdir(), "verify_m5.db")
    if os.path.exists(os.environ["SCRAPING_DB_PATH"]):
        os.remove(os.environ["SCRAPING_DB_PATH"])

    # Reload config to pick up env var
    from src.scraping import config as config_module
    config_module._config = None

    from src.scraping.exceptions import ScrapeFailed
    from src.scraping.models.results import InvalidTargetResult
    from src.scraping.scrapers.sites.argos import ArgosScraper

    # Case 1: valid Argos HTML should get past detection.
    # Post-M6/M8: empty parser list falls through to repair ladder. To test detection
    # in isolation, we patch the repair ladder + parser list to short-circuit.
    argos_html = (DATA_DIR / "argos_response_1.html").read_text(encoding="utf-8")

    from unittest.mock import AsyncMock

    async def run_valid_case():
        scraper = ArgosScraper()
        with patch.object(scraper, "_get_unlocker") as mock_unlocker, \
             patch("src.scraping.repair.agent.run_repair_ladder",
                   new=AsyncMock(return_value=ScrapeFailed(
                       site="argos", url="x", scraper_name="ArgosScraper",
                       failed_stage="parser_broken", errors=["stub"]))):
            mock_unlocker.return_value.fetch = _make_async_return((200, argos_html))
            try:
                return await scraper.scrape("https://www.argos.co.uk/product/3284476")
            except ScrapeFailed as e:
                return e

    result = asyncio.run(run_valid_case())
    check("Valid HTML passes detection (does not become InvalidTargetResult)",
          not isinstance(result, InvalidTargetResult),
          f"got {type(result).__name__}")

    # Case 2: 404 -> should return InvalidTargetResult, not raise
    async def run_404_case():
        scraper = ArgosScraper()
        with patch.object(scraper, "_get_unlocker") as mock_unlocker:
            mock_unlocker.return_value.fetch = _make_async_return((404, "<html>Not Found</html>"))
            return await scraper.scrape("https://www.argos.co.uk/product/9999999")

    result = asyncio.run(run_404_case())
    check("404 URL -> InvalidTargetResult (not exception)",
          isinstance(result, InvalidTargetResult), str(type(result).__name__))
    if isinstance(result, InvalidTargetResult):
        check("InvalidTargetResult carries http_status signal",
              "http_status" in result.reason_signal, result.reason_signal)

    # Cleanup
    if os.path.exists(os.environ["SCRAPING_DB_PATH"]):
        os.remove(os.environ["SCRAPING_DB_PATH"])


def _make_async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    print("Verification script for M4 + M5")
    print(f"Data directory: {DATA_DIR}")

    verifiers = [
        verify_amazon_mapping,
        verify_argos_dca_mapping,
        verify_tesco_dca_mapping,
        verify_api_not_found,
        verify_detection_on_real_html,
        verify_detection_signals,
        verify_html_scraper_integration,
    ]

    for fn in verifiers:
        try:
            fn()
        except Exception:
            FAILED.append((fn.__name__, "EXCEPTION"))
            print(f"  [EXCEPTION] {fn.__name__}")
            traceback.print_exc()

    print()
    print("=" * 70)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 70)
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name}  ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

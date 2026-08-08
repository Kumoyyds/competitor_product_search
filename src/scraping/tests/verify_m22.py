"""Offline verification for M22 unit-price field removal and API-route guard.

Run from the repository root:
    python src/scraping/tests/verify_m22.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
FIXTURE_DIR = Path(__file__).parent.parent / "data" / "html_sample"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def verify_model_and_amazon_mapping() -> None:
    from src.scraping.models.product_data import ProductData
    from src.scraping.scrapers.sites.amazon_uk import AmazonUKScraper

    section("M22.1 - ProductData schema and Amazon mapping")
    model_fields = set(ProductData.model_fields)
    check(
        "ProductData omits unit_price and unit",
        not {"unit_price", "unit"} & model_fields,
        str(sorted({"unit_price", "unit"} & model_fields)),
    )

    product = ProductData(
        url="https://example.test/product",
        website="amazon",
        scraped_at=datetime.now(timezone.utc),
        source_type="api",
        title="Example",
        image_urls=[],
        price=Decimal("2.99"),
        in_stock=True,
        unit_price=Decimal("1.50"),
        unit="kg",
    )
    dumped = product.model_dump()
    check(
        "legacy constructor extras do not surface",
        "unit_price" not in dumped and "unit" not in dumped,
        str(dumped),
    )

    amazon_data = ast.literal_eval(
        (FIXTURE_DIR / "amazon_response.json").read_text(encoding="utf-8")
    )
    scraper = object.__new__(AmazonUKScraper)
    mapped = scraper._map_fields(
        amazon_data, "https://www.amazon.de/dp/B0C62DWSDL"
    )
    check(
        "Amazon mapping omits unit_price and unit",
        "unit_price" not in mapped and "unit" not in mapped,
    )
    check(
        "Amazon canonical prices remain unchanged",
        mapped["price"] == Decimal("23.79")
        and mapped["list_price"] == Decimal("27.99")
        and mapped["currency"] == "EUR",
        f"{mapped['price']} / {mapped['list_price']} / {mapped['currency']}",
    )
    check(
        "Amazon raw payload remains available",
        mapped["raw"] is amazon_data
        and "unit_price" in amazon_data.get("buybox_prices", {}),
    )


def verify_predicate() -> None:
    from src.scraping.repair.json_healer import _is_unit_price_source

    section("M22.2 - deterministic unit-price source predicate")
    blocked = (
        ("price", "buybox_prices.unit_price", "668,26€ / kg"),
        ("list_price", "x.price_per_unit", "1.50"),
        ("price", "x.offer", "668,26€ / kg"),
        ("price", "x.offer", "£1.50/100g"),
        ("membership_price", "x.offer", "2.99 GBP per litre"),
    )
    allowed = (
        ("price", "buybox_prices.final_price", "23.79"),
        ("membership_price", "prime_price", "19.99"),
        ("availability_raw", "buybox_prices.unit_price", "668,26€ / kg"),
    )
    for target, source_path, value in blocked:
        check(
            f"blocks {target} <- {source_path} ({value})",
            _is_unit_price_source(target, source_path, value),
        )
    for target, source_path, value in allowed:
        check(
            f"allows {target} <- {source_path} ({value})",
            not _is_unit_price_source(target, source_path, value),
        )


class _StubLLM:
    def __init__(self, *responses: dict):
        self._responses = list(responses)

    async def ainvoke(self, _prompt):
        return SimpleNamespace(content=json.dumps(self._responses.pop(0)))


async def verify_healer_and_cache() -> None:
    from src.scraping.repair import json_healer
    from src.scraping.scrapers.api_scraper import DirectAPIScraper
    from src.scraping.scrapers.sites.amazon_uk import AmazonUKScraper

    section("M22.3 - healer and cached-remap enforcement")
    poisoned_json = {
        "buybox_prices": {"unit_price": "668,26€ / kg"},
        "final_price": "23.79",
    }
    original_price = Decimal("23.79")
    with patch.object(
        json_healer,
        "_make_llm",
        return_value=_StubLLM(
            {"decision": "source_present", "reason": "stub"},
            {"mapping": {"price": "buybox_prices.unit_price"}},
        ),
    ), patch.object(json_healer.logger, "warning") as warning:
        healed = await json_healer.heal_json(
            poisoned_json,
            {"price": original_price},
            ["price missing"],
            "amazon",
        )
    check(
        "healer skips poisoned unit-price remap",
        healed is not None and healed.get("price") == original_price,
        str(healed),
    )
    check("healer logs poisoned remap", warning.called)

    with patch.object(
        json_healer,
        "_make_llm",
        return_value=_StubLLM(
            {"decision": "source_present", "reason": "stub"},
            {"mapping": {"price": "final_price"}},
        ),
    ):
        healed = await json_healer.heal_json(
            poisoned_json,
            {"price": None},
            ["price missing"],
            "amazon",
        )
    check(
        "healer still applies legitimate remap",
        healed is not None and healed.get("price") == "23.79",
        str(healed),
    )

    scraper = object.__new__(AmazonUKScraper)
    poisoned_cache = {"amazon": {"price": "buybox_prices.unit_price"}}
    with patch.object(DirectAPIScraper, "_json_heal_cache", poisoned_cache):
        cached = scraper._apply_heal_cache(poisoned_json, {"price": None})
    check(
        "cached poisoned remap leaves price untouched",
        cached.get("price") is None,
        str(cached),
    )


def verify_prompts() -> None:
    from src.scraping.repair.prompts import (
        SCHEMA_HINT,
        json_heal_precheck_prompt,
        json_heal_remap_prompt,
        parser_gen_prompt,
    )

    section("M22.4 - schema and prompt guardrails")
    check(
        "SCHEMA_HINT omits unit-price targets",
        "  unit_price (" not in SCHEMA_HINT and "\n  unit (str)" not in SCHEMA_HINT,
    )

    context = SimpleNamespace(
        canonical_title="Example",
        url_product_id="1",
        promotion_signal=None,
        json_ld_blocks=[],
        price_evidence=[],
        unit_price_evidence=[],
        head_excerpt="",
        main_excerpt="<main>Example</main>",
    )
    parser_text = json.dumps(
        parser_gen_prompt(context, "example", "first", [], []),
        ensure_ascii=False,
    )
    check(
        "parser prompt keeps negative unit-price instruction",
        "NOT product prices" in parser_text
        and "never put them in `price`, `list_price`, or `membership_price`" in parser_text,
    )

    payload = {"buybox_prices": {"unit_price": "668,26€ / kg"}}
    precheck_text = json.dumps(
        json_heal_precheck_prompt(payload, ["price"]), ensure_ascii=False
    )
    remap_text = json.dumps(
        json_heal_remap_prompt(payload, ["price"]), ensure_ascii=False
    )
    check(
        "precheck prompt rejects unit-price-only payloads",
        "per-unit rate" in precheck_text and "source_absent" in precheck_text,
    )
    check(
        "remap prompt forbids unit-price mappings",
        "Unit-price keys" in remap_text
        and "NEVER map them" in remap_text
        and "membership_price" in remap_text,
    )


async def run() -> None:
    verify_model_and_amazon_mapping()
    verify_predicate()
    await verify_healer_and_cache()
    verify_prompts()


def main() -> int:
    try:
        asyncio.run(run())
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()

    print()
    print("=" * 74)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 74)
    for name, detail in FAILED:
        print(f"  FAILED: {name}  ({detail})")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

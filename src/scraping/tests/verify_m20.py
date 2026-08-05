"""M20 verification: canonical price-field contract.

Offline coverage for standard / discounted / membership combinations, the
ordering rules in Gate 2, cold-start feedback normalization, and prompt text.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from decimal import Decimal

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


def raw_product(**overrides):
    values = {
        "url": "https://example.test/product",
        "website": "test",
        "scraped_at": datetime.now(timezone.utc),
        "source_type": "html",
        "title": "Product",
        "image_urls": [],
        "in_stock": True,
        "price": Decimal("10.00"),
        "currency": "GBP",
    }
    values.update(overrides)
    return values


def verify_price_contract() -> None:
    from src.scraping.repair.golden import classify_page_type
    from src.scraping.validation import validate

    section("M20.1 - Gate 2 price combinations")
    standard, errors = validate(raw_product())
    check(
        "standard price-only product passes",
        standard is not None and not errors and classify_page_type(standard) == "standard",
        str(errors),
    )

    discounted, errors = validate(raw_product(list_price=Decimal("12.00")))
    check(
        "discount needs higher list_price and classifies discounted",
        discounted is not None and not errors
        and classify_page_type(discounted) == "discounted",
        str(errors),
    )

    membership, errors = validate(raw_product(membership_price=Decimal("8.00")))
    check(
        "membership needs lower membership_price and classifies membership",
        membership is not None and not errors
        and classify_page_type(membership) == "membership",
        str(errors),
    )

    triple, errors = validate(
        raw_product(list_price=Decimal("12.00"), membership_price=Decimal("8.00"))
    )
    check(
        "membership may also carry a higher list_price",
        triple is not None and not errors
        and classify_page_type(triple) == "membership",
        str(errors),
    )

    _, errors = validate(raw_product(list_price=Decimal("10.00")))
    check(
        "equal list_price is rejected",
        errors and "list_price must be greater" in errors[0],
        str(errors),
    )

    _, errors = validate(raw_product(list_price=Decimal("8.00")))
    check(
        "lower list_price is rejected",
        errors and "list_price must be greater" in errors[0],
        str(errors),
    )

    _, errors = validate(raw_product(membership_price=Decimal("10.00")))
    check(
        "equal membership_price is rejected",
        errors and "membership_price must be lower" in errors[0],
        str(errors),
    )

    _, errors = validate(raw_product(membership_price=Decimal("12.00")))
    check(
        "higher membership_price is rejected",
        errors and "membership_price must be lower" in errors[0],
        str(errors),
    )

    _, errors = validate(raw_product(membership_price=Decimal("0.00")))
    check(
        "zero membership_price is rejected",
        errors and "membership_price must be positive" in errors[0],
        str(errors),
    )

    _, errors = validate(raw_product(price=None, membership_price=Decimal("8.00")))
    check(
        "in-stock membership product still requires ordinary price",
        errors and "price must be a positive ordinary" in errors[0],
        str(errors),
    )

    oos, errors = validate(
        raw_product(
            in_stock=False,
            price=None,
            membership_price=Decimal("8.00"),
        )
    )
    check(
        "out-of-stock product may retain membership price without price",
        oos is not None and not errors,
        str(errors),
    )


def verify_prompt_and_feedback() -> None:
    from src.scraping.coldstart import _normalize_correction_value
    from src.scraping.repair.prepass import build_price_aware_context
    from src.scraping.repair.prompts import parser_gen_prompt

    section("M20.2 - prompt and cold-start feedback contract")
    context = build_price_aware_context(
        "<html><h1>Product</h1><span>£10.00</span></html>",
        "https://example.test/product",
    )
    messages = parser_gen_prompt(
        context, "test", role="first", initial_errors=[], attempts=[]
    )
    prompt = "\n".join(message["content"] for message in messages)
    check(
        "prompt names standard/discounted/membership combinations",
        all(text in prompt for text in (
            "standard product: price only",
            "normal discounted product: price + list_price",
            "membership product: price + membership_price",
        )),
    )
    check(
        "prompt states strict ordering",
        "list_price > price > membership_price" in prompt,
    )
    check(
        "dash correction means clear/omit",
        _normalize_correction_value("-") == "None (clear or omit this field)",
    )
    check(
        "None correction means clear/omit",
        _normalize_correction_value("NONE") == "None (clear or omit this field)",
    )


def main() -> int:
    try:
        verify_price_contract()
        verify_prompt_and_feedback()
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


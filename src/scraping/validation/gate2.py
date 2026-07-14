from __future__ import annotations

from typing import Callable

from ..models.product_data import ProductData

FeasibleRule = Callable[[ProductData], str | None]

_SITE_RULES: dict[str, list[FeasibleRule]] = {}


def register_feasible_rule(site: str, rule: FeasibleRule) -> None:
    _SITE_RULES.setdefault(site, []).append(rule)


def _core_price_rule(data: ProductData) -> str | None:
    """in_stock=True must be paired with a positive price signal (D13).

    Accepts price OR list_price (either one > 0 qualifies).  A parser that
    correctly extracts list_price but misses the current-price DOM node
    should still pass — we never silently reassign list_price → price,
    because on discount pages that would record the RRP as the current
    price.  The gate merely checks that *some* positive price exists.

    Still rejects:
      - Both None
      - A hallucinated 0.0 even when the other field carries a real value
    """
    if not data.in_stock:
        return None
    has_price = data.price is not None and data.price > 0
    has_list = data.list_price is not None and data.list_price > 0
    if not (has_price or has_list):
        return "in_stock=True but no positive price or list_price"
    return None


def _out_of_stock_signal_rule(data: ProductData) -> str | None:
    """Out-of-stock products must still exhibit at least one product-page signal.

    A ProductData with `in_stock=False` and NO price, NO list_price, and NO
    image_urls is more likely an error / soft-block / "Something went wrong"
    page than a legitimate out-of-stock product. Real retail out-of-stock
    pages still show product imagery and usually a historical price. Rejects
    "title-only" extractions that slipped past the parser when the LLM's
    v-N parser matched a generic `<h1>` on an error page.
    """
    if data.in_stock:
        return None
    has_image = bool(data.image_urls)
    has_price = data.price is not None or data.list_price is not None
    if not has_image and not has_price:
        return (
            "in_stock=False and no product signals present "
            "(image_urls empty, price=None, list_price=None) — "
            "likely an error page, not a real product"
        )
    return None


def feasible_check(data: ProductData) -> list[str]:
    """Gate 2: cross-field semantic validation.

    Returns empty list on pass, or list of violation descriptions on fail.
    """
    violations: list[str] = []

    core = _core_price_rule(data)
    if core:
        violations.append(core)

    oos = _out_of_stock_signal_rule(data)
    if oos:
        violations.append(oos)

    for rule in _SITE_RULES.get(data.website, []):
        result = rule(data)
        if result:
            violations.append(result)

    return violations

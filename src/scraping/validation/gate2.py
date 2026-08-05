from __future__ import annotations

from typing import Callable

from ..models.product_data import ProductData

FeasibleRule = Callable[[ProductData], str | None]

_SITE_RULES: dict[str, list[FeasibleRule]] = {}


def register_feasible_rule(site: str, rule: FeasibleRule) -> None:
    _SITE_RULES.setdefault(site, []).append(rule)


def _structural_price_rule(data: ProductData) -> str | None:
    """Reject price-field assignments that are structurally impossible.

    Price fields have distinct customer-context meanings:
    - ``price`` is the ordinary non-member current price;
    - ``list_price`` is a higher Was/RRP reference price;
    - ``membership_price`` is a lower, membership-gated price.

    Rejecting impossible orderings here (route-agnostic) causes the fast path to
    fall through to the repair ladder, and prevents a golden promotion that
    would lock a semantic mapping error in.
    """
    if data.price is not None:
        if data.list_price is not None and data.list_price <= data.price:
            return (
                "list_price must be greater than price — list_price is only a "
                "higher Was/RRP reference price, not the current/member price"
            )
        if (
            data.membership_price is not None
            and data.membership_price >= data.price
        ):
            return (
                "membership_price must be lower than price — membership_price "
                "is a gated discount, not the ordinary price"
            )
    if data.membership_price is not None and data.membership_price <= 0:
        return "membership_price must be positive when present"
    return None


def _core_price_rule(data: ProductData) -> str | None:
    """Require the ordinary current price for every in-stock product (D13).

    ``list_price`` and ``membership_price`` qualify the ordinary price; neither
    replaces it. This gives every in-stock product one stable comparable price,
    and keeps the page-type combinations unambiguous:
    standard = price; discounted = price + list_price; membership = price +
    membership_price (+ optional list_price).
    """
    if not data.in_stock:
        return None
    if data.price is None or data.price <= 0:
        return "in_stock=True but price must be a positive ordinary customer price"
    return None


def _out_of_stock_signal_rule(data: ProductData) -> str | None:
    """Out-of-stock products must still exhibit at least one product-page signal.

    A ProductData with `in_stock=False` and NO price, NO list_price, NO
    membership_price, and NO image_urls is more likely an error / soft-block /
    "Something went wrong" page than a legitimate out-of-stock product. Real
    retail out-of-stock pages still show product imagery and usually a historical
    price. Rejects "title-only" extractions that slipped past the parser when
    the LLM's v-N parser matched a generic `<h1>` on an error page.
    """
    if data.in_stock:
        return None
    has_image = bool(data.image_urls)
    has_price = (
        data.price is not None
        or data.list_price is not None
        or data.membership_price is not None
    )
    if not has_image and not has_price:
        return (
            "in_stock=False and no product signals present "
            "(image_urls empty, price=None, list_price=None, membership_price=None) — "
            "likely an error page, not a real product"
        )
    return None


def feasible_check(data: ProductData) -> list[str]:
    """Gate 2: cross-field semantic validation.

    Returns empty list on pass, or list of violation descriptions on fail.
    """
    violations: list[str] = []

    struct = _structural_price_rule(data)
    if struct:
        violations.append(struct)

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

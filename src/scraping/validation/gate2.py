from __future__ import annotations

from typing import Callable

from ..models.product_data import ProductData

FeasibleRule = Callable[[ProductData], str | None]

_SITE_RULES: dict[str, list[FeasibleRule]] = {}


def register_feasible_rule(site: str, rule: FeasibleRule) -> None:
    _SITE_RULES.setdefault(site, []).append(rule)


def _core_price_rule(data: ProductData) -> str | None:
    """in_stock=True and price=None is a fault (D13)."""
    if data.in_stock and data.price is None:
        return "in_stock=True but price is missing"
    return None


def feasible_check(data: ProductData) -> list[str]:
    """Gate 2: cross-field semantic validation.

    Returns empty list on pass, or list of violation descriptions on fail.
    """
    violations: list[str] = []

    core = _core_price_rule(data)
    if core:
        violations.append(core)

    for rule in _SITE_RULES.get(data.website, []):
        result = rule(data)
        if result:
            violations.append(result)

    return violations

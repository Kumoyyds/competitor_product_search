"""Canonical price-field cleanup for hand-written API mappings."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def normalize_price_fields(mapped: dict) -> tuple[dict, list[str]]:
    """Drop price qualifiers that violate the M20 canonical contract.

    Mirrors ``gate2._structural_price_rule``, including its ``price is not
    None`` guard: when price is absent, list and membership prices remain
    available as possible out-of-stock product signals. A non-positive
    membership price is always removed.
    """
    normalized = dict(mapped)
    dropped: list[str] = []

    price = _decimal(normalized.get("price"))
    list_price = _decimal(normalized.get("list_price"))
    membership_price = _decimal(normalized.get("membership_price"))

    if price is not None and list_price is not None and list_price <= price:
        relation = "== price" if list_price == price else "< price"
        dropped.append(f"list_price={normalized.get('list_price')} ({relation})")
        normalized["list_price"] = None

    if membership_price is not None and membership_price <= 0:
        dropped.append(
            f"membership_price={normalized.get('membership_price')} (<= 0)"
        )
        normalized["membership_price"] = None
    elif (
        price is not None
        and membership_price is not None
        and membership_price >= price
    ):
        relation = "== price" if membership_price == price else "> price"
        dropped.append(
            f"membership_price={normalized.get('membership_price')} ({relation})"
        )
        normalized["membership_price"] = None

    return normalized, dropped

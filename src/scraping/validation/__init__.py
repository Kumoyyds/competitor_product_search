from __future__ import annotations

from typing import Any

from ..models.product_data import ProductData
from .gate1 import gate1_validate
from .gate2 import feasible_check


def validate(data: dict[str, Any]) -> tuple[ProductData | None, list[str]]:
    """Run both gates. Returns (ProductData, []) on pass, or (None, errors) on fail."""
    result = gate1_validate(data)
    if isinstance(result, list):
        return None, result

    violations = feasible_check(result)
    if violations:
        return None, violations

    return result, []


__all__ = ["validate", "gate1_validate", "feasible_check"]

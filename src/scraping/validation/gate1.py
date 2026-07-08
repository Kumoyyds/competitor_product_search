from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..models.product_data import ProductData


def gate1_validate(data: dict[str, Any]) -> ProductData | list[str]:
    """Gate 1: Pydantic type/structure validation.

    Returns ProductData on success, or a list of error messages on failure.
    price is optional at this layer (intentionally passes out-of-stock products).
    """
    try:
        return ProductData(**data)
    except ValidationError as e:
        return [f"{err['loc']}: {err['msg']}" for err in e.errors()]

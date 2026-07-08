from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .product_data import ProductData


@dataclass(frozen=True)
class InvalidTargetResult:
    """Sentinel: URL is reachable but does not correspond to a valid product (D27)."""

    url: str
    site: str
    reason_signal: str


ScrapeOutcome = Union[ProductData, InvalidTargetResult]

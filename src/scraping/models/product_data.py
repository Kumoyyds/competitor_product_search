from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from .enums import SourceType

# Schema.org availability URL tokens → canonical human labels (W3C-standard vocabulary)
_SCHEMA_ORG_AVAILABILITY_MAP: dict[str, str] = {
    "instock":          "In stock",
    "outofstock":       "Out of stock",
    "preorder":         "Pre-order",
    "backorder":        "On back order",
    "discontinued":     "Discontinued",
    "soldout":          "Sold out",
    "limitedavailability": "Limited availability",
    "instoreonly":      "In store only",
    "onlineonly":       "Online only",
}


def _normalize_availability(raw: Optional[str], in_stock: bool) -> str:
    """Collapse a bad/blobby availability_raw into a canonical short label.

    Schema.org availability tokens (W3C-standard, site-agnostic) are recovered
    from anywhere inside the string.  Falls back to deriving from `in_stock`.
    """
    if raw and len(raw) <= 80 and "{" not in raw and "@context" not in raw:
        return raw  # already a short human label — keep it

    # Try to recover from an embedded schema.org availability URL token
    if raw:
        raw_lower = raw.lower()
        for token, label in _SCHEMA_ORG_AVAILABILITY_MAP.items():
            if token in raw_lower:
                return label

    # Fallback: derive from the already-trustworthy in_stock bool
    return "In stock" if in_stock else "Out of stock"


class ProductData(BaseModel):
    # --- tracing ---
    url: str
    website: str
    scraped_at: datetime
    source_type: SourceType
    parser_version: Optional[str] = None

    # --- identification ---
    title: str
    brand: Optional[str] = None
    gtin: Optional[str] = None
    image_urls: list[str] = Field(default_factory=list)
    variant: Optional[dict] = None

    # --- price (D1: Decimal only, float forbidden) ---
    price: Optional[Decimal] = None
    currency: Optional[str] = None
    list_price: Optional[Decimal] = None
    membership_price: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    unit: Optional[str] = None

    # --- stock ---
    in_stock: bool
    availability_raw: Optional[str] = None

    # --- debug ---
    raw: Optional[dict] = None

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def _sanitize_availability(self) -> "ProductData":
        """Normalize availability_raw so a parser can never surface a JSON blob.

        Safety net for both HTML and API routes — independent of parser quality.
        """
        self.availability_raw = _normalize_availability(
            self.availability_raw, self.in_stock
        )
        return self

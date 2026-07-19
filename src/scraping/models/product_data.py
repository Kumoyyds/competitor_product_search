from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from .enums import SourceType


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

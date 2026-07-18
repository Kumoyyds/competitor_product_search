from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

logger = logging.getLogger(__name__)

from ...extraction import BrightDataDatasets, with_extraction_retry
from ...registry import register_scraper
from ..api_scraper import DirectAPIScraper


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_unit_price(raw: Any) -> tuple[Optional[Decimal], Optional[str]]:
    """Parse BrightData unit_price string like '668,26€ / kg' or '668.26€/kg'."""
    if not raw or not isinstance(raw, str):
        return None, None
    import re

    m = re.search(r"([\d.,]+)\s*[^\d/]*\s*/\s*(\w+)", raw)
    if not m:
        return None, None
    price_str = m.group(1).replace(",", ".")
    unit = m.group(2)
    return _to_decimal(price_str), unit


@register_scraper("amazon", order=1)
class AmazonUKScraper(DirectAPIScraper):
    source_type = "api"

    def __init__(self):
        self._client = BrightDataDatasets()

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        snapshot_id = await with_extraction_retry(self._client._trigger, url)
        return await self._client._poll(snapshot_id)

    def _is_not_found(self, json_data: dict[str, Any]) -> bool:
        if json_data.get("error"):
            return True
        if not json_data.get("title"):
            logger.warning(
                "Amazon BD extraction dropped as invalid_target: no title; present keys=%s",
                list(json_data.keys()),
            )
            return True
        return False

    def _map_fields(self, json_data: dict[str, Any], url: str) -> dict[str, Any]:
        unit_price, unit = _parse_unit_price(
            (json_data.get("buybox_prices") or {}).get("unit_price")
        )

        variant = None
        variations = json_data.get("variations")
        variant_attrs = json_data.get("variant_attributes")
        if variant_attrs:
            variant = {}
            for attr in variant_attrs:
                name = attr.get("name", "").lower()
                val = attr.get("value")
                if "size" in name or "größe" in name:
                    variant["size"] = val
                elif "color" in name or "colour" in name or "farbe" in name:
                    variant["color"] = val

        image_urls = json_data.get("images") or []
        if not image_urls:
            single = json_data.get("image_url") or json_data.get("image")
            if single:
                image_urls = [single]

        return {
            "url": json_data.get("url", url),
            "website": "amazon",
            "scraped_at": datetime.now(timezone.utc),
            "source_type": "api",
            "title": json_data.get("title", ""),
            "brand": json_data.get("brand"),
            "gtin": json_data.get("upc"),
            "image_urls": image_urls,
            "variant": variant,
            "price": _to_decimal(json_data.get("final_price")),
            "currency": json_data.get("currency"),
            "list_price": _to_decimal(json_data.get("initial_price")),
            "unit_price": unit_price,
            "unit": unit,
            "in_stock": bool(json_data.get("is_available", False)),
            "availability_raw": json_data.get("availability"),
            "raw": json_data,
        }

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from ...extraction import BrightDataDCA, with_extraction_retry
from ...registry import register_scraper
from ..api_scraper import DirectAPIScraper


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


@register_scraper("tesco", order=2)
class TescoDCAScraper(DirectAPIScraper):
    """Tesco DCA backup route (scraper-level fallback)."""

    source_type = "api"

    def __init__(self):
        self._client = BrightDataDCA()

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        collection_id = await with_extraction_retry(self._client._trigger, url)
        return await self._client._poll(collection_id)

    def _is_not_found(self, json_data: dict[str, Any]) -> bool:
        if not json_data.get("product_name"):
            return True
        return False

    def _map_fields(self, json_data: dict[str, Any], url: str) -> dict[str, Any]:
        current_price = json_data.get("current_price")
        price = None
        currency = json_data.get("currency")
        if isinstance(current_price, dict):
            price = _to_decimal(current_price.get("value"))
            currency = current_price.get("currency", currency)
        else:
            price = _to_decimal(current_price)

        original_price = json_data.get("original_price")
        list_price = None
        if isinstance(original_price, dict):
            list_price = _to_decimal(original_price.get("value"))
        else:
            list_price = _to_decimal(original_price)

        image_urls = []
        img = json_data.get("product_image")
        if isinstance(img, list):
            image_urls = img
        elif isinstance(img, str) and img:
            image_urls = [img]

        return {
            "url": (json_data.get("input") or {}).get("url", url),
            "website": "tesco",
            "scraped_at": datetime.now(timezone.utc),
            "source_type": "api",
            "title": json_data.get("product_name", ""),
            "brand": json_data.get("brand"),
            "image_urls": image_urls,
            "price": price,
            "currency": currency,
            "list_price": list_price,
            "in_stock": bool(json_data.get("in_stock", False)),
            "availability_raw": json_data.get("availability"),
            "raw": json_data,
        }

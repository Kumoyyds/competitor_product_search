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


@register_scraper("argos", order=2)
class ArgosDCAScraper(DirectAPIScraper):
    """Argos DCA backup route (scraper-level fallback)."""

    source_type = "api"

    def __init__(self):
        self._client = BrightDataDCA(collector_id="c_mrkepcie19jse9x5xb")

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        collection_id = await with_extraction_retry(self._client._trigger, url)
        return await self._client._poll(collection_id)

    def _is_not_found(self, json_data: dict[str, Any]) -> bool:
        return not json_data.get("product_title")

    def _map_fields(self, json_data: dict[str, Any], url: str) -> dict[str, Any]:
        current_price = json_data.get("price")
        price = None
        currency = json_data.get("currency")
        if isinstance(current_price, dict):
            price = _to_decimal(current_price.get("value"))
            currency = current_price.get("currency", currency)
        else:
            price = _to_decimal(current_price)

        original_price = json_data.get("list_price")
        list_price = None
        if isinstance(original_price, dict):
            list_price = _to_decimal(original_price.get("value"))
        else:
            list_price = _to_decimal(original_price)

        image_urls = []
        images = json_data.get("image_urls")
        if isinstance(images, list):
            image_urls = [img for img in images if isinstance(img, str)]
        elif isinstance(images, str) and images:
            image_urls = [images]

        return {
            "url": (json_data.get("input") or {}).get("url", url),
            "website": "argos",
            "scraped_at": datetime.now(timezone.utc),
            "source_type": "api",
            "title": json_data.get("product_title", ""),
            "brand": None,
            "image_urls": image_urls,
            "price": price,
            "currency": currency,
            "list_price": list_price,
            "in_stock": bool(json_data.get("in_stock", False)),
            "availability_raw": None,
            "raw": json_data,
        }
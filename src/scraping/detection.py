"""Invalid page detection tool (spec §5.15, D20/D26).

Reused in two places:
  1. Pre-detection (after extraction, before parser) — primary interception
  2. Agent fallback judgment (in repair ladder) — catches new patterns

Signal priority (structural signals primary, keywords auxiliary):
  1. JSON-LD Product schema presence (strongest)
  2. HTTP status code (404/410 etc.)
  3. Multiple structural absence combination (title + price + add-to-cart)
  4. Page length anomaly
  5. Keyword matching (from invalid_target_phrases table)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from .config import get_config

logger = logging.getLogger(__name__)

_NORMAL_PAGE_MIN_LENGTH = 5000

_ADD_TO_CART_PATTERNS = [
    re.compile(r'add[- _]?to[- _]?(?:cart|bag|basket|trolley)', re.IGNORECASE),
    re.compile(r'buy[- _]?now', re.IGNORECASE),
    re.compile(r'in den (?:Einkaufs)?wagen', re.IGNORECASE),
    re.compile(r'ajouter au panier', re.IGNORECASE),
]

_TITLE_SELECTORS = [
    re.compile(r'<h1[^>]*>([^<]+)</h1>', re.IGNORECASE),
    re.compile(r'data-test="product-title"', re.IGNORECASE),
    re.compile(r'data-auto="pdp-product-title"', re.IGNORECASE),
    re.compile(r'"name"\s*:\s*"[^"]{3,}"', re.IGNORECASE),
]

_PRICE_PATTERNS = [
    re.compile(r'[£€$]\s*\d+[\d,.]*', re.IGNORECASE),
    re.compile(r'\d+[\d,.]*\s*[£€$]', re.IGNORECASE),
    re.compile(r'"price"\s*:\s*[\d.]+', re.IGNORECASE),
    re.compile(r'data-test="product-price', re.IGNORECASE),
    re.compile(r'data-auto="price-value', re.IGNORECASE),
]


@dataclass(frozen=True)
class InvalidPageSignal:
    signal_type: str
    detail: str


def _check_type_product(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("@type") == "Product":
        return True
    for item in obj.get("@graph", []):
        if isinstance(item, dict) and item.get("@type") == "Product":
            return True
    return False


def _has_jsonld_product(html: str) -> bool:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict):
                    if _check_type_product(data):
                        return True
                elif isinstance(data, list):
                    for item in data:
                        if _check_type_product(item):
                            return True
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception:
        pass
    return False


def _has_title(html: str) -> bool:
    return any(p.search(html) for p in _TITLE_SELECTORS)


def _has_price(html: str) -> bool:
    return any(p.search(html) for p in _PRICE_PATTERNS)


def _has_add_to_cart(html: str) -> bool:
    return any(p.search(html) for p in _ADD_TO_CART_PATTERNS)


def detect_invalid_page(
    html: str,
    status_code: int,
    site: str,
    phrases: Optional[list[str]] = None,
) -> Optional[InvalidPageSignal]:
    """Detect if a page is an invalid target (not a valid product page).

    Returns InvalidPageSignal if invalid, None if page looks valid.
    """
    # Signal 2: HTTP status code (before content checks)
    if status_code in (404, 410, 403, 451):
        return InvalidPageSignal("http_status", f"HTTP {status_code}")

    # Signal 1: JSON-LD Product schema presence (strongest positive signal)
    if _has_jsonld_product(html):
        return None

    # Signal 4: Page length anomaly (error pages are typically very short)
    if len(html) < _NORMAL_PAGE_MIN_LENGTH:
        return InvalidPageSignal(
            "page_length", f"Page only {len(html)} chars (min {_NORMAL_PAGE_MIN_LENGTH})"
        )

    # Signal 3: Multiple structural absence
    cfg = get_config()
    absent_count = 0
    absent_items = []
    if not _has_title(html):
        absent_count += 1
        absent_items.append("title")
    if not _has_price(html):
        absent_count += 1
        absent_items.append("price")
    if not _has_add_to_cart(html):
        absent_count += 1
        absent_items.append("add_to_cart")

    if absent_count >= cfg.invalid_target_absence_threshold:
        return InvalidPageSignal(
            "multi_absence", f"Missing: {', '.join(absent_items)}"
        )

    # Signal 5: Keyword matching from invalid_target_phrases table
    if phrases:
        html_lower = html.lower()
        for phrase in phrases:
            if phrase.lower() in html_lower:
                return InvalidPageSignal("keyword_match", f"Matched phrase: {phrase}")

    return None

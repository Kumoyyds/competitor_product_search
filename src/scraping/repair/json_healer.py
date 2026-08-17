"""Restricted JSON self-healing for the API route (spec §5.14, D25 red line).

Only remaps fields that ALREADY EXIST in the JSON. Fabrication of missing fields is
strictly forbidden — enforced at three layers:
  1. Prompt explicitly lists JSON's actual keys
  2. Response schema requires dotted source paths
  3. Post-response validation confirms each source path resolves to a non-None value
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ..config import get_config
from ..providers import make_chat_client
from .prompts import json_heal_precheck_prompt, json_heal_remap_prompt

logger = logging.getLogger(__name__)

_PRICE_TARGETS = {"price", "list_price", "membership_price"}

# Source-key names that denote a per-unit rate, not a product price.
_UNIT_PRICE_KEY_RE = re.compile(
    r"(?i)(unit[_\-\s]?price|price[_\-\s]?per[_\-\s]?unit|"
    r"per[_\-\s]?unit|unit[_\-\s]?cost|\bppu\b)"
)

# Value shapes like '668,26€ / kg', '£1.50/100g', '2.99 GBP per litre'.
# Deliberately not imported from repair/prepass.py: that regex targets DOM text
# with a leading currency symbol, whereas API payloads may put it after the number.
_UNIT_PRICE_VALUE_RE = re.compile(
    r"""(?ix)
    \d[\d.,]*\s*[£€$]?\s*(?:GBP|EUR|USD)?\s*(?:/|\bper\b)\s*
    (?:kg|g|l|ltr|litre|ml|cl|100\s*g|100\s*ml|oz|lb|each|unit|item|sheet)
    """
)


def _is_unit_price_source(target: str, source_path: str, value: Any) -> bool:
    """Return whether a price target would be fed from a per-unit-rate source."""
    if target not in _PRICE_TARGETS:
        return False
    if isinstance(source_path, str) and _UNIT_PRICE_KEY_RE.search(source_path):
        return True
    return bool(isinstance(value, str) and _UNIT_PRICE_VALUE_RE.search(value))


async def heal_json(
    json_data: dict[str, Any],
    mapped: dict[str, Any],
    errors: list[str],
    site: str,
) -> Optional[dict[str, Any]]:
    """Attempt to remap missing fields from JSON keys that already exist.

    Returns healed dict on success, None if source is genuinely absent or agent proposes
    invalid mapping (D25 violation).
    """
    missing_fields = _extract_missing_fields(errors)
    if not missing_fields:
        return None

    llm = _make_llm()
    if llm is None:
        return None

    # Turn A: source-present vs source-absent decision
    try:
        precheck_raw = await llm.ainvoke(
            json_heal_precheck_prompt(json_data, missing_fields)
        )
    except Exception:
        logger.exception("json_heal precheck LLM call failed")
        return None

    precheck = _parse_json_response(precheck_raw.content if hasattr(precheck_raw, "content") else str(precheck_raw))
    if not precheck or precheck.get("decision") != "source_present":
        logger.info("json_heal: source_absent — not healing (D25 red line respected)")
        return None

    # Turn B: propose remap
    try:
        remap_raw = await llm.ainvoke(
            json_heal_remap_prompt(json_data, missing_fields)
        )
    except Exception:
        logger.exception("json_heal remap LLM call failed")
        return None

    remap = _parse_json_response(remap_raw.content if hasattr(remap_raw, "content") else str(remap_raw))
    if not remap or "mapping" not in remap:
        return None

    mapping: dict[str, str] = remap["mapping"]

    # D25 layer 3: validate every proposed source path
    healed = dict(mapped)
    for target, source_path in mapping.items():
        if target not in missing_fields:
            logger.warning(
                "json_heal: agent proposed mapping for non-missing field %s — ignoring",
                target,
            )
            continue
        value = _lookup_path(json_data, source_path)
        if value is None:
            logger.warning(
                "json_heal D25 VIOLATION: source path %r does not resolve in JSON — rejecting entire mapping",
                source_path,
            )
            return None
        if _is_unit_price_source(target, source_path, value):
            logger.warning(
                "json_heal: refusing unit-price source %r for price field %s (value=%r)",
                source_path,
                target,
                value,
            )
            continue
        healed[target] = value

    logger.info(
        "json_heal: applied mapping site=%s targets=%s", site, list(mapping.keys())
    )
    return healed


def _make_llm():
    cfg = get_config()
    return make_chat_client(
        model=cfg.repair_model_ladder[0],
        temperature=0.1,
        purpose="JSON healer",
    )


def _lookup_path(data: Any, path: str) -> Any:
    """Walk a dotted path (with numeric indices for lists) through a JSON structure."""
    if not isinstance(path, str) or not path:
        return None
    parts = path.split(".")
    cursor = data
    for part in parts:
        if cursor is None:
            return None
        if isinstance(cursor, list):
            try:
                idx = int(part)
                cursor = cursor[idx] if 0 <= idx < len(cursor) else None
            except (ValueError, IndexError):
                return None
        elif isinstance(cursor, dict):
            cursor = cursor.get(part)
        else:
            return None
    return cursor


def _extract_missing_fields(errors: list[str]) -> list[str]:
    """Best-effort extraction of missing/invalid field names from gate errors."""
    missing = set()
    key_fields = {
        "title", "brand", "gtin", "price", "currency", "list_price",
        "membership_price", "in_stock", "image_urls", "variant",
    }
    for err in errors:
        for field in key_fields:
            if field in err.lower():
                missing.add(field)
    return sorted(missing)


def _parse_json_response(text: str) -> Optional[dict[str, Any]]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Try to find embedded JSON
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            return None

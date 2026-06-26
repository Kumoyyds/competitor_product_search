from __future__ import annotations

import re
from typing import Any

from .. import config
from ..models import Verdict


_quantulum_parser = None


def _get_parser():
    global _quantulum_parser
    if _quantulum_parser is None:
        from quantulum3 import parser as _qp  # heavy import
        _quantulum_parser = _qp
    return _quantulum_parser


_ABV_RE = re.compile(r"\bABV\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
_COUNT_X_RE = re.compile(
    r"\b(\d+)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(ml|l|cl|g|kg|mg)\b",
    re.IGNORECASE,
)
_PACK_RE = re.compile(r"\b(?:pack of|pk of)\s*(\d+)\b", re.IGNORECASE)


def _normalize_unit(u: str) -> str:
    return re.sub(r"\s+", "", u.lower()).rstrip(".s")


def _entity_to_attr(entity_name: str) -> str | None:
    mapping = config.get("numeric", "entity_to_attr", default={}) or {}
    return mapping.get(entity_name.lower())


def _disambiguate(entity_name: str, text: str, span: tuple[int, int]) -> str | None:
    rules = config.get("numeric", "ambiguity_rules", default={}) or {}
    rule = rules.get(entity_name.lower())
    if not rule:
        return None
    window = int(config.get("numeric", "ambiguity_window_chars", default=20))
    start = max(0, span[0] - window)
    end = min(len(text), span[1] + window)
    ctx = text[start:end].lower()
    for kw, attr in rule.items():
        if kw.lower() in ctx:
            return attr
    return None


def _convert(value: float, unit_raw: str, attr_key: str) -> float | None:
    table = config.get("numeric", "unit_conversions", attr_key, default={}) or {}
    if not table:
        return None
    u = _normalize_unit(unit_raw)
    factor = table.get(u)
    if factor is None:
        for k, v in table.items():
            if _normalize_unit(k) == u:
                factor = v
                break
    if factor is None:
        return None
    return float(value) * float(factor)


def extract_numerics(text: str) -> dict[str, float]:
    """Return {attr_key: value_in_base_unit} for a product title or candidate text.

    Custom regex fallbacks run before quantulum3 so we don't lose ABV / `N X Nml`
    count patterns to quantulum3's dimensionless bucket.
    """
    if not text:
        return {}
    out: dict[str, float] = {}

    m_abv = _ABV_RE.search(text)
    if m_abv:
        try:
            out["abv_percent"] = float(m_abv.group(1))
        except ValueError:
            pass

    m_count = _COUNT_X_RE.search(text)
    if m_count:
        try:
            count_val = int(m_count.group(1))
            out["count"] = float(count_val)
            unit_val = float(m_count.group(2))
            unit_raw = m_count.group(3)
            for attr in ("volume_ml", "weight_g"):
                v = _convert(unit_val, unit_raw, attr)
                if v is not None:
                    out[attr] = v
                    break
        except (ValueError, TypeError):
            pass

    m_pack = _PACK_RE.search(text)
    if m_pack and "count" not in out:
        try:
            out["count"] = float(int(m_pack.group(1)))
        except ValueError:
            pass

    try:
        parser = _get_parser()
        quantities = parser.parse(text)
    except Exception:
        quantities = []

    for q in quantities:
        ent_name = getattr(getattr(q, "unit", None), "entity", None)
        ent_name = getattr(ent_name, "name", None) or ""
        if not ent_name:
            continue

        attr = _disambiguate(ent_name, text, (q.span[0], q.span[1]))
        if attr is None:
            attr = _entity_to_attr(ent_name)
        if attr is None:
            continue

        if attr == "count":
            continue

        if attr in out:
            continue

        unit_name = getattr(q.unit, "name", "") or ""
        if unit_name.lower() in ("dimensionless", "unit"):
            continue

        v = _convert(q.value, unit_name, attr)
        if v is None:
            symbol = ""
            try:
                symbols = getattr(q.unit, "symbols", None) or []
                if symbols:
                    symbol = symbols[0]
            except Exception:
                symbol = ""
            if symbol:
                v = _convert(q.value, symbol, attr)
        if v is None:
            continue
        out[attr] = v

    return out


def compare_numerics(query: dict[str, float], cand: dict[str, float]) -> Verdict:
    if not query or not cand:
        return Verdict.UNKNOWN
    shared = set(query.keys()) & set(cand.keys())
    if not shared:
        return Verdict.UNKNOWN

    discrete = set(config.get("numeric", "discrete_attrs", default=[]) or [])
    tol = float(config.get("numeric", "continuous_tolerance", default=0.10))

    for key in shared:
        a = query[key]
        b = cand[key]
        if key in discrete:
            if float(a) != float(b):
                return Verdict.FAIL
        else:
            denom = max(abs(a), abs(b), 1e-9)
            if abs(a - b) / denom > tol:
                return Verdict.FAIL
    return Verdict.PASS

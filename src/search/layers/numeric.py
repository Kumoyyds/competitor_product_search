from __future__ import annotations

import re
from typing import Any

from .. import config
from ..models import Verdict


_quantulum_parser = None

_DECIMAL_NUMBER = r"\d+(?:\.\d+)?"


def _keyword_fragment(keyword: str) -> str:
    """Return a regex fragment for a configured word or short phrase."""
    parts = [
        re.escape(part)
        for part in re.split(r"[\s-]+", keyword.strip())
        if part
    ]
    return r"[\s-]+".join(parts)


def _alternation(keywords: list[str]) -> str:
    fragments = {_keyword_fragment(keyword) for keyword in keywords if keyword.strip()}
    return "|".join(sorted(fragments, key=len, reverse=True)) or r"(?!)"


def _flatten_keywords(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(item) for items in value.values() for item in (items or [])]
    return [str(item) for item in (value or [])]


def _get_parser():
    global _quantulum_parser
    if _quantulum_parser is None:
        from quantulum3 import parser as _qp  # heavy import
        _quantulum_parser = _qp
    return _quantulum_parser


_LOCALE_CONFIG = config.get("numeric", "locale", default={}) or {}
_DECIMAL_COMMA_COUNTRIES = {
    str(item).strip().lower()
    for item in (_LOCALE_CONFIG.get("decimal_comma_countries") or [])
}
_PACK_CONFIG = _LOCALE_CONFIG.get("pack_keywords", {}) or {}
_PACK_AFTER_RE = _alternation(
    [str(item) for item in (_PACK_CONFIG.get("after_count") or ["pack", "pk"])]
)
_PACK_BEFORE_RE = _alternation(
    [str(item) for item in (_PACK_CONFIG.get("before_count") or ["pack of", "pk of"])]
)
_INCH_KEYWORDS = _flatten_keywords(
    _LOCALE_CONFIG.get("inch_keywords", {"en": ["inch", "inches", "in"]})
)
_LONG_INCH_RE = _alternation(
    [keyword for keyword in _INCH_KEYWORDS if keyword.casefold() != "in"]
)
_ABV_KEYWORD_RE = _alternation(
    [str(item) for item in (_LOCALE_CONFIG.get("abv_keywords") or ["abv"])]
)
_UNIT_CASE_OVERRIDES = {
    str(symbol).casefold(): str(canonical)
    for symbol, canonical in (
        _LOCALE_CONFIG.get("unit_case_overrides", {}) or {}
    ).items()
}
_CASE_SYMBOL_ALTERNATION = "|".join(
    re.escape(symbol)
    for symbol in sorted(_UNIT_CASE_OVERRIDES, key=len, reverse=True)
) or r"(?!)"
_NETWORK_GENERATION_VALUES = {
    float(value)
    for value in (_LOCALE_CONFIG.get("network_generation_values") or [])
}
_DEVICE_CONTEXT_KEYWORDS = [
    str(item) for item in (_LOCALE_CONFIG.get("device_context_keywords") or [])
]
_DEVICE_CONTEXT_RE = re.compile(
    rf"(?<!\w)(?:{_alternation(_DEVICE_CONTEXT_KEYWORDS)})(?!\w)",
    re.IGNORECASE,
)
_UNIT_KEYWORDS = [
    str(unit)
    for conversions in (
        config.get("numeric", "unit_conversions", default={}) or {}
    ).values()
    for unit in (conversions or {})
]
_THOUSANDS_UNIT_RE = _alternation(_UNIT_KEYWORDS)

_ABV_RE = re.compile(
    rf"(?:"
    rf"(?<!\w)(?:{_ABV_KEYWORD_RE})(?!\w)\s*[:=]?\s*({_DECIMAL_NUMBER})\s*%|"
    rf"({_DECIMAL_NUMBER})\s*%\s*(?<!\w)(?:{_ABV_KEYWORD_RE})(?!\w)|"
    rf"({_DECIMAL_NUMBER})\s*(?<!\w)(?:{_ABV_KEYWORD_RE})(?!\w)\s*-?\s*%"
    rf")",
    re.IGNORECASE,
)
_COUNT_X_RE = re.compile(
    r"\b(\d+)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(ml|l|cl|g|kg|mg)\b",
    re.IGNORECASE,
)
_PACK_RE = re.compile(
    rf"(?<!\w)(?:(\d+)\s*(?:-\s*)?(?:{_PACK_AFTER_RE})(?!\w)|"
    rf"(?:{_PACK_BEFORE_RE})(?!\w)\s*(\d+))(?!\w)",
    re.IGNORECASE,
)
_SCREEN_INCH_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:"
    rf"[\"″”]|-?\s*(?:{_LONG_INCH_RE})(?!\w)|"
    r"-\s*in\b(?!\s*-\s*\d)|\s+in\b(?=\s*(?:$|[.,;/)]))"
    r")",
    re.IGNORECASE,
)
_SLASH_STORAGE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*GB\s*/\s*(\d+(?:\.\d+)?)\s*GB\b",
    re.IGNORECASE,
)
_THOUSANDS_DOT_RE = re.compile(
    rf"(?<![\d.])(\d{{1,3}})\.(\d{{3}})(?!\d)"
    rf"(?=\s*(?:{_THOUSANDS_UNIT_RE})(?!\w))",
    re.IGNORECASE,
)
_UNIT_CASE_RE = re.compile(
    rf"(?<![\w.\-/])({_DECIMAL_NUMBER})(\s*)({_CASE_SYMBOL_ALTERNATION})(?!\w)",
    re.IGNORECASE,
)


def _normalize_separators(text: str, country: str | None = None) -> str:
    """Normalize locale separators before regex and quantulum extraction.

    A comma followed by exactly three digits remains an English grouping
    separator. Other commas between digits become decimal points. A grouping
    dot is removed only for configured decimal-comma markets and only when a
    known unit immediately follows the three-digit group.
    """

    def replace_comma(match: re.Match[str]) -> str:
        following = re.match(r"\d+", text[match.end() :])
        if following and len(following.group()) == 3:
            return ","
        return "."

    normalized = re.sub(r"(?<=\d),(?=\d)", replace_comma, text)
    if (country or "").strip().lower() in _DECIMAL_COMMA_COUNTRIES:
        normalized = _THOUSANDS_DOT_RE.sub(r"\1\2", normalized)
    return normalized


def _normalize_unit_case(text: str) -> str:
    """Rewrite configured unit-symbol variants before quantulum parses them."""
    has_device_context = bool(_DEVICE_CONTEXT_RE.search(text))

    def replace(match: re.Match[str]) -> str:
        number, whitespace, symbol = match.groups()
        canonical = _UNIT_CASE_OVERRIDES.get(symbol.casefold())
        if canonical is None or symbol == canonical:
            return match.group(0)

        # An uppercase G is ambiguous with mobile-network generations and
        # camera-lens designations. Abstain instead of creating a hard false
        # weight mismatch when either independent safety signal fires.
        if symbol.casefold() == "g":
            if float(number) in _NETWORK_GENERATION_VALUES or has_device_context:
                return match.group(0)

        return f"{number}{whitespace}{canonical}"

    return _UNIT_CASE_RE.sub(replace, text)


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

    def keyword_pattern(keyword: str) -> re.Pattern[str]:
        parts = [re.escape(part) for part in keyword.lower().split()]
        return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)")

    def nearest(ctx: str, *, preceding: bool) -> str | None:
        matches: list[tuple[int, int, str]] = []
        for keyword, attr in rule.items():
            for match in keyword_pattern(keyword).finditer(ctx.lower()):
                distance = len(ctx) - match.end() if preceding else match.start()
                # At equal distance, prefer a longer, more specific phrase such as
                # "memory card" over its generic prefix "memory".
                matches.append((distance, -len(match.group()), attr))
        if not matches:
            return None
        return min(matches)[2]

    # Product qualifiers usually follow the quantity ("8GB RAM"). Prefer that
    # direction, but do not let a later quantity's qualifier leak into this one.
    after = text[span[1] : min(len(text), span[1] + window)]
    next_number = re.search(r"\d", after)
    if next_number:
        after = after[: next_number.start()]
    attr = nearest(after, preceding=False)
    if attr is not None:
        return attr

    before = text[max(0, span[0] - window) : span[0]]
    return nearest(before, preceding=True)


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


def extract_numerics(text: str, country: str | None = None) -> dict[str, float]:
    """Return {attr_key: value_in_base_unit} for a product title or candidate text.

    Custom regex fallbacks run before quantulum3 so we don't lose ABV / `N X Nml`
    count patterns to quantulum3's dimensionless bucket.
    """
    if not text:
        return {}
    text = _normalize_separators(text, country=country)
    text = _normalize_unit_case(text)
    out: dict[str, float] = {}

    m_abv = _ABV_RE.search(text)
    if m_abv:
        try:
            value = next(group for group in m_abv.groups() if group is not None)
            out["abv_percent"] = float(value)
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
            out["count"] = float(int(m_pack.group(1) or m_pack.group(2)))
        except ValueError:
            pass

    m_screen = _SCREEN_INCH_RE.search(text)
    if m_screen and "screen_inch" not in out:
        try:
            out["screen_inch"] = float(m_screen.group(1))
        except ValueError:
            pass

    # Phone listings often omit qualifiers in compact RAM/storage notation
    # ("6GB/128GB"). Recording only the larger value as storage is safer than
    # letting first-value-wins turn 6GB into a hard storage mismatch. Do not
    # infer the smaller value as RAM without an explicit qualifier.
    m_slash_storage = _SLASH_STORAGE_RE.search(text)
    if m_slash_storage and "storage_gb" not in out:
        try:
            out["storage_gb"] = max(
                float(m_slash_storage.group(1)), float(m_slash_storage.group(2))
            )
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
            try:
                symbols = getattr(q.unit, "symbols", None) or []
            except Exception:
                symbols = []
            for symbol in symbols:
                v = _convert(q.value, symbol, attr)
                if v is not None:
                    break
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

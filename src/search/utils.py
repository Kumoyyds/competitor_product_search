from __future__ import annotations

import os
import re
import unicodedata
from functools import lru_cache

import pandas as pd


# Maintained file — edit via Excel. See "Files to maintain" in src/search/README.md.
_BRAND_XLSX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maintain", "brand.xlsx")


def remove_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(s: str) -> str:
    return remove_accents(s).lower().strip()


@lru_cache(maxsize=1)
def load_brands_all() -> tuple[str, ...]:
    df = pd.read_excel(_BRAND_XLSX)
    series = df["brandname_en"].dropna().astype(str).str.strip()
    series = series[series != ""]
    return tuple(sorted(set(series.tolist()), key=lambda x: (-len(x), x.lower())))


@lru_cache(maxsize=1)
def load_brands_fuzzy_safe() -> tuple[str, ...]:
    return tuple(b for b in load_brands_all() if len(b) >= 4 and re.search(r"[A-Za-z]", b))


@lru_cache(maxsize=1)
def _literal_brand_regex() -> tuple[re.Pattern, dict[str, str]]:
    norm_to_original: dict[str, str] = {}
    for b in load_brands_all():
        nb = normalize(b)
        if not nb:
            continue
        prev = norm_to_original.get(nb)
        if prev is None or len(b) > len(prev):
            norm_to_original[nb] = b
    keys = sorted(norm_to_original.keys(), key=lambda s: (-len(s), s))
    pattern = r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)"
    return re.compile(pattern), norm_to_original


def find_literal_brands(text: str) -> list[str]:
    """Return ALL brands literally mentioned in `text`, in order of first appearance.

    Returning the full set (rather than picking one) lets the comparison layer
    handle noisy titles where descriptors collide with the brand list — e.g.
    "Tetley 20 Supergreen Vitamin C Tropical Tea 40g" yields ["Tetley", "Tropical"]
    so a downstream "any-pair-matches" rule can recover the real brand.
    Deduped, preserving original casing.
    """
    if not text:
        return []
    regex, mapping = _literal_brand_regex()
    norm = normalize(text)
    seen: set[str] = set()
    out: list[str] = []
    for m in regex.finditer(norm):
        nb = m.group(1)
        original = mapping.get(nb, nb)
        if original not in seen:
            seen.add(original)
            out.append(original)
    return out


def find_literal_brand(text: str) -> str | None:
    """Backwards-compat shim: first brand by appearance, or None.

    Prefer find_literal_brands for matching logic — single-pick is only useful
    for legacy callers.
    """
    brands = find_literal_brands(text)
    return brands[0] if brands else None


def get_split_num(num: int) -> int:
    i = 1
    while num // 10 > 0:
        num = num // 10
        i *= 10
    return num * i

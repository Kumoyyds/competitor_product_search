from __future__ import annotations

from rapidfuzz import fuzz, process

from .. import config
from ..models import Verdict
from ..utils import find_literal_brands, load_brands_fuzzy_safe, normalize


def extract_brands(text: str, supplied: str | None = None) -> list[str]:
    """Return all plausible brand identifications for `text`.

    Priority:
      1. user-supplied brand (query-side only) — wins outright
      2. ALL literal word-boundary matches against brand.xlsx
         (handles short / numeric / digit-bearing brands precisely)
      3. ONE fuzzy match against the length>=4-with-letter subset
         (only when literal found nothing — fuzzy is a last resort)
      4. ONE heuristic: first uppercase token, length>=2, non-numeric

    Returning the full list lets compare_brands use an any-pair-pass rule so
    noise tokens that happen to be in brand.xlsx (e.g. "Tropical") don't drown
    out the real brand sitting next to them.
    """
    if supplied:
        s = supplied.strip()
        return [s] if s else []
    if not text:
        return []

    literal = find_literal_brands(text)
    if literal:
        return literal

    fuzzy_set = load_brands_fuzzy_safe()
    threshold = int(config.get("brand", "fuzzy_same_threshold", default=88))
    match = process.extractOne(
        normalize(text),
        [normalize(b) for b in fuzzy_set],
        scorer=fuzz.WRatio,
        score_cutoff=threshold,
    )
    if match is not None:
        _, _, idx = match
        return [fuzzy_set[idx]]

    first = text.strip().split()
    if first:
        tok = first[0]
        if tok.isupper() and len(tok) >= 2 and not tok.isdigit():
            return [tok]

    return []


def compare_brands(a: list[str], b: list[str]) -> Verdict:
    """Three-state comparison between two brand lists.

    PASS    — any pair has WRatio >= same_threshold (or normalized-equal)
    FAIL    — both sides non-empty AND every pair has WRatio <= differ_threshold
    UNKNOWN — at least one side empty, or middle-ground scores exist
    """
    if not a or not b:
        return Verdict.UNKNOWN

    same_thr = int(config.get("brand", "fuzzy_same_threshold", default=88))
    diff_thr = int(config.get("brand", "fuzzy_differ_threshold", default=40))

    norm_a = [normalize(x) for x in a]
    norm_b = [normalize(y) for y in b]

    all_differ = True
    for na in norm_a:
        for nb in norm_b:
            if not na or not nb:
                continue
            if na == nb:
                return Verdict.PASS
            score = fuzz.WRatio(na, nb)
            if score >= same_thr:
                return Verdict.PASS
            if score > diff_thr:
                all_differ = False

    return Verdict.FAIL if all_differ else Verdict.UNKNOWN

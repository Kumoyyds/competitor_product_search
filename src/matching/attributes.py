from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from src.models import EvidenceStatus, InputItem, ProductData
from src.search.layers.brand import compare_brands, extract_brands
from src.search.layers.numeric import compare_numerics, extract_numerics
from src.search.models import Verdict

_NUMBER = r"\d+(?:\.\d+)?"
_UNIT = r"ml|cl|l|mg|g|kg"
_COUNT_FIRST_RE = re.compile(
    rf"\b(\d+)\s*[x×*]\s*({_NUMBER})\s*({_UNIT})\b", re.IGNORECASE
)
_SIZE_FIRST_RE = re.compile(
    rf"\b({_NUMBER})\s*({_UNIT})\s*[x×*]\s*(\d+)\b", re.IGNORECASE
)
_EACH_RE = re.compile(
    rf"\b({_NUMBER})\s*({_UNIT})\s*(?:each|per\s+(?:item|unit|piece))\b",
    re.IGNORECASE,
)
_TOTAL_AFTER_RE = re.compile(
    rf"\b({_NUMBER})\s*({_UNIT})\s*(?:in\s+)?total\b", re.IGNORECASE
)
_TOTAL_BEFORE_RE = re.compile(
    rf"\btotal\s*(?:of\s*)?({_NUMBER})\s*({_UNIT})\b", re.IGNORECASE
)
_PACK_COUNT_RE = re.compile(
    r"\b(?:pack\s+of\s+(\d+)|(\d+)\s*[- ]?(?:pack|pk|pcs|pieces|units))\b",
    re.IGNORECASE,
)
_UNIT_MAP: dict[str, tuple[str, float]] = {
    "ml": ("volume", 1.0),
    "cl": ("volume", 10.0),
    "l": ("volume", 1000.0),
    "mg": ("mass", 0.001),
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
}
_DIMENSION_NUMERIC_KEY = {"volume": "volume_ml", "mass": "weight_g"}


@dataclass(slots=True)
class QuantitySignature:
    dimension: str
    per_item: float | None = None
    count: int | None = None
    total: float | None = None
    inconsistent: bool = False

    def derive(self, tolerance: float = 0.10) -> None:
        if self.per_item is not None and self.count is not None:
            derived = self.per_item * self.count
            if self.total is None:
                self.total = derived
            elif _different(self.total, derived, tolerance):
                self.inconsistent = True
        elif self.total is not None and self.count:
            self.per_item = self.total / self.count
        elif self.total is not None and self.per_item:
            ratio = self.total / self.per_item
            rounded = round(ratio)
            if rounded > 0 and abs(ratio - rounded) < 1e-9:
                self.count = rounded


def normalize_gtin(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"[\s-]+", "", value)
    if not digits.isdigit() or len(digits) not in {8, 12, 13, 14}:
        return None
    payload = [int(char) for char in digits]
    check = payload.pop()
    total = sum(
        digit * (3 if offset % 2 == 0 else 1)
        for offset, digit in enumerate(reversed(payload))
    )
    return digits if (10 - total % 10) % 10 == check else None


def _normalized(value: str, unit: str) -> tuple[str, float]:
    dimension, factor = _UNIT_MAP[unit.casefold()]
    return dimension, float(value) * factor


def _count_near_each(text: str, each_end: int) -> int | None:
    tail = text[each_end : each_end + 80]
    explicit = re.search(
        r"(?:pack\s+of|pack|count|qty|quantity)\s*[:=]?\s*(\d+)",
        tail,
        re.IGNORECASE,
    )
    if explicit:
        return int(explicit.group(1))
    bare = re.search(r"[,;]\s*(\d+)\b(?!\s*(?:ml|cl|l|mg|g|kg)\b)", tail, re.IGNORECASE)
    return int(bare.group(1)) if bare else None


def extract_multipack(text: str) -> dict[str, QuantitySignature]:
    signatures: dict[str, QuantitySignature] = {}

    def get(dimension: str) -> QuantitySignature:
        return signatures.setdefault(dimension, QuantitySignature(dimension))

    for match in _COUNT_FIRST_RE.finditer(text):
        dimension, value = _normalized(match.group(2), match.group(3))
        sig = get(dimension)
        sig.count = int(match.group(1))
        sig.per_item = value
    for match in _SIZE_FIRST_RE.finditer(text):
        dimension, value = _normalized(match.group(1), match.group(2))
        sig = get(dimension)
        sig.per_item = value
        sig.count = int(match.group(3))
    for match in _EACH_RE.finditer(text):
        dimension, value = _normalized(match.group(1), match.group(2))
        sig = get(dimension)
        sig.per_item = value
        sig.count = sig.count or _count_near_each(text, match.end())
    for pattern in (_TOTAL_AFTER_RE, _TOTAL_BEFORE_RE):
        for match in pattern.finditer(text):
            dimension, value = _normalized(match.group(1), match.group(2))
            get(dimension).total = value

    pack_match = _PACK_COUNT_RE.search(text)
    pack_count = int(pack_match.group(1) or pack_match.group(2)) if pack_match else None
    for sig in signatures.values():
        if sig.count is None:
            sig.count = pack_count
        sig.derive()
    return signatures


def _different(a: float, b: float, tolerance: float = 0.10) -> bool:
    return abs(a - b) / max(abs(a), abs(b), 1e-9) > tolerance


def compare_multipacks(
    left: dict[str, QuantitySignature],
    right: dict[str, QuantitySignature],
    *,
    tolerance: float = 0.10,
) -> tuple[EvidenceStatus, list[str]]:
    notes: list[str] = []
    comparable = False
    for dimension in sorted(set(left) & set(right)):
        a, b = left[dimension], right[dimension]
        if a.inconsistent or b.inconsistent:
            notes.append(f"{dimension}: internally inconsistent multipack declaration")
            continue
        for field in ("per_item", "count", "total"):
            av, bv = getattr(a, field), getattr(b, field)
            if av is None or bv is None:
                continue
            comparable = True
            differs = av != bv if field == "count" else _different(float(av), float(bv), tolerance)
            if differs:
                notes.append(f"{dimension}.{field}: {av:g} vs {bv:g}")
                return EvidenceStatus.CONFLICT, notes
            notes.append(f"{dimension}.{field}: {av:g} agrees")
    return (EvidenceStatus.PASS if comparable else EvidenceStatus.UNKNOWN), notes


def _product_text(product: ProductData) -> str:
    pieces = [product.title]
    if product.brand:
        pieces.append(product.brand)
    if product.variant:
        pieces.append(json.dumps(product.variant, ensure_ascii=False, sort_keys=True))
    return " | ".join(pieces)


def compare_variants(item: InputItem, product: ProductData) -> tuple[EvidenceStatus, dict[str, Any]]:
    left_text = item.title
    right_text = _product_text(product)
    left_brands = extract_brands(left_text)
    right_brands = extract_brands(right_text, supplied=product.brand)
    brand_verdict = compare_brands(left_brands, right_brands)

    left_pack = extract_multipack(left_text)
    right_pack = extract_multipack(right_text)
    pack_status, pack_notes = compare_multipacks(left_pack, right_pack)

    left_numeric = extract_numerics(left_text, country=item.country)
    right_numeric = extract_numerics(right_text, country=item.country)
    if left_pack or right_pack:
        dimensions = set(left_pack) | set(right_pack)
        for dimension in dimensions:
            key = _DIMENSION_NUMERIC_KEY[dimension]
            left_numeric.pop(key, None)
            right_numeric.pop(key, None)
        left_numeric.pop("count", None)
        right_numeric.pop("count", None)
    numeric_verdict = compare_numerics(left_numeric, right_numeric)

    conflict = (
        brand_verdict == Verdict.FAIL
        or numeric_verdict == Verdict.FAIL
        or pack_status == EvidenceStatus.CONFLICT
    )
    any_pass = (
        brand_verdict == Verdict.PASS
        or numeric_verdict == Verdict.PASS
        or pack_status == EvidenceStatus.PASS
    )
    status = EvidenceStatus.CONFLICT if conflict else (
        EvidenceStatus.PASS if any_pass else EvidenceStatus.UNKNOWN
    )
    evidence = {
        "input_brands": left_brands,
        "product_brands": right_brands,
        "brand_status": brand_verdict.value,
        "input_numerics": left_numeric,
        "product_numerics": right_numeric,
        "numeric_status": numeric_verdict.value,
        "input_multipack": {key: asdict(value) for key, value in left_pack.items()},
        "product_multipack": {key: asdict(value) for key, value in right_pack.items()},
        "multipack_status": pack_status.value,
        "multipack_notes": pack_notes,
    }
    return status, evidence

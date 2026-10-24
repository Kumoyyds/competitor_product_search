"""Session truth and candidate comparisons for interactive cold start.

Correction parsing and decimal-scale equivalence are deliberately local to cold
start; runtime golden comparison keeps its existing contract.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import unified_diff
from typing import Any, Mapping

from .repair.golden import _normalize

COMPARE_FIELDS = (
    "title", "brand", "gtin", "image_urls", "variant", "price", "currency",
    "list_price", "membership_price", "in_stock", "availability_raw",
)
PRICE_FIELDS = frozenset({"price", "list_price", "membership_price"})
FIELD_WEIGHTS = dict(zip(COMPARE_FIELDS, (1, .5, .5, .5, .5, 3, 3, 2, 2, 3, 1)))
Score = tuple[int, float]


def parse_correction(field_name: str, raw: str) -> Any:
    """Parse human input without ever putting prompt prose into expected data."""
    text = raw.strip()
    if text.lower() in {"-", "none", "null", "n/a", "na"}:
        return None
    if field_name in PRICE_FIELDS:
        if "," in text:
            if "." in text and text.rfind(".") > text.rfind(","):
                text = text.replace(",", "")
            else:
                text = text.replace(".", "").replace(",", ".")
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("enter a finite number (e.g. 3.31 or 3,31)") from exc
        if not value.is_finite():
            raise ValueError("enter a finite number")
        return format(value.normalize(), "f")
    if field_name == "in_stock":
        if text.lower() not in {"true", "false", "y", "n", "yes", "no", "1", "0"}:
            raise ValueError("enter true or false")
        return text.lower() in {"true", "y", "yes", "1"}
    if field_name in {"image_urls", "variant"}:
        try:
            value = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ValueError("enter a JSON list of URLs or JSON object") from exc
        if field_name == "image_urls":
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError("enter a JSON list of URL strings")
        elif not isinstance(value, dict):
            raise ValueError("enter a JSON object")
        return value
    if not text:
        raise ValueError("enter a value or '-' to clear")
    return text


def product_fields(product: Any) -> dict[str, Any]:
    if product is None:
        return {}
    source = product.model_dump(mode="json") if hasattr(product, "model_dump") else product
    return {name: source.get(name) for name in COMPARE_FIELDS}


def _comparable(name: str, value: Any) -> Any:
    if name in PRICE_FIELDS and value is not None:
        try:
            number = Decimal(str(value))
            if number.is_finite():
                value = format(number.normalize(), "f")
        except InvalidOperation:
            pass
    return _normalize(value)


@dataclass
class CaseExpectation:
    url: str
    page_type: str
    fields: dict[str, Any] = field(default_factory=dict)
    hints: list[str] = field(default_factory=list)
    status: str = "rejected_unspecified"


class ExpectationLedger:
    def __init__(self) -> None:
        self.cases: dict[str, CaseExpectation] = {}

    def review(self, url: str, page_type: str, product: Any, *, accepted: bool,
               corrections: Mapping[str, Any] | None = None, hint: str = "",
               unresolved: bool = False) -> CaseExpectation:
        old = self.cases.get(url)
        if old is not None and old.page_type != page_type:
            raise ValueError(f"Conflicting page types for {url}")
        hints = list(old.hints) if old else []
        if hint:
            hints.append(hint)
        confirmed = product is not None and (accepted or bool(corrections)) and not unresolved
        fields = product_fields(product)
        fields.update(corrections or {})
        expectation = CaseExpectation(url, page_type, fields, hints,
                                      "confirmed" if confirmed else "rejected_unspecified")
        self.cases[url] = expectation
        return expectation

    def matches(self, url: str, product: Any) -> bool:
        exp = self.cases.get(url)
        return bool(exp and exp.status == "confirmed" and product is not None
                    and not diff_case(product, exp))

    def score(self, results: Mapping[str, Any]) -> Score:
        pages, fields = 0, 0.0
        for url, product in results.items():
            exp = self.cases.get(url)
            if exp is None or exp.status != "confirmed" or product is None:
                continue
            wrong = diff_case(product, exp)
            pages += not wrong
            fields += sum(weight for name, weight in FIELD_WEIGHTS.items() if name not in wrong)
        return pages, fields


def diff_case(product: Any, expectation: CaseExpectation) -> dict[str, tuple[Any, Any]]:
    got = product_fields(product)
    return {
        name: (expectation.fields.get(name), got.get(name))
        for name in COMPARE_FIELDS
        if product is None or _comparable(name, expectation.fields.get(name)) != _comparable(name, got.get(name))
    }


def regressions(base_result: Mapping[str, Any], cand_result: Mapping[str, Any],
                ledger: ExpectationLedger) -> set[tuple[str, str]]:
    return changes(base_result, cand_result, ledger)[1]


def changes(base_result: Mapping[str, Any], cand_result: Mapping[str, Any],
            ledger: ExpectationLedger) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    fixed, broke = set(), set()
    for url, exp in ledger.cases.items():
        if exp.status != "confirmed":
            continue
        before = set(diff_case(base_result.get(url), exp))
        after = set(diff_case(cand_result.get(url), exp))
        fixed.update((url, name) for name in before - after)
        broke.update((url, name) for name in after - before)
    return fixed, broke


@dataclass(frozen=True)
class RoundRecord:
    round: int
    code: str
    diff_vs_base: str
    per_case: dict[str, set[str]]
    fixed: set[tuple[str, str]]
    broke: set[tuple[str, str]]
    adopted: bool
    reviewer: str
    base_code: str = ""

    @classmethod
    def create(cls, round: int, code: str, base_code: str, results: Mapping[str, Any],
               ledger: ExpectationLedger, fixed: set[tuple[str, str]],
               broke: set[tuple[str, str]], adopted: bool, reviewer: str) -> "RoundRecord":
        diff = "".join(unified_diff(base_code.splitlines(True), code.splitlines(True),
                                    fromfile="base", tofile="candidate"))[:3000]
        per_case = {}
        for url, product in results.items():
            exp = ledger.cases.get(url)
            per_case[url] = (set(diff_case(product, exp)) if exp and exp.status == "confirmed"
                             else {"<unconfirmed>"})
        return cls(round, code, diff, per_case, fixed, broke, adopted, reviewer, base_code)

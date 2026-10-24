from decimal import Decimal

import pytest

from src.scraping.coldstart_ledger import (
    COMPARE_FIELDS, ExpectationLedger, RoundRecord, changes, diff_case, parse_correction,
)


def product(**kwargs):
    return dict(title="Thing", price="10.00", list_price="20", membership_price=None,
                currency="EUR", in_stock=True, image_urls=[], **kwargs)


def test_accept_reject_overwrite_and_accumulated_hints():
    ledger = ExpectationLedger()
    current = product()
    exp = ledger.review("url", "standard", current, accepted=True, hint="first")
    assert exp.status == "confirmed" and set(exp.fields) == set(COMPARE_FIELDS)
    assert ledger.matches("url", current)
    exp = ledger.review("url", "standard", current, accepted=False,
                        corrections={"membership_price": "3.31"}, hint="JoyPlus")
    assert exp.fields["list_price"] == "20"
    assert diff_case(current, exp) == {"membership_price": ("3.31", None)}
    ledger.review("url", "standard", current, accepted=False)
    assert not ledger.matches("url", current)
    ledger.review("url", "standard", current, accepted=True, hint="latest")
    assert ledger.matches("url", current)
    assert ledger.cases["url"].hints == ["first", "JoyPlus", "latest"]
    with pytest.raises(ValueError, match="Conflicting"):
        ledger.review("url", "membership", current, accepted=True)


@pytest.mark.parametrize(("field", "raw", "expected"), [
    ("price", "3,31", "3.31"), ("list_price", "13.099,05", "13099.05"),
    ("price", "13,099.05", "13099.05"),
    ("price", "3.30", "3.3"), ("price", "-", None), ("brand", "none", None),
    ("in_stock", "false", False), ("in_stock", "yes", True),
    ("image_urls", '["https://image"]', ["https://image"]),
    ("variant", '{"size": "L"}', {"size": "L"}), ("gtin", "001234", "001234"),
])
def test_typed_correction(field, raw, expected):
    assert parse_correction(field, raw) == expected


@pytest.mark.parametrize(("field", "raw"), [("price", "NaN"), ("price", "Infinity"),
    ("in_stock", "maybe"), ("variant", "[]"), ("image_urls", '[1]'), ("title", "")])
def test_invalid_correction(field, raw):
    with pytest.raises(ValueError):
        parse_correction(field, raw)


def test_decimal_scale_and_failure_regressions_and_score_priority():
    ledger = ExpectationLedger()
    truth = product()
    truth["membership_price"] = "3.3"
    ledger.review("u", "membership", truth, accepted=True)
    a = {**truth, "membership_price": None}
    b = {**truth, "list_price": None}
    assert ledger.score({"u": a}) == ledger.score({"u": b})
    fixed, broke = changes({"u": a}, {"u": b}, ledger)
    assert fixed == {("u", "membership_price")}
    assert broke == {("u", "list_price")}
    assert ledger.score({"u": truth}) > ledger.score({"u": a})
    assert ledger.matches("u", {**truth, "price": Decimal("10.000"), "membership_price": "3.30"})
    assert len(diff_case(None, ledger.cases["u"])) == 11
    assert len(changes({"u": truth}, {"u": None}, ledger)[1]) == 11
    record = RoundRecord.create(1, "new\n" * 3000, "base\n", {"u": b}, ledger,
                                fixed, broke, False, "auto")
    assert len(record.diff_vs_base) <= 3000 and record.per_case["u"] == {"list_price"}


def test_declined_correction_is_not_confirmation():
    ledger = ExpectationLedger()
    ledger.review("u", "standard", product(), accepted=False,
                  corrections={"brand": "new"}, unresolved=True)
    assert not ledger.matches("u", product())
    assert ledger.score({"u": product()}) == (0, 0)

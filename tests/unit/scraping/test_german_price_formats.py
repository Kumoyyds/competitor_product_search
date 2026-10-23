"""German/EU price formats in prepass, detection, and the parser prompt.

The UK regression tests compare against frozen copies of the regex and cleaner
that existed before decimal-comma support, so any drift in GBP/USD handling fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.scraping.detection import _has_add_to_cart, _has_price
from src.scraping.repair import prepass
from src.scraping.repair.prompts import SCHEMA_HINT, parser_gen_prompt

# --- frozen pre-change behaviour -------------------------------------------

_OLD_CURRENCY_RE = re.compile(
    r"""(?x)
    [£€$]\s?\d[\d,]*\.?\d*
    |
    \d[\d,]*\.\d{2}\s?(?:GBP|EUR|USD)
    """
)
_OLD_UNIT_PRICE_RE = re.compile(
    r"""(?ix)
    [£€$]\s?\d[\d,]*\.?\d*\s*/\s*(?:kg|litre|l|100\s*g|g|oz|lb|each|unit)
    |
    \d[\d,]*\.?\d*\s*(?:GBP|EUR|USD)\s*/\s*(?:kg|litre|l|100\s*g|g|oz|lb|each|unit)
    """
)


def _old_clean_numeric(value_text: str) -> str:
    cleaned = re.sub(r"[£€$]", "", value_text).strip()
    cleaned = re.sub(r"\s*(GBP|EUR|USD)\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = cleaned.replace(",", "")
    try:
        float(cleaned)
        return cleaned
    except (ValueError, TypeError):
        return ""


def _extract(text: str, regex: re.Pattern[str], clean) -> list[str]:
    return [clean(m) for m in regex.findall(text)]


UK_TEXTS = [
    "£19.99",
    "£ 19.99",
    "£1,299.99",
    "£1,299",
    "£0.00",
    "Was £24.99 Now £19.99",
    "Save £5.00",
    "Clubcard Price £3.50",
    "£1.50/100g",
    "£2.20/kg",
    "£12.99 each",
    "19.99 GBP",
    "1,299.99 GBP",
    "$49.99",
    "€19.99",
    "€1,299.99",
    "2.99 EUR",
    "12x330ml £9.00",
    "4.5 out of 5 stars, 1,234 ratings",
    "Pack of 6, 12, 24",
    "Free delivery on orders over £50",
    "£1.299",
]


@pytest.mark.parametrize("text", UK_TEXTS)
def test_uk_currency_extraction_unchanged(text: str) -> None:
    old = _extract(text, _OLD_CURRENCY_RE, _old_clean_numeric)
    new = _extract(text, prepass._CURRENCY_RE, prepass._clean_numeric)
    assert new == old


@pytest.mark.parametrize("text", UK_TEXTS)
def test_uk_unit_price_flag_unchanged(text: str) -> None:
    assert bool(prepass._UNIT_PRICE_RE.search(text)) == bool(_OLD_UNIT_PRICE_RE.search(text))


# --- German formats --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("19,99 €", ["19.99"]),
        ("19,99\xa0€", ["19.99"]),
        ("€19,99", ["19.99"]),
        ("€ 1.299,00", ["1299.00"]),
        ("1.299,00 €", ["1299.00"]),
        ("1.299 €", ["1299"]),
        ("19 €", ["19"]),
        ("19,99 EUR", ["19.99"]),
        ("EUR 19,99", ["19.99"]),
        ("UVP 24,99 € jetzt 19,99 €", ["24.99", "19.99"]),
        ("statt 1.049,00 € nur 899,00 €", ["1049.00", "899.00"]),
    ],
)
def test_german_amounts(text: str, expected: list[str]) -> None:
    assert _extract(text, prepass._CURRENCY_RE, prepass._clean_numeric) == expected


@pytest.mark.parametrize(
    "text",
    ["2,49 €/kg", "2,49 € / 1 kg", "9,96 € pro 1 l", "0,99 € je 100 ml",
     "1 kg = 9,96 €", "Grundpreis: 9,96 € / 1.000 g", "€ 2,49/kg"],
)
def test_german_unit_prices_are_flagged(text: str) -> None:
    assert prepass._UNIT_PRICE_RE.search(text)


@pytest.mark.parametrize("text", ["19,99 €", "1.299,00 €", "UVP 24,99 €"])
def test_german_shelf_prices_are_not_unit_prices(text: str) -> None:
    assert not prepass._UNIT_PRICE_RE.search(text)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("19,99", "19.99"), ("1.299,00", "1299.00"), ("19.99", None), ("1,299.99", None),
     ("1,299", None), ("1.299", None)],
)
def test_german_amount_helper(raw: str, expected: str | None) -> None:
    assert prepass._german_amount(raw) == expected


def test_jsonld_decimal_comma_string_price() -> None:
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "Product", "name": "X", "offers": {"@type": "Offer", '
        '"price": "19,99", "priceCurrency": "EUR"}}'
        "</script></head><body><h1>X</h1></body></html>"
    )
    ctx = prepass.build_price_aware_context(html, "https://example.de/p/1")
    values = {ev.value for ev in ctx.price_evidence if ev.source == "json_ld"}
    assert values == {"19.99"}


# --- detection -------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    ['<button>In den Warenkorb</button>', '<button>Jetzt kaufen</button>',
     '<button>In den Einkaufswagen</button>', '<button>Add to basket</button>'],
)
def test_add_to_cart_patterns(html: str) -> None:
    assert _has_add_to_cart(html)


def test_german_price_detected_by_invalid_page_check() -> None:
    assert _has_price("<span>19,99 €</span>")


# --- parser prompt ---------------------------------------------------------


def test_parser_prompt_documents_decimal_comma_rule() -> None:
    ctx = prepass.build_price_aware_context("<html><body><h1>X</h1></body></html>", "https://x.de/p")
    system = parser_gen_prompt(ctx, "x", role="first", initial_errors=[], attempts=[])[0]["content"]
    assert "European decimal-comma amount" in system
    # The original tolerant regex is still the fallback path.
    assert r"re.search(r'\d[\d,]*\.?\d*', node_text)" in system
    assert '"1.299,00 €" is returned as "1299.00"' in SCHEMA_HINT


def _prompt_decimal_comma_regex() -> re.Pattern[str]:
    ctx = prepass.build_price_aware_context("<html><body><h1>X</h1></body></html>", "https://x.de/p")
    system = parser_gen_prompt(ctx, "x", role="first", initial_errors=[], attempts=[])[0]["content"]
    m = re.search(r"de = re\.search\(r'(.+?)', node_text\)", system)
    assert m is not None
    return re.compile(m.group(1))


@pytest.mark.parametrize(
    ("node_text", "expected"),
    [("19,99 €", "19.99"), ("1.299,00 €", "1299.00"), ("€ 7,50", "7.50"),
     ("£19.99", None), ("£1,299.99", None), ("£1,299", None), ("Sizes 10,12,14", None)],
)
def test_prompt_regex_as_llm_would_copy_it(node_text: str, expected: str | None) -> None:
    m = _prompt_decimal_comma_regex().search(node_text)
    got = m.group().replace(".", "").replace(",", ".") if m else None
    assert got == expected


# --- Joybuy.de: displayed price vs hidden state ----------------------------

_JOYBUY_FIXTURE = Path(__file__).parent / "fixtures" / "joybuy_de_standard.html"
_JOYBUY_URL = "https://www.joybuy.de/dp/braun-series-3-300s-elektrorasierer-schwarz/10233120"


@pytest.fixture(scope="module")
def joybuy_html() -> str:
    # Original page source. It also carries 79.99 / discount 20.00 in __next_f
    # state, switched off by isDiscount=false -- the page shows only 59,99 €.
    return _JOYBUY_FIXTURE.read_text(encoding="utf-8")


def test_joybuy_fixture_keeps_hidden_state(joybuy_html: str) -> None:
    assert "originalPriceForPopup" in joybuy_html
    assert "79.99" in joybuy_html


def test_split_span_price_is_collected(joybuy_html: str) -> None:
    ctx = prepass.build_price_aware_context(joybuy_html, _JOYBUY_URL)
    dom_values = {ev.value for ev in ctx.price_evidence if ev.source == "dom"}
    assert "59.99" in dom_values
    # Hidden-state amounts never become evidence.
    assert not {"79.99", "20.00"} & {ev.value for ev in ctx.price_evidence}


def test_joybuy_expected_price_contract(joybuy_html: str) -> None:
    ctx = prepass.build_price_aware_context(joybuy_html, _JOYBUY_URL)
    main_prices = {
        ev.value for ev in ctx.price_evidence if ev.source == "json_ld" and not ev.struck_through
    }
    assert main_prices == {"59.99"}
    assert ctx.promotion_signal is None or ctx.promotion_signal.get("kind") not in (
        "discount", "membership",
    )


def test_split_price_does_not_duplicate_or_touch_whole_node_prices() -> None:
    html = (
        '<html><body><h1>X</h1><div><span class="a">£19.99</span></div>'
        '<div><span>12</span><span>,</span><span>50</span><span> €</span></div></body></html>'
    )
    ctx = prepass.build_price_aware_context(html, "https://x.de/p")
    vals = sorted(ev.value for ev in ctx.price_evidence)
    assert vals == ["12.50", "19.99"]


def test_split_price_ignores_long_or_non_price_text() -> None:
    html = (
        "<html><body><h1>X</h1>"
        "<p><span>Lieferung ab</span> <span>3</span> <span>Tagen für 4,99 €</span></p>"
        "</body></html>"
    )
    ctx = prepass.build_price_aware_context(html, "https://x.de/p")
    assert [ev.value for ev in ctx.price_evidence] == ["4.99"]


def test_parser_prompt_scopes_embedded_state_prices() -> None:
    ctx = prepass.build_price_aware_context("<html><body><h1>X</h1></body></html>", "https://x.de/p")
    system = parser_gen_prompt(ctx, "x", role="first", initial_errors=[], attempts=[])[0]["content"]
    assert "originalPriceForPopup" in system
    assert "JSON-LD and visible DOM prices are not subject to this rule" in system

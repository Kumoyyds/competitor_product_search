"""Soft-404 pages (HTTP 200 with a not-found <title>) must be invalid targets."""

from __future__ import annotations

import pytest

from src.scraping.detection import detect_invalid_page

# Recommendation cards keep price / title / add-to-basket signals alive on the
# error page, so only the <title> distinguishes it from a product page.
_CHROME = (
    '<h1>Recommended</h1><span>£24.90</span><button>Add to basket</button>'
    + "<div>filler</div>" * 600
)


def _page(title: str) -> str:
    return f"<html><head><title>{title}</title></head><body>{_CHROME}</body></html>"


@pytest.mark.parametrize(
    "title",
    [
        "404 - Page Not Found",
        "Error 404",
        "Page not found | Tesco",
        "Seite nicht gefunden - Shop",
        "Page introuvable",
    ],
)
def test_not_found_title_is_invalid(title: str) -> None:
    signal = detect_invalid_page(_page(title), 200, "onbuy")
    assert signal is not None
    assert signal.signal_type == "not_found_title"


def test_normal_title_stays_valid() -> None:
    assert detect_invalid_page(_page("Super King Duvet 7.5 Tog on OnBuy"), 200, "onbuy") is None


def test_number_in_product_title_is_not_a_false_positive() -> None:
    assert detect_invalid_page(_page("Bosch Series 4040 Drill 404 Bit Set"), 200, "onbuy") is None

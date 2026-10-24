"""Bright Data request URL encoding and API-rejection classification."""

from __future__ import annotations

import pytest

from src.scraping.exceptions import BrightDataInfraError
from src.scraping.extraction.bright_data import _check_infra_error, _to_request_uri


def test_non_ascii_slug_is_percent_encoded() -> None:
    url = "https://www.joybuy.de/dp/essensschüssel-ø-115-cm-h-6/10466952"
    assert _to_request_uri(url) == (
        "https://www.joybuy.de/dp/essenssch%C3%BCssel-%C3%B8-115-cm-h-6/10466952"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.argos.co.uk/product/3284476",
        "https://www.tesco.com/groceries/en-GB/products/123?sc_cmp=a&x=1#reviews",
        "https://www.joybuy.de/dp/essenssch%C3%BCssel/10466952",
    ],
)
def test_ascii_and_already_encoded_urls_are_unchanged(url: str) -> None:
    assert _to_request_uri(url) == url


def test_bright_data_validation_400_is_not_handed_to_detection() -> None:
    body = (
        '{"error":"Request validation failed","error_code":"validation",'
        '"details":[{"message":"\\"url\\" must be a valid uri"}]}'
    )
    with pytest.raises(BrightDataInfraError, match="error_code=validation"):
        _check_infra_error(400, body, {}, expect_html=True)


def test_target_site_400_html_still_passes_through() -> None:
    _check_infra_error(400, "<html>" + "x" * 6000 + "</html>", {}, expect_html=True)

import asyncio

import pytest

from src.search.layers.distinguishing import _PROMPT_HEADER, _build_user_msg
from src.search.layers.search import search_node
from src.search.layers.url_rules import clean_url, is_product_url
from src.search.models import CandidateEval, RawCandidate
from src.search.providers.base import SearchProvider


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.tesco.com/products/313169581?srsltid=abc",
            "https://www.tesco.com/products/313169581",
        ),
        (
            "https://www.argos.co.uk/product/123?utm_custom6=LIA&gStoreCode=5476",
            "https://www.argos.co.uk/product/123?gStoreCode=5476",
        ),
        (
            "https://example.com/item?gclid=abc&keep=a%20b#details",
            "https://example.com/item?keep=a%20b#details",
        ),
        (
            "https://www.amazon.nl/drukspuit-metaal?srsltid=x&k=drukspuit+metaal",
            "https://www.amazon.nl/drukspuit-metaal?k=drukspuit+metaal",
        ),
        (
            "https://www.amazon.nl/b?node=16462610031",
            "https://www.amazon.nl/b?node=16462610031",
        ),
        (
            "https://www.amazon.nl/dp/B09TRRYFMY?th=1&psc=1",
            "https://www.amazon.nl/dp/B09TRRYFMY?th=1&psc=1",
        ),
        ("https://example.com/item?", "https://example.com/item"),
    ],
)
def test_clean_url(url, expected):
    assert clean_url(url) == expected
    assert clean_url(clean_url(url)) == expected


@pytest.mark.parametrize(
    ("website", "url", "expected"),
    [
        (
            "tesco",
            "https://www.tesco.com/shop/en-GB/products/313169581",
            True,
        ),
        ("tesco", "https://www.tesco.com/shop/en-GB/search?q=tea", False),
        ("amazon.nl", "https://www.amazon.nl/dp/B09TRRYFMY", True),
        (
            "amazon.nl",
            "https://www.amazon.nl/drukspuit-metaal?s?k=drukspuit+metaal",
            False,
        ),
        ("amazon.nl", "https://www.amazon.nl/b?node=16462610031", False),
        ("unmapped", "https://example.com/anything", True),
    ],
)
def test_is_product_url(website, url, expected):
    assert is_product_url(website, url) is expected


class _DuplicateTrackingProvider(SearchProvider):
    name = "duplicate-tracking"

    async def search(self, query, k=10, country="uk"):
        return [
            RawCandidate(
                title="Product",
                url="https://www.tesco.com/products/313169581?srsltid=first",
            ),
            RawCandidate(
                title="Product duplicate",
                url="https://www.tesco.com/products/313169581?srsltid=second",
            ),
        ]


def test_search_node_cleans_before_deduplication():
    out = asyncio.run(
        search_node(
            {
                "provider": _DuplicateTrackingProvider(),
                "product_name": "Product",
                "website": "tesco",
                "country": "uk",
            }
        )
    )

    assert len(out["candidates"]) == 1
    assert out["candidates"][0].raw.url == (
        "https://www.tesco.com/products/313169581"
    )


def test_distinguishing_prompt_includes_url_and_gallery_instruction():
    candidate = CandidateEval(
        raw=RawCandidate(
            title="Product",
            url="https://example.com/item/1",
            snippet="One product",
        )
    )

    message = _build_user_msg("Product", [], {}, [candidate])

    assert "url: https://example.com/item/1" in message
    assert "single-product page" in _PROMPT_HEADER
    assert "never infer the product from a snippet" in _PROMPT_HEADER

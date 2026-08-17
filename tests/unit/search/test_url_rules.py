import pytest

from src.search.layers.url_rules import clean_url, is_product_url


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

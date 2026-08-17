from src.search.layers.search import search_node
from src.search.models import RawCandidate
from tests._support.providers import FakeSearchProvider


async def test_search_node_cleans_before_deduplication():
    provider = FakeSearchProvider(
        [
            RawCandidate(
                title="Product",
                url="https://www.tesco.com/products/313169581?srsltid=first",
            ),
            RawCandidate(
                title="Product duplicate",
                url="https://www.tesco.com/products/313169581?srsltid=second",
            ),
        ],
        name="duplicate-tracking",
    )
    out = await search_node(
        {
            "provider": provider,
            "product_name": "Product",
            "website": "tesco",
            "country": "uk",
        }
    )

    assert len(out["candidates"]) == 1
    assert out["candidates"][0].raw.url == (
        "https://www.tesco.com/products/313169581"
    )

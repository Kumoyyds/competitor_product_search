import asyncio

import pytest

from src.search.layers.domain_filter import _host_matches, domain_filter_node
from src.search.models import CandidateEval, RawCandidate, Verdict


def _make(*urls):
    return [CandidateEval(raw=RawCandidate(title=u, url=u)) for u in urls]


def test_domain_filter_passes_tesco_links():
    cands = _make(
        "https://www.tesco.com/groceries/en-GB/products/12345",
        "https://www.argos.co.uk/product/9999",
    )
    out = asyncio.run(domain_filter_node({"website": "tesco", "candidates": cands}))
    res = out["candidates"]
    assert res[0].trace.domain == Verdict.PASS and res[0].alive is True
    assert res[1].trace.domain == Verdict.FAIL and res[1].alive is False


def test_domain_filter_passes_subdomain():
    cands = _make("https://groceries.tesco.com/products/123")
    out = asyncio.run(domain_filter_node({"website": "tesco", "candidates": cands}))
    assert out["candidates"][0].trace.domain == Verdict.PASS


def test_domain_filter_unknown_website_fails_all():
    cands = _make("https://www.tesco.com/x", "https://www.argos.co.uk/y")
    out = asyncio.run(domain_filter_node({"website": "doesnotexist", "candidates": cands}))
    for c in out["candidates"]:
        assert c.trace.domain == Verdict.FAIL
        assert c.alive is False


# A domain_map value ending in "." is a registrable-name prefix: it must match
# every TLD Amazon operates under, without letting look-alike hosts through.
@pytest.mark.parametrize(
    "host, expected",
    [
        ("amazon.de", True),
        ("www.amazon.de", True),
        ("amazon.co.uk", True),
        ("www.amazon.co.uk", True),
        ("notamazon.de", False),
        ("amazon.de.evil.com", False),
        ("www.tesco.com", False),
    ],
)
def test_host_matches_prefix_target(host, expected):
    assert _host_matches(host, "amazon.") is expected


@pytest.mark.parametrize(
    "host, target, expected",
    [
        ("www.tesco.com", "tesco.com", True),
        ("tesco.com", "tesco.com", True),
        ("www.argos.co.uk", "argos.co.uk", True),
        ("faketesco.com", "tesco.com", False),
    ],
)
def test_host_matches_exact_target(host, target, expected):
    assert _host_matches(host, target) is expected


def test_domain_filter_uses_explicit_amazon_marketplace():
    cands = _make(
        "https://www.amazon.de/dp/B09TRRYFMY",
        "https://amazon.co.uk/dp/B0C123ABCD",
        "https://www.notamazon.de/dp/B0D456EFGH",
    )
    out = asyncio.run(
        domain_filter_node({"website": "amazon.co.uk", "candidates": cands})
    )
    res = out["candidates"]
    assert res[0].trace.domain == Verdict.FAIL and res[0].alive is False
    assert res[1].trace.domain == Verdict.PASS and res[1].alive is True
    assert res[2].trace.domain == Verdict.FAIL and res[2].alive is False


def test_domain_filter_rejects_gallery_pages_after_host_passes():
    cands = _make(
        "https://www.amazon.nl/dp/B09TRRYFMY",
        "https://www.amazon.nl/drukspuit-metaal?s?k=drukspuit+metaal",
        "https://www.amazon.nl/b?node=16462610031",
        "https://www.tesco.com/shop/en-GB/search?q=tea",
    )

    amazon_out = asyncio.run(
        domain_filter_node({"website": "amazon.nl", "candidates": cands[:3]})
    )
    tesco_out = asyncio.run(
        domain_filter_node({"website": "tesco", "candidates": cands[3:]})
    )

    assert amazon_out["candidates"][0].trace.domain == Verdict.PASS
    for candidate in amazon_out["candidates"][1:] + tesco_out["candidates"]:
        assert candidate.trace.domain == Verdict.FAIL
        assert candidate.alive is False
    assert amazon_out["domain_rejects"] == {"host": 0, "not_product_page": 2}
    assert tesco_out["domain_rejects"] == {"host": 0, "not_product_page": 1}

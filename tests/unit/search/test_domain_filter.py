import asyncio

from src.search.layers.domain_filter import domain_filter_node
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
    cands = _make("https://groceries.tesco.com/product/abc")
    out = asyncio.run(domain_filter_node({"website": "tesco", "candidates": cands}))
    assert out["candidates"][0].trace.domain == Verdict.PASS


def test_domain_filter_unknown_website_fails_all():
    cands = _make("https://www.tesco.com/x", "https://www.argos.co.uk/y")
    out = asyncio.run(domain_filter_node({"website": "doesnotexist", "candidates": cands}))
    for c in out["candidates"]:
        assert c.trace.domain == Verdict.FAIL
        assert c.alive is False

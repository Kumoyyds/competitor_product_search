from pathlib import Path
import re

from src.scraping.repair.evidence import locate_value_snippets, value_variants
from src.scraping.repair.prepass import _extract_url_product_id, _resolve_site, build_price_aware_context

URL = "https://www.joybuy.de/dp/example/10563197?requestIdentity=cmsPagePC"


def lookup(html, value="3.31", **kwargs):
    return locate_value_snippets(html, URL, value, keywords=["JoyPlus", "memberPromotion"], **kwargs)


def test_variants_and_exact_numeric_boundaries():
    patterns = value_variants("13099.05")
    assert all(any(re.search(p, x) for p in patterns)
               for x in ["13099.05", "13099,05", "13.099,05", "13,099.05"])
    for raw in ["13.31", "3.310", "3.31,99", "13,31", "3.31.99"]:
        assert lookup(f"<p>{raw}</p>") == []
    assert lookup("<p>unrelated</p>") == []
    for value in [None, True, [], {}]:
        assert value_variants(value) == []


def test_dom_script_split_nodes_and_attributes():
    raw = '<div class="price"><b>3</b><span>,31</span></div>'
    hits = lookup(raw)
    assert any(h.source == "DOM" and "div.price" in h.path for h in hits)
    assert lookup('<meta content="3.31" name="price">')
    assert lookup('<div>' + "x" * 2000 + '3,31' + "x" * 2000 + '</div>')
    script = r'<script>self.__next_f.push([1,"{\"skuId\":10563197,\"memberPromotion\":\"3.31\"}"])</script>'
    hits = lookup(script)
    assert hits[0].source == "script" and hits[0].path == "script[1]"
    assert r'\"' not in hits[0].text


def test_product_id_priority_then_keywords_and_bounded_long_scripts():
    html = ('<script>{"crossSell": "3.31"}' + 'x' * 5000 +
            '{"skuId":10563197,"memberPromotion":"3,31"}' + 'x' * 10000 + '</script>')
    hits = lookup(html)
    assert "10563197" in hits[0].text and "memberPromotion" in hits[0].text
    assert len(hits) <= 3 and all(len(h.text) <= 620 for h in hits)


def test_actual_joybuy_state_fixture_and_real_url_anchoring():
    raw = Path('tests/unit/scraping/fixtures/joybuy_de_golden_membership_evidence.html').read_text(encoding="utf-8")
    # Genuine raw fragments from golden_samples id=34; tests do not open a DB.
    actual_url = "https://www.joybuy.de/dp/schulanfangbai-jia-a-kuan-breite-nudeln/10137761"
    for amount in ("3.31", "3,31"):
        hits = locate_value_snippets(raw, actual_url, amount, keywords=["JoyPlus", "memberPromotion"], max_hits=8)
        assert any(hit.source == "DOM" and "3,31" in hit.text for hit in hits)
        assert any(hit.source == "script" and "plusMockPrice" in hit.text for hit in hits)
        assert any(hit.source == "script" and "JoyPlus" in hit.text for hit in hits)
    assert _resolve_site(actual_url) == "joybuy_de"
    assert _extract_url_product_id(actual_url, "joybuy_de") == "10137761"
    assert _extract_url_product_id('https://www.joybuy.de/category/10563197', 'joybuy_de') is None
    for url, site, expected in [
        ('https://argos.co.uk/product/1234', 'argos', '1234'),
        ('https://amazon.de/dp/B012345678', 'amazon', 'B012345678'),
        ('https://tesco.com/groceries/en-GB/products/1234', 'tesco', '1234'),
    ]:
        assert _extract_url_product_id(url, site) == expected
    context = build_price_aware_context(raw, actual_url)
    assert context.url_product_id == "10137761"


def test_nested_split_price_walks_all_suitable_ancestors_and_keeps_bounds():
    raw = ('<section>' + 'x' * 800 + '<div class="price">'
           '<span><b>3</b></span><span><b>,31</b></span></div></section>')
    hits = lookup(raw)
    assert any(hit.source == "DOM" and "div.price" in hit.path
               and '<span><b>3</b></span><span><b>,31</b></span>' in hit.text for hit in hits)
    assert all(len(hit.text) <= 600 for hit in hits)
    assert len({hit.text for hit in hits}) == len(hits)

from src.search.models import FinalVerdict, RawCandidate, Verdict
from src.search.pipeline import match_product
from tests._support.llm import ExplodingLLM, fake_llm
from tests._support.providers import FakeSearchProvider


async def test_shortcircuit_zero_results_skips_all_downstream(monkeypatch):
    provider = FakeSearchProvider()
    monkeypatch.setattr(
        "src.search.layers.distinguishing._get_llm", lambda: ExplodingLLM()
    )
    res = await match_product("Some Product", "tesco", provider=provider)
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.candidates_considered == 0
    assert res.layer_trace.domain is None
    assert res.layer_trace.brand is None
    assert res.layer_trace.numeric is None
    assert res.layer_trace.distinguishing is None


async def test_shortcircuit_domain_fail_skips_llm(monkeypatch):
    provider = FakeSearchProvider([
        RawCandidate(title="not tesco", url="https://www.argos.co.uk/x"),
        RawCandidate(title="also not tesco", url="https://www.amazon.co.uk/y"),
    ])
    monkeypatch.setattr(
        "src.search.layers.distinguishing._get_llm", lambda: ExplodingLLM()
    )
    res = await match_product("Some Product", "tesco", provider=provider)
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.layer_trace.domain == Verdict.FAIL
    assert res.layer_trace.brand is None
    assert res.layer_trace.numeric is None
    assert res.layer_trace.distinguishing is None


async def test_shortcircuit_gallery_pages_skip_llm(monkeypatch):
    provider = FakeSearchProvider([
        RawCandidate(
            title="Drukspuit Metaal",
            url="https://www.amazon.nl/drukspuit-metaal?s?k=drukspuit+metaal",
        ),
        RawCandidate(
            title="Garden Sprayers",
            url="https://www.amazon.nl/b?node=16462610031",
        ),
    ])
    monkeypatch.setattr(
        "src.search.layers.distinguishing._get_llm", lambda: ExplodingLLM()
    )
    res = await match_product("Snoerloze Drukspuit", "amazon.nl", provider=provider)
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.layer_trace.domain == Verdict.FAIL
    assert res.layer_trace.brand is None
    assert res.layer_trace.numeric is None
    assert res.layer_trace.distinguishing is None


async def test_shortcircuit_numeric_conflict_skips_llm(monkeypatch):
    provider = FakeSearchProvider([
        RawCandidate(
            title="Magic Rock Saucery 4 X 500ML",
            url="https://www.tesco.com/groceries/en-GB/products/111",
        ),
    ])
    monkeypatch.setattr(
        "src.search.layers.distinguishing._get_llm", lambda: ExplodingLLM()
    )
    res = await match_product(
        "Magic Rock Saucery 4 X 330ML", "tesco", provider=provider
    )
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.layer_trace.domain == Verdict.PASS
    assert res.layer_trace.numeric == Verdict.FAIL
    assert res.layer_trace.distinguishing is None


async def test_llm_invoked_when_base_passes(monkeypatch):
    provider = FakeSearchProvider([
        RawCandidate(
            title="Magic Rock Saucery 4 X 330ML",
            url="https://www.tesco.com/groceries/en-GB/products/111",
        ),
    ])
    llm = fake_llm('{"match_idx": 0, "reason": "same SKU"}')
    monkeypatch.setattr(
        "src.search.layers.distinguishing._get_llm", lambda: llm
    )
    res = await match_product(
        "Magic Rock Saucery 4 X 330ML", "tesco", provider=provider
    )
    assert res.verdict == FinalVerdict.MATCH
    assert res.layer_trace.distinguishing == Verdict.PASS


async def test_country_reaches_numeric_extraction_for_thousands_dot(monkeypatch):
    provider = FakeSearchProvider([
        RawCandidate(
            title="Flour Bag 1.000 g",
            url="https://www.amazon.nl/dp/B09TRRYFMY",
        ),
    ])
    llm = fake_llm('{"match_idx": 0, "reason": "same SKU"}')
    monkeypatch.setattr(
        "src.search.layers.distinguishing._get_llm", lambda: llm
    )
    res = await match_product(
        "Flour Bag 1000 g",
        "amazon.nl",
        country="de",
        provider=provider,
    )

    assert res.verdict == FinalVerdict.MATCH
    assert res.layer_trace.numeric == Verdict.PASS
    assert res.layer_trace.distinguishing == Verdict.PASS

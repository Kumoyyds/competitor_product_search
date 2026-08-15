import asyncio
from unittest.mock import patch, AsyncMock

import pytest

from src.search.models import FinalVerdict, RawCandidate, Verdict
from src.search.pipeline import match_product
from src.search.providers.base import SearchProvider


class FakeProvider(SearchProvider):
    name = "fake"

    def __init__(self, results):
        self._results = results
        self._calls = 0

    async def search(self, query, k=10, country="uk"):
        self._calls += 1
        return list(self._results)

    def calls_made(self):
        return self._calls

    async def aclose(self):
        return None


def _run(coro):
    return asyncio.run(coro)


def test_shortcircuit_zero_results_skips_all_downstream():
    provider = FakeProvider([])
    with patch("src.search.layers.distinguishing._get_llm") as get_llm:
        get_llm.side_effect = AssertionError("LLM must not be called")
        res = _run(match_product("Some Product", "tesco", provider=provider))
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.candidates_considered == 0
    assert res.layer_trace.domain is None
    assert res.layer_trace.brand is None
    assert res.layer_trace.numeric is None
    assert res.layer_trace.distinguishing is None


def test_shortcircuit_domain_fail_skips_llm():
    provider = FakeProvider([
        RawCandidate(title="not tesco", url="https://www.argos.co.uk/x"),
        RawCandidate(title="also not tesco", url="https://www.amazon.co.uk/y"),
    ])
    with patch("src.search.layers.distinguishing._get_llm") as get_llm:
        get_llm.side_effect = AssertionError("LLM must not be called")
        res = _run(match_product("Some Product", "tesco", provider=provider))
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.layer_trace.domain == Verdict.FAIL
    assert res.layer_trace.brand is None
    assert res.layer_trace.numeric is None
    assert res.layer_trace.distinguishing is None


def test_shortcircuit_gallery_pages_skip_llm():
    provider = FakeProvider([
        RawCandidate(
            title="Drukspuit Metaal",
            url="https://www.amazon.nl/drukspuit-metaal?s?k=drukspuit+metaal",
        ),
        RawCandidate(
            title="Garden Sprayers",
            url="https://www.amazon.nl/b?node=16462610031",
        ),
    ])
    with patch("src.search.layers.distinguishing._get_llm") as get_llm:
        get_llm.side_effect = AssertionError("LLM must not be called")
        res = _run(match_product("Snoerloze Drukspuit", "amazon.nl", provider=provider))
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.layer_trace.domain == Verdict.FAIL
    assert res.layer_trace.brand is None
    assert res.layer_trace.numeric is None
    assert res.layer_trace.distinguishing is None


def test_shortcircuit_numeric_conflict_skips_llm():
    provider = FakeProvider([
        RawCandidate(
            title="Magic Rock Saucery 4 X 500ML",
            url="https://www.tesco.com/groceries/en-GB/products/111",
        ),
    ])
    with patch("src.search.layers.distinguishing._get_llm") as get_llm:
        get_llm.side_effect = AssertionError("LLM must not be called")
        res = _run(match_product("Magic Rock Saucery 4 X 330ML", "tesco", provider=provider))
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.layer_trace.domain == Verdict.PASS
    assert res.layer_trace.numeric == Verdict.FAIL
    assert res.layer_trace.distinguishing is None


def test_llm_invoked_when_base_passes():
    provider = FakeProvider([
        RawCandidate(
            title="Magic Rock Saucery 4 X 330ML",
            url="https://www.tesco.com/groceries/en-GB/products/111",
        ),
    ])
    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(
        return_value=type(
            "Msg", (), {"content": '{"match_idx": 0, "reason": "same SKU"}'}
        )()
    )
    with patch("src.search.layers.distinguishing._get_llm", return_value=fake_llm):
        res = _run(match_product("Magic Rock Saucery 4 X 330ML", "tesco", provider=provider))
    assert res.verdict == FinalVerdict.MATCH
    assert res.layer_trace.distinguishing == Verdict.PASS


def test_country_reaches_numeric_extraction_for_thousands_dot():
    provider = FakeProvider([
        RawCandidate(
            title="Flour Bag 1.000 g",
            url="https://www.amazon.nl/dp/B09TRRYFMY",
        ),
    ])
    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(
        return_value=type("Msg", (), {"content": '{"match_idx": 0, "reason": "same SKU"}'})()
    )

    with patch("src.search.layers.distinguishing._get_llm", return_value=fake_llm):
        res = _run(
            match_product(
                "Flour Bag 1000 g",
                "amazon.nl",
                country="de",
                provider=provider,
            )
        )

    assert res.verdict == FinalVerdict.MATCH
    assert res.layer_trace.numeric == Verdict.PASS
    assert res.layer_trace.distinguishing == Verdict.PASS

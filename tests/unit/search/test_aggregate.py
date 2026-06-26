import asyncio

from src.search.layers.aggregate import aggregate_node
from src.search.models import (
    BaseAttributes,
    CandidateEval,
    FinalVerdict,
    RawCandidate,
    Verdict,
)


def _cand(url: str, **trace_kwargs) -> CandidateEval:
    c = CandidateEval(
        raw=RawCandidate(title=url, url=url),
        base=BaseAttributes(),
    )
    for k, v in trace_kwargs.items():
        setattr(c.trace, k, v)
    return c


def test_aggregate_match_when_distinguishing_pass():
    a = _cand("https://x/a", domain=Verdict.PASS, brand=Verdict.PASS,
              numeric=Verdict.PASS, distinguishing=Verdict.FAIL)
    b = _cand("https://x/b", domain=Verdict.PASS, brand=Verdict.PASS,
              numeric=Verdict.PASS, distinguishing=Verdict.PASS)
    out = asyncio.run(aggregate_node({"candidates": [a, b]}))
    res = out["result"]
    assert res.verdict == FinalVerdict.MATCH
    assert res.matched_candidate.url == "https://x/b"
    assert res.layer_trace.distinguishing == Verdict.PASS


def test_aggregate_no_match_picks_deepest_trace():
    # candidate stopped at numeric=fail; another stopped at brand=fail
    shallow = _cand("https://x/s", domain=Verdict.PASS, brand=Verdict.FAIL)
    deep = _cand("https://x/d", domain=Verdict.PASS, brand=Verdict.PASS,
                 numeric=Verdict.FAIL)
    out = asyncio.run(aggregate_node({"candidates": [shallow, deep]}))
    res = out["result"]
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.layer_trace.numeric == Verdict.FAIL
    assert res.layer_trace.brand == Verdict.PASS


def test_aggregate_empty_candidates_all_none_trace():
    out = asyncio.run(aggregate_node({"candidates": []}))
    res = out["result"]
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.candidates_considered == 0
    assert res.layer_trace.domain is None
    assert res.layer_trace.brand is None
    assert res.layer_trace.numeric is None
    assert res.layer_trace.distinguishing is None


def test_aggregate_all_domain_failed():
    a = _cand("https://x/a", domain=Verdict.FAIL)
    b = _cand("https://x/b", domain=Verdict.FAIL)
    out = asyncio.run(aggregate_node({"candidates": [a, b]}))
    res = out["result"]
    assert res.verdict == FinalVerdict.NO_MATCH
    assert res.layer_trace.domain == Verdict.FAIL
    assert res.layer_trace.brand is None

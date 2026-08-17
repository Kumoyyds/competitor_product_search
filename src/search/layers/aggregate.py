from __future__ import annotations

from typing import Any

from ..models import (
    CandidateEval,
    FinalVerdict,
    LayerTrace,
    MatchResult,
    Verdict,
)


def _deepest_candidate(candidates: list[CandidateEval]) -> CandidateEval | None:
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.trace.depth())


def _pick_matched(candidates: list[CandidateEval]) -> CandidateEval | None:
    for c in candidates:
        if c.trace.distinguishing == Verdict.PASS:
            return c
    return None


async def aggregate_node(state: dict[str, Any]) -> dict[str, Any]:
    candidates: list[CandidateEval] = state.get("candidates", [])

    if state.get("budget_exhausted"):
        reason = state.get("llm_reason") or "serper budget exhausted before completion"
    else:
        reason = state.get("llm_reason") or ""

    matched = _pick_matched(candidates)
    if matched is not None:
        result = MatchResult(
            verdict=FinalVerdict.MATCH,
            matched_candidate=matched.raw,
            layer_trace=matched.trace,
            candidates_considered=len(candidates),
            reason=reason,
        )
        return {**state, "result": result}

    if not candidates:
        result = MatchResult(
            verdict=FinalVerdict.NO_MATCH,
            matched_candidate=None,
            layer_trace=LayerTrace(),
            candidates_considered=0,
            reason=reason or "no search results",
        )
        return {**state, "result": result}

    deepest = _deepest_candidate(candidates)
    trace = deepest.trace if deepest else LayerTrace()
    result = MatchResult(
        verdict=FinalVerdict.NO_MATCH,
        matched_candidate=None,
        layer_trace=trace,
        candidates_considered=len(candidates),
        reason=reason,
    )
    return {**state, "result": result}

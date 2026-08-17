from __future__ import annotations

import time
import traceback
from typing import Any

from langgraph.graph import END, START, StateGraph

from .layers.aggregate import aggregate_node
from .layers.base_match import base_match_node
from .layers.distinguishing import distinguishing_node
from .layers.domain_filter import domain_filter_node
from .layers.search import search_node
from .models import CandidateEval
from . import config
from .layers.query_builder import build_queries
from .trace import get_recorder, utc_now


def _alive_count(candidates: list[CandidateEval]) -> int:
    return sum(1 for c in candidates if c.alive)


def _instrument(name: str, fn):
    """Record every graph node in one place without coupling layers to SQLite."""
    async def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        recorder = get_recorder()
        candidates_in = _alive_count(state.get("candidates", []))
        started_at = utc_now()
        started = time.monotonic()

        if recorder is not None and name == "search":
            recorder.set_query_variants(
                build_queries(
                    state["product_name"],
                    state["website"],
                    brand=state.get("brand"),
                    provider_name=state["provider"].name,
                )
            )

        try:
            out = await fn(state)
        except BaseException as exc:
            if recorder is not None:
                recorder.record_node_event(
                    node=name,
                    status="error",
                    error_kind=type(exc).__name__ or "Unknown",
                    error_message=str(exc),
                    traceback="".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                    detail={},
                    candidates_in=candidates_in,
                    candidates_out=0,
                    started_at=started_at,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            raise

        if recorder is None:
            return out

        candidates_out = _alive_count(out.get("candidates", []))
        status = "ok"
        error_kind = None
        error_message = None
        event_traceback = None
        detail: dict[str, Any] = {}

        if name == "domain_filter":
            detail["domain_rejects"] = out.get(
                "domain_rejects", {"host": 0, "not_product_page": 0}
            )
            if not config.domain_for(state["website"]):
                status = "error"
                error_kind = "DomainMapMissing"
                error_message = f"domain_map has no entry for {state['website']!r}"
                detail.update({"website": state["website"], "expected_domain": None})
        elif name == "distinguishing" and out.get("llm_error_kind"):
            status = "error"
            error_kind = out["llm_error_kind"]
            error_message = out.get("llm_error_message")
            event_traceback = out.get("llm_error_traceback")

        recorder.record_node_event(
            node=name,
            status=status,
            error_kind=error_kind,
            error_message=error_message,
            traceback=event_traceback,
            detail=detail,
            candidates_in=candidates_in,
            candidates_out=candidates_out,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        if name == "search":
            provider = state["provider"].name
            for message in out.get("search_errors", []):
                lower = str(message).lower()
                if out.get("budget_exhausted") and "budget" in lower:
                    kind = "BudgetExhausted"
                elif any(marker in lower for marker in ("429", "rate limit", "ratelimit")):
                    kind = "RateLimit"
                else:
                    kind = "SearchProviderError"
                recorder.record_node_event(
                    node="search",
                    status="warning",
                    error_kind=kind,
                    error_message=str(message),
                    detail={"provider": provider},
                    candidates_in=0,
                    candidates_out=candidates_out,
                    started_at=started_at,
                    duration_ms=0,
                )
        return out

    return wrapped


def _after_search(state: dict[str, Any]) -> str:
    if not state.get("candidates"):
        recorder = get_recorder()
        if recorder is not None:
            recorder.record_skipped(["domain_filter", "base_match", "distinguishing"])
        return "aggregate"
    return "domain_filter"


def _after_domain(state: dict[str, Any]) -> str:
    if _alive_count(state.get("candidates", [])) == 0:
        recorder = get_recorder()
        if recorder is not None:
            recorder.record_skipped(["base_match", "distinguishing"])
        return "aggregate"
    return "base_match"


def _after_base(state: dict[str, Any]) -> str:
    if _alive_count(state.get("candidates", [])) == 0:
        recorder = get_recorder()
        if recorder is not None:
            recorder.record_skipped(["distinguishing"])
        return "aggregate"
    return "distinguishing"


def build_graph():
    g: StateGraph = StateGraph(dict)
    g.add_node("search", _instrument("search", search_node))
    g.add_node("domain_filter", _instrument("domain_filter", domain_filter_node))
    g.add_node("base_match", _instrument("base_match", base_match_node))
    g.add_node("distinguishing", _instrument("distinguishing", distinguishing_node))
    g.add_node("aggregate", _instrument("aggregate", aggregate_node))

    g.add_edge(START, "search")
    g.add_conditional_edges("search", _after_search, {
        "domain_filter": "domain_filter",
        "aggregate": "aggregate",
    })
    g.add_conditional_edges("domain_filter", _after_domain, {
        "base_match": "base_match",
        "aggregate": "aggregate",
    })
    g.add_conditional_edges("base_match", _after_base, {
        "distinguishing": "distinguishing",
        "aggregate": "aggregate",
    })
    g.add_edge("distinguishing", "aggregate")
    g.add_edge("aggregate", END)

    return g.compile()

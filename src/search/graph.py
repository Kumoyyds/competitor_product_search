from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .layers.aggregate import aggregate_node
from .layers.base_match import base_match_node
from .layers.distinguishing import distinguishing_node
from .layers.domain_filter import domain_filter_node
from .layers.search import search_node
from .models import CandidateEval


def _alive_count(candidates: list[CandidateEval]) -> int:
    return sum(1 for c in candidates if c.alive)


def _after_search(state: dict[str, Any]) -> str:
    if not state.get("candidates"):
        return "aggregate"
    return "domain_filter"


def _after_domain(state: dict[str, Any]) -> str:
    if _alive_count(state.get("candidates", [])) == 0:
        return "aggregate"
    return "base_match"


def _after_base(state: dict[str, Any]) -> str:
    if _alive_count(state.get("candidates", [])) == 0:
        return "aggregate"
    return "distinguishing"


def build_graph():
    g: StateGraph = StateGraph(dict)
    g.add_node("search", search_node)
    g.add_node("domain_filter", domain_filter_node)
    g.add_node("base_match", base_match_node)
    g.add_node("distinguishing", distinguishing_node)
    g.add_node("aggregate", aggregate_node)

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

from __future__ import annotations

import asyncio
from typing import Any

from ..models import CandidateEval, RawCandidate
from ..providers.base import BudgetExhausted, SearchProvider, SearchProviderError
from .. import config
from .query_builder import build_queries


async def search_node(state: dict[str, Any]) -> dict[str, Any]:
    provider: SearchProvider = state["provider"]
    product_name: str = state["product_name"]
    website: str = state["website"]
    brand: str | None = state.get("brand")
    country: str = state.get("country", "uk")

    k = int(config.get("search", "k", default=10))
    queries = build_queries(product_name, website, brand=brand)

    try:
        results = await asyncio.gather(
            *(provider.search(q, k=k, country=country) for q in queries),
            return_exceptions=True,
        )
    except BudgetExhausted as e:
        return {**state, "candidates": [], "budget_error": str(e)}

    seen_urls: set[str] = set()
    candidates: list[CandidateEval] = []
    errors: list[str] = []
    budget_hit = False
    for r in results:
        if isinstance(r, BudgetExhausted):
            budget_hit = True
            errors.append(str(r))
            continue
        if isinstance(r, SearchProviderError):
            errors.append(str(r))
            continue
        if isinstance(r, Exception):
            errors.append(repr(r))
            continue
        for raw in r:
            if raw.url in seen_urls:
                continue
            seen_urls.add(raw.url)
            candidates.append(CandidateEval(raw=raw))

    return {
        **state,
        "candidates": candidates,
        "search_errors": errors,
        "budget_exhausted": budget_hit,
    }

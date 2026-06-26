from __future__ import annotations

import asyncio
from typing import Any

from ..cache import BaseExtractionCache, get_cache
from ..models import BaseAttributes, CandidateEval, Verdict
from .brand import compare_brands, extract_brands
from .numeric import compare_numerics, extract_numerics


def _extract(text: str, supplied_brand: str | None, cache: BaseExtractionCache | None) -> BaseAttributes:
    if cache:
        hit = cache.get(text)
        if hit is not None:
            return hit
    attrs = BaseAttributes(
        brands=extract_brands(text, supplied=supplied_brand),
        numerics=extract_numerics(text),
    )
    if cache:
        cache.set(text, attrs)
    return attrs


def _evaluate_one(cand: CandidateEval, query: BaseAttributes, cache: BaseExtractionCache | None) -> CandidateEval:
    cand.base = _extract(cand.raw.title, supplied_brand=None, cache=cache)

    bv = compare_brands(query.brands, cand.base.brands)
    cand.trace.brand = bv
    if bv == Verdict.FAIL:
        cand.alive = False
        return cand

    nv = compare_numerics(query.numerics, cand.base.numerics)
    cand.trace.numeric = nv
    if nv == Verdict.FAIL:
        cand.alive = False
    return cand


async def base_match_node(state: dict[str, Any]) -> dict[str, Any]:
    candidates: list[CandidateEval] = state.get("candidates", [])
    cache = get_cache()

    query_attrs = state.get("query_attrs")
    if query_attrs is None:
        query_attrs = BaseAttributes(
            brands=extract_brands(state["product_name"], supplied=state.get("brand")),
            numerics=extract_numerics(state["product_name"]),
        )

    alive = [c for c in candidates if c.alive]
    if not alive:
        return {**state, "candidates": candidates, "query_attrs": query_attrs}

    results = await asyncio.gather(
        *(asyncio.to_thread(_evaluate_one, c, query_attrs, cache) for c in alive)
    )
    by_id = {id(r): r for r in results}
    new_list = [by_id.get(id(c), c) for c in candidates]
    return {**state, "candidates": new_list, "query_attrs": query_attrs}

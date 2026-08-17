from __future__ import annotations

import asyncio
from typing import Any

from ..models import BaseAttributes, CandidateEval, Verdict
from .brand import compare_brands, extract_brands
from .numeric import compare_numerics, extract_numerics


def _extract(
    text: str,
    supplied_brand: str | None,
    country: str | None = None,
) -> BaseAttributes:
    return BaseAttributes(
        brands=extract_brands(text, supplied=supplied_brand),
        numerics=extract_numerics(text, country=country),
    )


def _evaluate_one(
    cand: CandidateEval,
    query: BaseAttributes,
    country: str | None = None,
) -> CandidateEval:
    cand.base = _extract(cand.raw.title, supplied_brand=None, country=country)

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
    country = state.get("country")

    query_attrs = state.get("query_attrs")
    if query_attrs is None:
        query_attrs = BaseAttributes(
            brands=extract_brands(state["product_name"], supplied=state.get("brand")),
            numerics=extract_numerics(state["product_name"], country=country),
        )

    alive = [c for c in candidates if c.alive]
    if not alive:
        return {**state, "candidates": candidates, "query_attrs": query_attrs}

    results = await asyncio.gather(
        *(
            asyncio.to_thread(_evaluate_one, c, query_attrs, country)
            for c in alive
        )
    )
    by_id = {id(r): r for r in results}
    new_list = [by_id.get(id(c), c) for c in candidates]
    return {**state, "candidates": new_list, "query_attrs": query_attrs}

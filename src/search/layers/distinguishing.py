from __future__ import annotations

import json
import re
from typing import Any

from .. import config
from ..models import CandidateEval, Verdict


_PROMPT_HEADER = (
    "You are a product-matching assistant. Decide whether ONE of the listed "
    "candidate listings is the same SKU as the query.\n"
    "Focus on variant-level distinctions (flavour, colour, version, pack size, etc.) — "
    "obvious brand and numeric mismatches have already been filtered upstream.\n"
    "Return STRICT JSON only, no extra text:\n"
    '{"match_idx": <int|null>, "reason": "<one short sentence>"}\n'
    "Use null for match_idx when none of the candidates is the same SKU."
)


def _render_attrs(numerics: dict[str, float]) -> str:
    if not numerics:
        return "{}"
    parts = [f"{k}={v:g}" for k, v in sorted(numerics.items())]
    return "{" + ", ".join(parts) + "}"


def _render_brands(brands: list[str]) -> str:
    return ", ".join(brands) if brands else "unknown"


def _build_user_msg(query_name: str, query_brands: list[str], query_num: dict[str, float],
                    alive: list[CandidateEval]) -> str:
    lines = [
        "Query:",
        f"  title: {query_name}",
        f"  brand: {_render_brands(query_brands)}",
        f"  numeric: {_render_attrs(query_num)}",
        "",
        "Candidates:",
    ]
    for i, c in enumerate(alive):
        lines.append(
            f"  [{i}] title: {c.raw.title}"
        )
        lines.append(
            f"      brand: {_render_brands(c.base.brands)} | numeric: {_render_attrs(c.base.numerics)}"
        )
        if c.raw.snippet:
            snip = c.raw.snippet[:160].replace("\n", " ")
            lines.append(f"      snippet: {snip}")
    return "\n".join(lines)


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _parse_response(text: str) -> tuple[int | None, str]:
    cleaned = _JSON_FENCE_RE.sub("", text or "").strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return None, "llm parse error"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, "llm parse error"
    idx = obj.get("match_idx")
    reason = str(obj.get("reason") or "").strip() or "no reason given"
    if idx is None:
        return None, reason
    try:
        return int(idx), reason
    except (TypeError, ValueError):
        return None, "llm parse error"


def _get_llm():
    from langchain_openai import ChatOpenAI

    model = config.get("llm", "model")
    base_url, api_key = config.resolve_llm_route(model)
    return ChatOpenAI(
        api_key=api_key,
        temperature=float(config.get("llm", "temperature", default=0.1)),
        base_url=base_url,
        model=model,
        timeout=float(config.get("llm", "timeout_s", default=60)),
    )


async def distinguishing_node(state: dict[str, Any]) -> dict[str, Any]:
    candidates: list[CandidateEval] = state.get("candidates", [])
    alive_idx = [i for i, c in enumerate(candidates) if c.alive]
    alive = [candidates[i] for i in alive_idx]
    if not alive:
        return {**state, "llm_reason": ""}

    query_attrs = state.get("query_attrs")
    user_msg = _build_user_msg(
        state["product_name"],
        query_attrs.brands if query_attrs else [],
        query_attrs.numerics if query_attrs else {},
        alive,
    )

    try:
        llm = _get_llm()
        msg = await llm.ainvoke(
            [
                {"role": "system", "content": _PROMPT_HEADER},
                {"role": "user", "content": user_msg},
            ]
        )
        raw_text = getattr(msg, "content", "") or ""
        match_idx, reason = _parse_response(raw_text)
    except Exception as e:
        for c in alive:
            c.trace.distinguishing = Verdict.FAIL
        return {**state, "llm_reason": f"llm error: {e}"}

    if match_idx is None or match_idx < 0 or match_idx >= len(alive):
        for c in alive:
            c.trace.distinguishing = Verdict.FAIL
        return {**state, "llm_reason": reason}

    for i, c in enumerate(alive):
        c.trace.distinguishing = Verdict.PASS if i == match_idx else Verdict.FAIL
    return {**state, "llm_reason": reason}

from __future__ import annotations

from typing import Any

from . import config
from .db import get_db, run_scope
from .graph import build_graph
from .models import FinalVerdict, LayerTrace, MatchResult
from .providers import SearchProvider, make_provider_chain
from .trace import get_recorder, record_task


_GRAPH = None


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def _fallback_no_match() -> MatchResult:
    return MatchResult(
        verdict=FinalVerdict.NO_MATCH,
        matched_candidate=None,
        layer_trace=LayerTrace(),
        candidates_considered=0,
        reason="pipeline returned no result",
    )


async def _match_product(
    product_name: str,
    website: str,
    brand: str | None,
    country: str,
    providers: list[SearchProvider],
) -> MatchResult:
    """Run the graph using an already-resolved, caller-managed provider chain."""
    if not providers:
        raise ValueError("provider chain must contain at least one provider")

    last_result: MatchResult | None = None
    used_provider: SearchProvider | None = None
    for provider in providers:
        recorder = get_recorder()
        if recorder is not None:
            recorder.begin_attempt(provider.name)
        initial: dict[str, Any] = {
            "product_name": product_name,
            "website": website.lower(),
            "brand": brand,
            "country": country,
            "provider": provider,
        }
        try:
            final_state = await _get_graph().ainvoke(initial)
        except BaseException as exc:
            if recorder is not None:
                recorder.end_attempt_error(exc)
            raise
        result = final_state.get("result")
        if not isinstance(result, MatchResult):
            result = _fallback_no_match()
        if recorder is not None:
            recorder.end_attempt(final_state, result)
        last_result = result
        used_provider = provider
        if result.verdict == FinalVerdict.MATCH:
            break

    assert last_result is not None and used_provider is not None
    if len(providers) > 1:
        reason = last_result.reason or ""
        sep = " " if reason else ""
        last_result = MatchResult(
            verdict=last_result.verdict,
            matched_candidate=last_result.matched_candidate,
            layer_trace=last_result.layer_trace,
            candidates_considered=last_result.candidates_considered,
            reason=f"{reason}{sep}(via {used_provider.name})",
        )
    recorder = get_recorder()
    if recorder is not None:
        recorder.final_provider = used_provider.name
    return last_result


async def match_product(
    product_name: str,
    website: str,
    brand: str | None = None,
    country: str = "uk",
    provider: SearchProvider | list[SearchProvider] | None = None,
    record: bool | None = None,
) -> MatchResult:
    """Run the product-matching pipeline, recording a standalone call by default.

    ``provider`` accepts one provider, an ordered provider chain, or ``None``
    to resolve the chain from ``maintain/search_config.yaml``. Set ``record``
    to ``False`` to suppress standalone DB tracing. Calls made inside an
    existing batch task always reuse that task's recorder.
    """
    own_providers = provider is None
    spec = None
    if own_providers:
        spec = config.get("search", "provider", default="serper")
        provider_names = [spec] if isinstance(spec, str) else list(spec or [])
        providers: list[SearchProvider] = []
    elif isinstance(provider, SearchProvider):
        providers = [provider]
        provider_names = [provider.name]
    else:
        providers = list(provider)
        provider_names = [item.name for item in providers]

    provider_chain = ",".join(str(name) for name in provider_names)

    async def execute() -> MatchResult:
        if own_providers and not providers:
            providers.extend(make_provider_chain(spec))
        return await _match_product(product_name, website, brand, country, providers)

    try:
        if get_recorder() is not None or record is False:
            return await execute()

        db = get_db()
        if db is None:
            return await execute()

        calls = (
            (lambda: {item.name: item.calls_made() for item in providers})
            if own_providers
            else None
        )
        job_config = {
            "product_name": product_name,
            "website": website,
            "brand": brand,
            "country": country,
        }
        async with run_scope(
            db=db,
            mode="single",
            total_tasks=1,
            job_config=job_config,
            provider_chain=provider_chain,
            provider_calls=calls,
            country=country,
            website=website,
            concurrency=1,
        ) as run_id:
            async with record_task(
                db,
                run_id=run_id,
                row_index=0,
                product_name=product_name,
                website=website,
                country=country,
                brand=brand,
            ) as recorder:
                result = await execute()
                recorder.complete(result, recorder.final_provider)
                return result
    finally:
        if own_providers:
            for item in providers:
                await item.aclose()

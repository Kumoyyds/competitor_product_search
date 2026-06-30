from __future__ import annotations

from typing import Any

from . import config
from .graph import build_graph
from .models import FinalVerdict, LayerTrace, MatchResult
from .providers import SearchProvider, make_provider_chain


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


async def match_product(
    product_name: str,
    website: str,
    brand: str | None = None,
    country: str = "uk",
    provider: SearchProvider | list[SearchProvider] | None = None,
) -> MatchResult:
    """Run the full search→domain→base→distinguishing→aggregate pipeline.

    ``provider`` accepts a single SearchProvider, a list of SearchProviders
    (ordered chain — on NO_MATCH, the next provider is tried), or None
    (resolve from ``search_config.yaml``'s ``search.provider``).
    """
    own_providers = False
    if provider is None:
        spec = config.get("search", "provider", default="serper")
        providers = make_provider_chain(spec)
        own_providers = True
    elif isinstance(provider, SearchProvider):
        providers = [provider]
    else:
        providers = list(provider)

    try:
        last_result: MatchResult | None = None
        used_provider: SearchProvider | None = None
        for p in providers:
            initial: dict[str, Any] = {
                "product_name": product_name,
                "website": website.lower(),
                "brand": brand,
                "country": country,
                "provider": p,
            }
            final_state = await _get_graph().ainvoke(initial)
            result = final_state.get("result")
            if not isinstance(result, MatchResult):
                result = _fallback_no_match()
            last_result = result
            used_provider = p
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
        return last_result
    finally:
        if own_providers:
            for p in providers:
                await p.aclose()

from __future__ import annotations

from typing import Any

from . import config
from .graph import build_graph
from .models import FinalVerdict, LayerTrace, MatchResult
from .providers import SearchProvider, make_provider


_GRAPH = None


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


async def match_product(
    product_name: str,
    website: str,
    brand: str | None = None,
    country: str = "uk",
    provider: SearchProvider | None = None,
) -> MatchResult:
    """Run the full search→domain→base→distinguishing→aggregate pipeline.

    Pass `provider` to share a single SerperProvider (and its budget counter)
    across many calls; otherwise a fresh one is created per call.
    """
    own_provider = False
    if provider is None:
        provider_name = config.get("search", "provider", default="serper")
        provider = make_provider(provider_name)
        own_provider = True

    initial: dict[str, Any] = {
        "product_name": product_name,
        "website": website.lower(),
        "brand": brand,
        "country": country,
        "provider": provider,
    }

    try:
        final_state = await _get_graph().ainvoke(initial)
    finally:
        if own_provider:
            await provider.aclose()

    result = final_state.get("result")
    if isinstance(result, MatchResult):
        return result
    return MatchResult(
        verdict=FinalVerdict.NO_MATCH,
        matched_candidate=None,
        layer_trace=LayerTrace(),
        candidates_considered=0,
        reason="pipeline returned no result",
    )

from __future__ import annotations

from ..models import RawCandidate
from .base import SearchProvider, SearchProviderError


class DuckDuckGoProvider(SearchProvider):
    """Placeholder for a future DuckDuckGo-based provider.

    Implements the SearchProvider contract so the rest of the pipeline can already
    target it via `make_provider("duckduckgo")`. Implementation deferred —
    raises at call time, never at construction.
    """

    name = "duckduckgo"

    def __init__(self, **kwargs):
        self._kwargs = kwargs

    async def search(self, query: str, k: int = 10, country: str = "uk") -> list[RawCandidate]:
        raise SearchProviderError(
            "DuckDuckGoProvider not implemented yet — set search.provider to 'serper' "
            "in search_config.yaml."
        )

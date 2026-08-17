from .base import SearchProvider, SearchProviderError, BudgetExhausted
from .serper import SerperProvider
from .duckduckgo import DuckDuckGoProvider


def make_provider(name: str, **kwargs) -> SearchProvider:
    name = name.lower()
    if name == "serper":
        return SerperProvider(**kwargs)
    if name == "duckduckgo":
        return DuckDuckGoProvider(**kwargs)
    raise ValueError(f"unknown search provider: {name}")


def make_provider_chain(
    spec: str | list[str],
    *,
    serper_max_calls: int | None = None,
) -> list[SearchProvider]:
    """Resolve a provider spec (str or list[str]) into an ordered list of providers.

    A bare string becomes a one-element list — single-provider runs flow through
    the same code path as chains of length > 1, just without escalation.
    Per-provider kwargs (e.g. *serper_max_calls*) apply only to the matching
    provider in the chain.
    """
    names = [spec] if isinstance(spec, str) else list(spec)
    providers: list[SearchProvider] = []
    for name in names:
        kwargs: dict = {}
        if name.lower() == "serper" and serper_max_calls is not None:
            kwargs["max_calls"] = serper_max_calls
        providers.append(make_provider(name, **kwargs))
    return providers


__all__ = [
    "SearchProvider",
    "SearchProviderError",
    "BudgetExhausted",
    "SerperProvider",
    "DuckDuckGoProvider",
    "make_provider",
    "make_provider_chain",
]

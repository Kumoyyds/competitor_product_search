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


__all__ = [
    "SearchProvider",
    "SearchProviderError",
    "BudgetExhausted",
    "SerperProvider",
    "DuckDuckGoProvider",
    "make_provider",
]

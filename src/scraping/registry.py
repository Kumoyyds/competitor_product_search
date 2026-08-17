from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scrapers.base import BaseScraper

_REGISTRY: dict[str, list[type[BaseScraper]]] = {}


def register_scraper(site: str, order: int = 1):
    """Decorator to register a scraper class for a site (D3).

    Lower order = higher priority (tried first in fallback chain).
    """

    def decorator(cls: type[BaseScraper]) -> type[BaseScraper]:
        lst = _REGISTRY.setdefault(site, [])
        lst.append(cls)
        lst.sort(key=lambda c: getattr(c, "_order", 999))
        cls._order = order  # type: ignore[attr-defined]
        cls.site = site
        lst.sort(key=lambda c: getattr(c, "_order", 999))
        return cls

    return decorator


def get_scrapers(site: str) -> list[type[BaseScraper]]:
    return list(_REGISTRY.get(site, []))


def get_all_sites() -> list[str]:
    return list(_REGISTRY.keys())

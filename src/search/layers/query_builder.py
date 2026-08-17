from __future__ import annotations

import re

from .. import config


def build_queries(
    product_name: str,
    website: str,
    brand: str | None = None,
    provider_name: str | None = None,
) -> list[str]:
    """Return provider-specific, deduplicated search queries."""
    del brand  # Retained in the public signature for caller compatibility.
    base = product_name.strip()
    retailer = website.strip()
    mode = config.query_mode_for(provider_name or "")
    domain = config.domain_for(website)
    out: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    def _add_for_name(name: str) -> None:
        if mode in {"keyword", "both"} or domain is None:
            _add(f"{name} {retailer}")
        if mode in {"sitename", "both"} and domain is not None:
            _add(f"{name} site:{domain}")

    _add_for_name(base)
    if config.strip_parens_enabled():
        _add_for_name(re.sub(r"\([^)]*\)", "", base))
    return out

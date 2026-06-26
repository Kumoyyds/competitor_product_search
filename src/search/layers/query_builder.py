from __future__ import annotations

import re

from .. import config


def build_queries(product_name: str, website: str, brand: str | None = None) -> list[str]:
    """Return the deduped list of query variants to fire against the search provider.

    Per spec §3.1: include retailer keyword for recall; never use a `site:` operator
    (domain filtering happens at layer 2). Variants are simple rule-based for now,
    LLM rewrite hook can be added later behind the same interface.
    """
    retailer = config.retailer_keyword_for(website)
    base = product_name.strip()
    variants_cfg = config.get("search", "query_variants", default=["raw"]) or ["raw"]

    raw_q = f"{base} {retailer}".strip()
    out: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    if "raw" in variants_cfg:
        _add(raw_q)
    if "with_retailer_kw" in variants_cfg:
        _add(raw_q)
    if "with_brand" in variants_cfg and brand:
        _add(f"{brand} {base} {retailer}")
    if "strip_parens" in variants_cfg:
        stripped = re.sub(r"\([^)]*\)", "", base)
        _add(f"{stripped} {retailer}")

    if not out:
        _add(raw_q)
    return out

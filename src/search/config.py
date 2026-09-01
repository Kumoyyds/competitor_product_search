from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import yaml
from src.common.llm_client import resolve_llm_route as _resolve_shared_llm_route


# Maintained file — edit thresholds / domain map / units / LLM model here.
# See "Files to maintain" in src/search/README.md.
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maintain", "search_config.yaml")

@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get(*keys: str, default: Any = None) -> Any:
    cur: Any = load_config()
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def resolve_llm_route(model: str) -> tuple[str, str]:
    """Compatibility wrapper around the shared Search/Matching router."""
    return _resolve_shared_llm_route(model)


def domain_for(website: str) -> str | None:
    return get("domain_map", website.lower())


def query_mode_for(provider_name: str) -> str:
    """Return the configured query strategy for *provider_name*."""
    mode = get("search", "query_mode", provider_name.lower(), default="keyword")
    if not isinstance(mode, str) or mode not in {"keyword", "sitename", "both"}:
        raise ValueError(
            f"invalid search.query_mode for {provider_name!r}: {mode!r}; "
            "expected keyword, sitename, or both"
        )
    return mode


def strip_parens_enabled() -> bool:
    return bool(get("search", "strip_parens", default=True))

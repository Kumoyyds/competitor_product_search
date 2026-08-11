from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import yaml
from dotenv import load_dotenv


# Maintained file — edit thresholds / domain map / units / LLM model here.
# See "Files to maintain" in src/search/README.md.
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maintain", "search_config.yaml")

# Maintained file — add a vendor entry here when introducing a new LLM provider.
# See "Files to maintain" in src/search/README.md.
_ROUTER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maintain", "llm_router_config.yaml")


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


@lru_cache(maxsize=1)
def load_router_config() -> dict[str, Any]:
    with open(_ROUTER_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_llm_route(model: str) -> tuple[str, str]:
    """Return (base_url, api_key) for a model name via keyword routing."""
    load_dotenv()
    providers: dict[str, Any] = load_router_config().get("providers", {})
    model_lower = model.lower()
    best_keyword = None
    for keyword in providers:
        if keyword.lower() in model_lower:
            if best_keyword is None or len(keyword) > len(best_keyword):
                best_keyword = keyword
    if best_keyword is None:
        raise RuntimeError(
            f"No LLM route matches model {model!r}. "
            f"Add a keyword entry to {_ROUTER_CONFIG_PATH}. "
            f"Available keywords: {', '.join(sorted(providers)) or '(none configured)'}"
        )
    entry = providers[best_keyword]
    key_name = entry["key_name"]
    api_key = os.getenv(key_name)
    if not api_key:
        raise RuntimeError(
            f"Model {model!r} routes to provider {best_keyword!r} which needs env var "
            f"{key_name!r}, but it is unset. Set it in .env, or fix the route in {_ROUTER_CONFIG_PATH}."
        )
    return entry["base_url"], api_key


def domain_for(website: str) -> str | None:
    return get("domain_map", website.lower())


def retailer_keyword_for(website: str) -> str:
    return get("search", "retailer_keywords", website.lower(), default=website)

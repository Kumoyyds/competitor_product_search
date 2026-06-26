from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import yaml


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


def domain_for(website: str) -> str | None:
    return get("domain_map", website.lower())


def retailer_keyword_for(website: str) -> str:
    return get("search", "retailer_keywords", website.lower(), default=website)

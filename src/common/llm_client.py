"""Shared OpenAI-compatible LLM routing for search, matching, and scraping."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_DEFAULT_ROUTER_PATH = Path(__file__).with_name("llm_router_config.yaml")
_router_path = _DEFAULT_ROUTER_PATH


class UnknownModelError(RuntimeError):
    """Raised when a model name matches no provider keyword in the router table."""


@dataclass(frozen=True)
class LlmRoute:
    provider: str
    base_url: str
    key_name: str


def set_router_config_path(path: Path | None) -> None:
    """Test hook: point the router at a different YAML file (or reset to default)."""
    global _router_path
    _router_path = path or _DEFAULT_ROUTER_PATH
    load_router_config.cache_clear()


@lru_cache(maxsize=None)
def load_router_config(path: Path | None = None) -> dict[str, Any]:
    target = path or _router_path
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{target}: top level must be a mapping")
    return data


def resolve_llm_endpoint(model: str) -> LlmRoute:
    """Resolve ``model`` to its provider route by longest keyword match.

    Does not touch the environment -- callers with their own key-resolution
    policy (e.g. scraping's ``ScrapingConfig.api_key_for``) can use this
    without triggering ``load_dotenv()``'s process-wide environment mutation.
    """
    providers = load_router_config().get("providers") or {}
    matches = [key for key in providers if str(key).lower() in model.lower()]
    if not matches:
        available = ", ".join(sorted(map(str, providers))) or "(none configured)"
        raise UnknownModelError(
            f"No LLM route matches model {model!r}. Add it to {_router_path}. "
            f"Available keywords: {available}"
        )
    keyword = max(matches, key=lambda item: len(str(item)))
    entry = providers[keyword]
    return LlmRoute(
        provider=str(keyword),
        base_url=str(entry["base_url"]),
        key_name=str(entry["key_name"]),
    )


def resolve_llm_route(model: str) -> tuple[str, str]:
    """Resolve ``model`` to ``(base_url, api_key)`` by longest keyword."""
    load_dotenv()
    route = resolve_llm_endpoint(model)
    api_key = os.getenv(route.key_name)
    if not api_key:
        raise RuntimeError(
            f"Model {model!r} routes to {route.provider!r}, which requires "
            f"{route.key_name!r}"
        )
    return route.base_url, api_key


def make_chat_model(
    *, model: str, temperature: float, timeout_s: float
):
    """Build a LangChain ChatOpenAI client using the shared router."""
    from langchain_openai import ChatOpenAI

    base_url, api_key = resolve_llm_route(model)
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        timeout=timeout_s,
        # Callers own the retry budget so request and parse failures follow one
        # predictable policy instead of multiplying LangChain's retries.
        max_retries=0,
    )

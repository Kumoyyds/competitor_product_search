"""Shared OpenAI-compatible LLM routing for search and matching."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ROUTER_PATH = Path(__file__).with_name("llm_router_config.yaml")


@lru_cache(maxsize=1)
def load_router_config() -> dict[str, Any]:
    data = yaml.safe_load(_ROUTER_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{_ROUTER_PATH}: top level must be a mapping")
    return data


def resolve_llm_route(model: str) -> tuple[str, str]:
    """Resolve ``model`` to ``(base_url, api_key)`` by longest keyword."""
    load_dotenv()
    providers = load_router_config().get("providers") or {}
    matches = [key for key in providers if str(key).lower() in model.lower()]
    if not matches:
        available = ", ".join(sorted(map(str, providers))) or "(none configured)"
        raise RuntimeError(
            f"No LLM route matches model {model!r}. Add it to {_ROUTER_PATH}. "
            f"Available keywords: {available}"
        )
    keyword = max(matches, key=lambda item: len(str(item)))
    entry = providers[keyword]
    key_name = str(entry["key_name"])
    api_key = os.getenv(key_name)
    if not api_key:
        raise RuntimeError(
            f"Model {model!r} routes to {keyword!r}, which requires {key_name!r}"
        )
    return str(entry["base_url"]), api_key


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

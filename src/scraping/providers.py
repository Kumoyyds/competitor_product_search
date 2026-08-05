"""LLM provider registry -- the single place to add a model or vendor.

Adding a model requires one line in an existing provider's ``models`` tuple.
Adding a vendor requires one registry entry and its API key in ``.env``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .config import get_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    base_url: str
    key_name: str
    models: tuple[str, ...]
    thinking_extra_body: Optional[dict[str, Any]] = None
    non_thinking_extra_body: Optional[dict[str, Any]] = None
    supports_json_object: bool = True
    # Output cap requested per call. Providers apply a small default (DeepSeek
    # uses 8192) when the field is omitted, which truncates parser-generation
    # replies mid-JSON; the SDK then raises LengthFinishReasonError and drops
    # the partial content. ``None`` keeps the provider default.
    max_output_tokens: Optional[int] = None


PROVIDERS: dict[str, ProviderSpec] = {
    "qwen": ProviderSpec(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        key_name="QWEN_KEY",
        models=(
            "qwen3.7-plus",
            # Legacy project spelling; retained for compatibility.
            "qwen-3.7-plus",
            "qwen3.7-flash",
        ),
        thinking_extra_body={"enable_thinking": True},
    ),
    "deepseek": ProviderSpec(
        base_url="https://api.deepseek.com",
        key_name="DEEPSEEK_KEY",
        models=("deepseek-v4-flash", "deepseek-v4-pro"),
        thinking_extra_body={"thinking": {"type": "enabled"}},
        # DeepSeek V4 defaults to thinking mode, so explicitly disable it on
        # ordinary ladder nodes to preserve the existing last-node-only policy.
        non_thinking_extra_body={"thinking": {"type": "disabled"}},
        # V4 allows up to 384K output; 32K is far above any parser we generate
        # while staying well clear of the cap.
        max_output_tokens=32768,
    ),
}

DEFAULT_PROVIDER = "qwen"


def resolve_provider(model: str) -> tuple[str, ProviderSpec]:
    """Resolve a model name to its bare model id and provider specification.

    An explicit ``provider/model`` prefix wins. Otherwise the registry's model
    tuples are searched. Unknown names fall back to the default provider so
    offline tests and private deployments can keep using unregistered model ids.
    """
    if "/" in model:
        provider_name, bare_model = model.split("/", 1)
        if provider_name in PROVIDERS and bare_model:
            return bare_model, PROVIDERS[provider_name]

    for spec in PROVIDERS.values():
        if model in spec.models:
            return model, spec

    logger.warning(
        "Unknown LLM model %r; falling back to provider=%s",
        model,
        DEFAULT_PROVIDER,
    )
    return model, PROVIDERS[DEFAULT_PROVIDER]


def make_chat_client(
    model: str,
    temperature: float = 0.1,
    enable_thinking: bool = False,
    *,
    purpose: str = "",
    max_tokens: Optional[int] = None,
):
    """Build a LangChain ChatOpenAI client for a registered provider.

    ``max_tokens`` overrides the provider's registered output cap; when both are
    ``None`` no cap is sent and the provider's own default applies.

    Returns ``None`` when ``langchain_openai`` is unavailable or the resolved
    provider key is unset, preserving the scraping module's graceful-degradation
    behavior.
    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.error("langchain_openai not installed%s", _purpose_suffix(purpose))
        return None

    bare_model, spec = resolve_provider(model)
    provider_name = _provider_name(spec)
    cfg = get_config()
    api_key = cfg.api_key_for(spec.key_name)
    if not api_key:
        logger.warning(
            "%s not set -- cannot invoke provider=%s%s",
            spec.key_name,
            provider_name,
            _purpose_suffix(purpose),
        )
        return None

    model_kwargs: dict[str, Any] = {}
    if spec.supports_json_object:
        model_kwargs["response_format"] = {"type": "json_object"}

    extra_body = (
        spec.thinking_extra_body
        if enable_thinking
        else spec.non_thinking_extra_body
    )
    if enable_thinking and extra_body is None:
        logger.debug(
            "provider=%s has no parameter-level thinking toggle; select a "
            "reasoning model in the ladder instead",
            provider_name,
        )

    base_url = spec.base_url
    # Retain the pre-M18 Qwen endpoint override while keeping vendor-specific
    # handling inside this registry module.
    if spec is PROVIDERS["qwen"] and cfg.qwen_base_url:
        base_url = cfg.qwen_base_url

    # Sent through extra_body, not ChatOpenAI(max_tokens=...): langchain renames
    # that field to `max_completion_tokens`, which DeepSeek accepts and then
    # silently ignores (verified — the reply runs past the requested cap).
    # `max_tokens` is the parameter these OpenAI-compatible endpoints honor.
    output_cap = max_tokens if max_tokens is not None else spec.max_output_tokens
    if output_cap is not None:
        extra_body = {**(extra_body or {}), "max_tokens": output_cap}

    logger.info(
        "Creating LLM client purpose=%s provider=%s model=%s base_url=%s max_tokens=%s",
        purpose or "unspecified",
        provider_name,
        bare_model,
        base_url,
        output_cap if output_cap is not None else "provider-default",
    )
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=bare_model,
        temperature=temperature,
        model_kwargs=model_kwargs,
        extra_body=extra_body,
    )


def _provider_name(spec: ProviderSpec) -> str:
    for name, registered in PROVIDERS.items():
        if registered is spec:
            return name
    return "unknown"


def _purpose_suffix(purpose: str) -> str:
    return f" for {purpose}" if purpose else ""

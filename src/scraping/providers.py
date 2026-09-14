"""LLM vendor call-capability registry.

Routing (model name -> vendor -> base_url + API-key env name) lives in the
shared ``src/common/llm_router_config.yaml``, resolved by keyword match. This
module holds only per-vendor CALL CAPABILITIES that the shared router doesn't
know about: thinking-mode parameters, output-token caps, and JSON-mode
support. Adding a vendor here is optional -- a vendor with no entry gets a
neutral default (no special call parameters). Add an entry only when a vendor
needs one of these capability overrides.
"""

from __future__ import annotations

import atexit
import asyncio
import logging
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Optional

from src.common.llm_client import LlmRoute, UnknownModelError, resolve_llm_endpoint

from .config import get_config

logger = logging.getLogger(__name__)

_CHAT_CLIENTS: dict[tuple[Any, ...], Any] = {}
_CHAT_CLIENTS_LOCK = threading.Lock()
_CLIENT_CLOSE_TASKS: set[asyncio.Task[None]] = set()
_CLIENT_CLOSE_TASKS_LOCK = threading.Lock()


@dataclass(frozen=True)
class ProviderSpec:
    thinking_extra_body: Optional[dict[str, Any]] = None
    non_thinking_extra_body: Optional[dict[str, Any]] = None
    supports_json_object: bool = True
    # Output cap requested per call. Providers apply a small default (DeepSeek
    # uses 8192) when the field is omitted, which truncates parser-generation
    # replies mid-JSON; the SDK then raises LengthFinishReasonError and drops
    # the partial content. ``None`` keeps the provider default.
    max_output_tokens: Optional[int] = None
    # Thinking providers count reasoning and visible content against one output
    # budget, so their final ladder rung may require a larger dedicated cap.
    thinking_max_output_tokens: Optional[int] = None


_DEFAULT_SPEC = ProviderSpec()

# Keys here must match the provider keywords in
# src/common/llm_router_config.yaml -- that file owns base_url/key_name.
PROVIDERS: dict[str, ProviderSpec] = {
    "qwen": ProviderSpec(
        thinking_extra_body={"enable_thinking": True},
    ),
    "deepseek": ProviderSpec(
        thinking_extra_body={"thinking": {"type": "enabled"}},
        # DeepSeek V4 defaults to thinking mode, so explicitly disable it on
        # ordinary ladder nodes to preserve the existing last-node-only policy.
        non_thinking_extra_body={"thinking": {"type": "disabled"}},
        # V4 allows up to 384K output; 32K is far above any parser we generate
        # while staying well clear of the cap.
        max_output_tokens=32768,
        thinking_max_output_tokens=65536,
    ),
}


def resolve_provider(model: str) -> tuple[str, "LlmRoute", ProviderSpec]:
    """Resolve a model name to its bare model id, route, and call capabilities.

    An explicit ``provider/model`` prefix is validated against the shared
    router table and stripped before routing the remainder. Otherwise the
    whole string is routed by the shared router's keyword match. Unroutable
    names raise ``UnknownModelError`` -- there is no silent fallback.
    """
    if "/" in model:
        prefix, rest = model.split("/", 1)
        if rest:
            route = resolve_llm_endpoint(prefix)
            return rest, route, PROVIDERS.get(route.provider, _DEFAULT_SPEC)

    route = resolve_llm_endpoint(model)
    return model, route, PROVIDERS.get(route.provider, _DEFAULT_SPEC)


def validate_model_ladders(cfg: Any = None) -> None:
    """Eagerly resolve every configured model ladder entry.

    Call this at a batch entry point (cold start, orchestrator run) so a
    typo'd model name raises before any paid scraping/repair spend, instead of
    surfacing lazily on the first LLM call deep in the repair ladder.
    """
    cfg = cfg or get_config()
    for ladder_name in ("repair_model_ladder", "cold_start_model_ladder"):
        for model in getattr(cfg, ladder_name):
            resolve_provider(model)


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

    bare_model, route, spec = resolve_provider(model)
    provider_name = route.provider
    cfg = get_config()
    api_key = cfg.api_key_for(route.key_name)
    if not api_key:
        logger.warning(
            "%s not set -- cannot invoke provider=%s%s",
            route.key_name,
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

    # A configured per-provider override (e.g. the legacy qwen_base_url field)
    # wins; otherwise use the shared router's endpoint for this vendor.
    base_url = cfg.base_url_for(provider_name) or route.base_url

    # Sent through extra_body, not ChatOpenAI(max_tokens=...): langchain renames
    # that field to `max_completion_tokens`, which DeepSeek accepts and then
    # silently ignores (verified — the reply runs past the requested cap).
    # `max_tokens` is the parameter these OpenAI-compatible endpoints honor.
    output_cap = (
        max_tokens
        if max_tokens is not None
        else spec.thinking_max_output_tokens
        if enable_thinking and spec.thinking_max_output_tokens is not None
        else spec.max_output_tokens
    )
    if output_cap is not None:
        extra_body = {**(extra_body or {}), "max_tokens": output_cap}

    try:
        loop_identity: weakref.ReferenceType[Any] | None = weakref.ref(
            asyncio.get_running_loop()
        )
    except RuntimeError:
        loop_identity = None

    # Include endpoint/key/class identity as well as semantic call parameters.
    # This keeps cache reuse safe across set_config(), key rotation, endpoint
    # overrides, and tests that replace ChatOpenAI. ``purpose`` is only logging
    # metadata and deliberately does not split connection pools.
    cache_key = (
        ChatOpenAI,
        provider_name,
        bare_model,
        float(temperature),
        enable_thinking,
        output_cap,
        base_url,
        api_key,
        loop_identity,
    )
    with _CHAT_CLIENTS_LOCK:
        cached = _CHAT_CLIENTS.get(cache_key)
        if cached is not None:
            return cached

        logger.info(
            "Creating LLM client purpose=%s provider=%s model=%s base_url=%s "
            "thinking=%s max_tokens=%s",
            purpose or "unspecified",
            provider_name,
            bare_model,
            base_url,
            enable_thinking,
            output_cap if output_cap is not None else "provider-default",
        )
        client = ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=bare_model,
            temperature=temperature,
            model_kwargs=model_kwargs,
            extra_body=extra_body,
        )
        _CHAT_CLIENTS[cache_key] = client
        return client


async def _close_async_roots(roots: list[Any]) -> None:
    for root in roots:
        try:
            await root.close()
        except Exception:
            logger.exception("failed to close an async LLM client")


def _close_task_done(task: asyncio.Task[None]) -> None:
    with _CLIENT_CLOSE_TASKS_LOCK:
        _CLIENT_CLOSE_TASKS.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        logger.error("async LLM client cleanup task was cancelled")
    except Exception:
        logger.exception("async LLM client cleanup task failed")


def _dispose_chat_clients(*, close_async: bool) -> None:
    with _CHAT_CLIENTS_LOCK:
        clients = list(_CHAT_CLIENTS.values())
        _CHAT_CLIENTS.clear()

    sync_roots: dict[int, Any] = {}
    async_roots: dict[int, Any] = {}
    for client in clients:
        sync_root = getattr(client, "root_client", None)
        async_root = getattr(client, "root_async_client", None)
        if sync_root is not None and callable(getattr(sync_root, "close", None)):
            sync_roots[id(sync_root)] = sync_root
        if async_root is not None and callable(getattr(async_root, "close", None)):
            async_roots[id(async_root)] = async_root

    for root in sync_roots.values():
        try:
            root.close()
        except Exception:
            logger.exception("failed to close a synchronous LLM client")

    if not async_roots:
        return
    if not close_async:
        logger.debug(
            "interpreter shutdown -- skipping async LLM client close because "
            "its owning event loop may already be closed"
        )
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_close_async_roots(list(async_roots.values())))
    else:
        task = loop.create_task(_close_async_roots(list(async_roots.values())))
        with _CLIENT_CLOSE_TASKS_LOCK:
            _CLIENT_CLOSE_TASKS.add(task)
        task.add_done_callback(_close_task_done)


def close_chat_clients() -> None:
    """Clear cached clients and best-effort close their HTTP connection pools."""
    _dispose_chat_clients(close_async=True)


def reset_chat_clients() -> None:
    """Testing/configuration hook: dispose all cached clients."""
    close_chat_clients()


def _close_chat_clients_at_exit() -> None:
    """Close only loop-independent client resources during interpreter exit."""
    _dispose_chat_clients(close_async=False)


atexit.register(_close_chat_clients_at_exit)


def _purpose_suffix(purpose: str) -> str:
    return f" for {purpose}" if purpose else ""

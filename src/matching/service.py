from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol, Sequence

from src.common.llm_client import make_chat_model, resolve_llm_route
from src.models import (
    DecisionNode,
    DecisionNodeRecord,
    DecisionSource,
    EvidenceStatus,
    InputItem,
    ProductData,
    ProductMatchResult,
    ProductMatchVerdict,
    VisionStatus,
)

from . import config
from .attributes import compare_variants, normalize_gtin


class MatchingError(RuntimeError):
    """The matching operation could not produce a business verdict."""

    def __init__(
        self, message: str, *, trace: list[DecisionNodeRecord] | None = None
    ) -> None:
        super().__init__(message)
        self.trace = list(trace or [])


class MatchingBatchError(MatchingError):
    def __init__(
        self,
        results: list[ProductMatchResult | None],
        errors: dict[int, MatchingError],
    ) -> None:
        super().__init__(f"{len(errors)} matching item(s) failed technically")
        self.results = results
        self.errors = errors


class ChatModel(Protocol):
    async def ainvoke(self, messages: list[tuple[str, str]]) -> Any: ...


@dataclass(frozen=True, slots=True)
class MatchRequest:
    item: InputItem
    product: ProductData


_SYSTEM_PROMPT = """You verify whether an intended SKU and a scraped listing are the same exact product variant.
Use only the supplied evidence. Missing information is unknown, not a conflict. Do not infer differences that are not directly supported.
Judge which attributes are variant-defining based on the product category and item. Treat differences as material only if they change the product’s identity or what the customer is purchasing. Do not reject based solely on superficial differences in wording, packaging, label presentation, or marketing claims.
A GTIN conflict is strong negative evidence, but not an automatic rejection. Visual evidence provides observations, not a verdict.
If the evidence is insufficient to confirm the exact SKU, return no_match.
Return strict JSON only: {\"verdict\": \"match\"|\"no_match\", \"reasoning\": \"one concise sentence\"}."""
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _gtin_status(item: InputItem, product: ProductData) -> tuple[EvidenceStatus, str | None, str | None]:
    left = normalize_gtin(item.gtin)
    right = normalize_gtin(product.gtin)
    if left is None or right is None:
        return EvidenceStatus.UNKNOWN, left, right
    if left == right:
        return EvidenceStatus.PASS, left, right
    return EvidenceStatus.CONFLICT, left, right


def _build_prompt(
    request: MatchRequest,
    *,
    gtin_status: EvidenceStatus,
    variant_status: EvidenceStatus,
    evidence: dict[str, Any],
    vision_status: VisionStatus,
    vision_comment: str | None,
) -> str:
    payload = {
        "input": request.item.model_dump(),
        "scraped_product": request.product.model_dump(mode="json", exclude={"raw"}),
        "rule_evidence": {
            "gtin_status": gtin_status.value,
            "variant_status": variant_status.value,
            **evidence,
        },
        "vision": {
            "status": vision_status.value,
            "comment": vision_comment,
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _parse_llm(text: str) -> tuple[ProductMatchVerdict, str]:
    cleaned = _FENCE_RE.sub("", text or "").strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("LLM response must be an object")
    try:
        verdict = ProductMatchVerdict(value.get("verdict"))
    except ValueError as exc:
        raise ValueError("LLM verdict must be match or no_match") from exc
    reasoning = str(value.get("reasoning") or "").strip()
    if not reasoning:
        raise ValueError("LLM reasoning must not be empty")
    return verdict, reasoning


def _message_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def _positive_int(section: str, key: str, override: int | None, default: int) -> int:
    """Resolve one positive-integer setting from a per-call override or matching_config.yaml."""
    value = override if override is not None else config.get(section, key, default=default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{section}.{key} must be an integer >= 1, got {value!r}")
    return value


def _vision_pairs(
    requests: Sequence[MatchRequest], max_input: int, max_scraping: int
) -> list[tuple[list[str], list[str]]]:
    """Deduplicate each side's image URLs in order, then keep at most the leading cap."""
    return [
        (
            list(dict.fromkeys(request.item.image_urls))[:max_input],
            list(dict.fromkeys(request.product.image_urls))[:max_scraping],
        )
        for request in requests
    ]


async def _vision_results(
    requests: Sequence[MatchRequest], *, max_input: int, max_scraping: int
):
    from image_load_compression import compare_batch, load_compare_config

    model = str(config.get("vision", "model", default="qwen3-vl-flash"))
    base_url, api_key = resolve_llm_route(model)
    compare_config = load_compare_config(api_key=api_key, base_url=base_url, model=model)
    # We already truncated per side; raise the package's shared cap so it cannot re-truncate.
    compare_config = dataclasses.replace(
        compare_config, max_images_per_set=max(max_input, max_scraping)
    )
    return await compare_batch(_vision_pairs(requests, max_input, max_scraping), compare_config)


async def verify_products(
    requests: Sequence[MatchRequest | tuple[InputItem, ProductData]],
    *,
    vision_enabled: bool = False,
    max_considered_num_input_image: int | None = None,
    max_considered_num_scraping_image: int | None = None,
    concurrency: int | None = None,
    chat_model: ChatModel | None = None,
    vision_runner=None,
) -> list[ProductMatchResult]:
    max_input = _positive_int(
        "vision", "max_considered_num_input_image", max_considered_num_input_image, 2
    )
    max_scraping = _positive_int(
        "vision", "max_considered_num_scraping_image", max_considered_num_scraping_image, 2
    )
    active_concurrency = _positive_int("llm", "concurrency", concurrency, 8)
    prepared = [
        request if isinstance(request, MatchRequest) else MatchRequest(*request)
        for request in requests
    ]
    results: list[ProductMatchResult | None] = [None] * len(prepared)
    unresolved: list[int] = []
    contexts: dict[int, tuple[EvidenceStatus, EvidenceStatus, dict[str, Any]]] = {}
    traces: dict[int, list[DecisionNodeRecord]] = {}
    technical_errors: dict[int, MatchingError] = {}

    for index, request in enumerate(prepared):
        gtin_status, left_gtin, right_gtin = _gtin_status(request.item, request.product)
        trace = [
            DecisionNodeRecord(
                node=DecisionNode.GTIN,
                status=gtin_status.value,
                terminal=gtin_status == EvidenceStatus.PASS,
                detail={
                    "normalized_input_gtin": left_gtin,
                    "normalized_product_gtin": right_gtin,
                },
            )
        ]
        traces[index] = trace
        if gtin_status == EvidenceStatus.PASS:
            variant_status = EvidenceStatus.UNKNOWN
            evidence = {
                "normalized_input_gtin": left_gtin,
                "normalized_product_gtin": right_gtin,
            }
            results[index] = ProductMatchResult(
                verdict=ProductMatchVerdict.MATCH,
                decision_source=DecisionSource.GTIN,
                reasoning="Both listings contain the same valid GTIN.",
                gtin_status=gtin_status,
                variant_status=variant_status,
                evidence=evidence,
                trace=trace,
            )
            continue
        variant_status, evidence = compare_variants(request.item, request.product)
        trace.append(
            DecisionNodeRecord(
                node=DecisionNode.VARIANT_RULE,
                status=variant_status.value,
                terminal=variant_status == EvidenceStatus.CONFLICT,
                detail=dict(evidence),
            )
        )
        evidence["normalized_input_gtin"] = left_gtin
        evidence["normalized_product_gtin"] = right_gtin
        contexts[index] = (gtin_status, variant_status, evidence)
        if variant_status == EvidenceStatus.CONFLICT:
            results[index] = ProductMatchResult(
                verdict=ProductMatchVerdict.NO_MATCH,
                decision_source=DecisionSource.VARIANT_RULE,
                reasoning="A confirmed brand or normalized variant attribute conflicts.",
                gtin_status=gtin_status,
                variant_status=variant_status,
                evidence=evidence,
                trace=trace,
            )
        else:
            unresolved.append(index)

    vision_detail_fields = (
        "comment",
        "set_a_images_used",
        "set_b_images_used",
        "dropped_urls",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "error_detail",
    )
    vision_by_index: dict[int, tuple[VisionStatus, str | None, dict[str, Any]]] = {}
    eligible = [
        index for index in unresolved
        if vision_enabled and prepared[index].item.image_urls and prepared[index].product.image_urls
    ]
    if eligible:
        runner = vision_runner or partial(
            _vision_results, max_input=max_input, max_scraping=max_scraping
        )
        try:
            raw_results = await runner([prepared[index] for index in eligible])
            for index, raw in zip(eligible, raw_results):
                raw_status = getattr(raw, "status", "failed")
                status_value = str(getattr(raw_status, "value", raw_status))
                if status_value == "success":
                    status = VisionStatus.SUCCESS
                elif status_value == "insufficient_images":
                    status = VisionStatus.NOT_AVAILABLE
                else:
                    status = VisionStatus.FAILED
                detail = {field: getattr(raw, field, None) for field in vision_detail_fields}
                vision_by_index[index] = (status, detail["comment"], detail)
        except Exception as exc:
            for index in eligible:
                comment = f"Vision unavailable: {exc}"
                detail = {field: None for field in vision_detail_fields}
                detail["comment"] = comment
                detail["error_detail"] = str(exc)
                vision_by_index[index] = (VisionStatus.FAILED, comment, detail)

    if unresolved:
        model_name = str(config.get("llm", "model"))
        temperature = float(config.get("llm", "temperature", default=0.1))
        llm = chat_model or make_chat_model(
            model=model_name,
            temperature=temperature,
            timeout_s=float(config.get("llm", "timeout_s", default=60)),
        )
        retries = int(config.get("llm", "max_retries", default=2))
        semaphore = asyncio.Semaphore(active_concurrency)

        async def decide(index: int) -> None:
            """Settle one unresolved item; each call writes only its own slot."""
            request = prepared[index]
            gtin_status, variant_status, evidence = contexts[index]
            if not vision_enabled:
                vision_status, vision_comment = VisionStatus.NOT_REQUESTED, None
            elif index in vision_by_index:
                vision_status, vision_comment, vision_detail = vision_by_index[index]
            else:
                vision_status, vision_comment = VisionStatus.NOT_AVAILABLE, None
                vision_detail = {field: None for field in vision_detail_fields}
            trace = traces[index]
            if vision_enabled:
                trace.append(
                    DecisionNodeRecord(
                        node=DecisionNode.VISION,
                        status=vision_status.value,
                        detail=vision_detail,
                    )
                )
            prompt = _build_prompt(
                request,
                gtin_status=gtin_status,
                variant_status=variant_status,
                evidence=evidence,
                vision_status=vision_status,
                vision_comment=vision_comment,
            )
            last_error: Exception | None = None
            started = time.perf_counter()
            for _attempt in range(retries + 1):
                try:
                    async with semaphore:
                        response = await llm.ainvoke([
                            ("system", _SYSTEM_PROMPT),
                            ("human", prompt),
                        ])
                    verdict, reasoning = _parse_llm(_message_text(response))
                    trace.append(
                        DecisionNodeRecord(
                            node=DecisionNode.LLM,
                            status=verdict.value,
                            terminal=True,
                            detail={
                                "model": model_name,
                                "temperature": temperature,
                                "attempts": _attempt + 1,
                                "latency_ms": round(
                                    (time.perf_counter() - started) * 1000, 3
                                ),
                                "reasoning": reasoning,
                            },
                        )
                    )
                    results[index] = ProductMatchResult(
                        verdict=verdict,
                        decision_source=DecisionSource.LLM,
                        reasoning=reasoning,
                        gtin_status=gtin_status,
                        variant_status=variant_status,
                        vision_status=vision_status,
                        vision_comment=vision_comment,
                        evidence=evidence,
                        trace=trace,
                    )
                    return
                except Exception as exc:
                    last_error = exc
            trace.append(
                DecisionNodeRecord(
                    node=DecisionNode.LLM,
                    status="error",
                    terminal=True,
                    detail={
                        "model": model_name,
                        "attempts": retries + 1,
                        "error": str(last_error),
                    },
                )
            )
            technical_errors[index] = MatchingError(
                f"matching LLM failed after {retries + 1} attempts: {last_error}",
                trace=trace,
            )

        await asyncio.gather(*(decide(index) for index in unresolved))

    if technical_errors:
        raise MatchingBatchError(results, technical_errors)
    return [result for result in results if result is not None]


async def verify_product(
    item: InputItem,
    product: ProductData,
    *,
    vision_enabled: bool = False,
    max_considered_num_input_image: int | None = None,
    max_considered_num_scraping_image: int | None = None,
    chat_model: ChatModel | None = None,
    vision_runner=None,
) -> ProductMatchResult:
    return (
        await verify_products(
            [(item, product)],
            vision_enabled=vision_enabled,
            max_considered_num_input_image=max_considered_num_input_image,
            max_considered_num_scraping_image=max_considered_num_scraping_image,
            chat_model=chat_model,
            vision_runner=vision_runner,
        )
    )[0]

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from src.common.llm_client import make_chat_model, resolve_llm_route
from src.models import (
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


_SYSTEM_PROMPT = """You verify whether a user's intended SKU and one scraped listing are the same exact product variant.
Use only the supplied evidence. A missing value is unknown, not a conflict. A GTIN conflict is strong negative evidence but is not an automatic rejection. Respect differences in brand, model, formulation, colour, per-item size, pack count, total quantity, and included accessories. Visual evidence contains observations, not a verdict. If the evidence is insufficient to confirm the exact SKU, return no_match.
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


async def _vision_results(requests: Sequence[MatchRequest]):
    from image_load_compression import compare_batch, load_compare_config

    model = str(config.get("vision", "model", default="qwen3-vl-flash"))
    base_url, api_key = resolve_llm_route(model)
    compare_config = load_compare_config(api_key=api_key, base_url=base_url, model=model)
    pairs = [(request.item.image_urls, request.product.image_urls) for request in requests]
    return await compare_batch(pairs, compare_config)


async def verify_products(
    requests: Sequence[MatchRequest | tuple[InputItem, ProductData]],
    *,
    vision_enabled: bool = False,
    chat_model: ChatModel | None = None,
    vision_runner=None,
) -> list[ProductMatchResult]:
    prepared = [
        request if isinstance(request, MatchRequest) else MatchRequest(*request)
        for request in requests
    ]
    results: list[ProductMatchResult | None] = [None] * len(prepared)
    unresolved: list[int] = []
    contexts: dict[int, tuple[EvidenceStatus, EvidenceStatus, dict[str, Any]]] = {}
    technical_errors: dict[int, MatchingError] = {}

    for index, request in enumerate(prepared):
        gtin_status, left_gtin, right_gtin = _gtin_status(request.item, request.product)
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
            )
            continue
        variant_status, evidence = compare_variants(request.item, request.product)
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
            )
        else:
            unresolved.append(index)

    vision_by_index: dict[int, tuple[VisionStatus, str | None]] = {}
    eligible = [
        index for index in unresolved
        if vision_enabled and prepared[index].item.image_urls and prepared[index].product.image_urls
    ]
    if eligible:
        runner = vision_runner or _vision_results
        try:
            raw_results = await runner([prepared[index] for index in eligible])
            for index, raw in zip(eligible, raw_results):
                status_value = str(getattr(raw, "status", "failed"))
                if status_value == "success":
                    status = VisionStatus.SUCCESS
                elif status_value == "insufficient_images":
                    status = VisionStatus.NOT_AVAILABLE
                else:
                    status = VisionStatus.FAILED
                vision_by_index[index] = (status, getattr(raw, "comment", None))
        except Exception as exc:
            for index in eligible:
                vision_by_index[index] = (VisionStatus.FAILED, f"Vision unavailable: {exc}")

    if unresolved:
        llm = chat_model or make_chat_model(
            model=str(config.get("llm", "model")),
            temperature=float(config.get("llm", "temperature", default=0.1)),
            timeout_s=float(config.get("llm", "timeout_s", default=60)),
        )
        retries = int(config.get("llm", "max_retries", default=2))
        for index in unresolved:
            request = prepared[index]
            gtin_status, variant_status, evidence = contexts[index]
            if not vision_enabled:
                vision_status, vision_comment = VisionStatus.NOT_REQUESTED, None
            elif index in vision_by_index:
                vision_status, vision_comment = vision_by_index[index]
            else:
                vision_status, vision_comment = VisionStatus.NOT_AVAILABLE, None
            prompt = _build_prompt(
                request,
                gtin_status=gtin_status,
                variant_status=variant_status,
                evidence=evidence,
                vision_status=vision_status,
                vision_comment=vision_comment,
            )
            last_error: Exception | None = None
            for _attempt in range(retries + 1):
                try:
                    response = await llm.ainvoke([
                        ("system", _SYSTEM_PROMPT),
                        ("human", prompt),
                    ])
                    verdict, reasoning = _parse_llm(_message_text(response))
                    results[index] = ProductMatchResult(
                        verdict=verdict,
                        decision_source=DecisionSource.LLM,
                        reasoning=reasoning,
                        gtin_status=gtin_status,
                        variant_status=variant_status,
                        vision_status=vision_status,
                        vision_comment=vision_comment,
                        evidence=evidence,
                    )
                    break
                except Exception as exc:
                    last_error = exc
            if results[index] is None:
                technical_errors[index] = MatchingError(
                    f"matching LLM failed after {retries + 1} attempts: {last_error}"
                )

    if technical_errors:
        raise MatchingBatchError(results, technical_errors)
    return [result for result in results if result is not None]


async def verify_product(
    item: InputItem,
    product: ProductData,
    *,
    vision_enabled: bool = False,
    chat_model: ChatModel | None = None,
    vision_runner=None,
) -> ProductMatchResult:
    return (
        await verify_products(
            [(item, product)],
            vision_enabled=vision_enabled,
            chat_model=chat_model,
            vision_runner=vision_runner,
        )
    )[0]

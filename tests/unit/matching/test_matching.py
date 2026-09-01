from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.matching.attributes import compare_multipacks, compare_variants, extract_multipack
from src.matching.service import MatchingBatchError, verify_product
from src.models import (
    DecisionSource,
    EvidenceStatus,
    InputItem,
    ProductMatchVerdict,
    VisionStatus,
)
from tests._support.factories import product_data


class FakeChat:
    def __init__(self, text: str = '{"verdict":"match","reasoning":"titles agree"}'):
        self.text = text
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.text)


async def test_same_valid_gtin_short_circuits_llm():
    chat = FakeChat()
    result = await verify_product(
        InputItem(title="Anything", country="uk", site_name="tesco", gtin="4006381333931"),
        product_data(gtin="4006381333931", title="Different title"),
        chat_model=chat,
    )
    assert result.verdict == ProductMatchVerdict.MATCH
    assert result.decision_source == DecisionSource.GTIN
    assert chat.calls == []


async def test_missing_gtin_is_unknown_and_different_gtin_reaches_one_llm_prompt():
    chat = FakeChat()
    result = await verify_product(
        InputItem(title="Neutral widget", country="uk", site_name="tesco", gtin="4006381333931"),
        product_data(gtin="5901234123457", title="Neutral widget"),
        chat_model=chat,
    )
    assert result.gtin_status == EvidenceStatus.CONFLICT
    assert result.decision_source == DecisionSource.LLM
    assert len(chat.calls) == 1
    assert '"gtin_status": "conflict"' in chat.calls[0][1][1]


def test_numeric_tolerance_and_hard_conflict():
    near, _ = compare_variants(
        InputItem(title="Product 100ml", country="uk", site_name="tesco"),
        product_data(title="Product 109ml"),
    )
    far, _ = compare_variants(
        InputItem(title="Product 100ml", country="uk", site_name="tesco"),
        product_data(title="Product 120ml"),
    )
    assert near == EvidenceStatus.PASS
    assert far == EvidenceStatus.CONFLICT


def test_multipack_orders_and_each_total_are_equivalent():
    first = extract_multipack("15ml * 20")
    second = extract_multipack("20 x 15 ml")
    third = extract_multipack("15ml each, 20, 300 ml in total")
    fourth = extract_multipack("pack of 20, 15ml each, 300ml total")
    fifth = extract_multipack("15ml each, 20-pack")
    assert first["volume"].total == 300
    assert compare_multipacks(first, second)[0] == EvidenceStatus.PASS
    assert compare_multipacks(first, third)[0] == EvidenceStatus.PASS
    assert compare_multipacks(first, fourth)[0] == EvidenceStatus.PASS
    assert compare_multipacks(first, fifth)[0] == EvidenceStatus.PASS


def test_multipack_unit_conversion_uses_same_semantic_slots():
    millilitres = extract_multipack("20 x 15ml")
    litres = extract_multipack("20 x 0.015l, 0.3l total")
    assert compare_multipacks(millilitres, litres)[0] == EvidenceStatus.PASS


def test_internally_inconsistent_multipack_is_not_hard_conflict():
    bad = extract_multipack("15ml x 20, 200ml total")
    good = extract_multipack("15ml x 20, 300ml total")
    assert bad["volume"].inconsistent is True
    assert compare_multipacks(bad, good)[0] == EvidenceStatus.UNKNOWN


async def test_vision_evidence_uses_same_llm_prompt_and_failure_degrades():
    chat = FakeChat()

    async def vision_runner(_requests):
        return [SimpleNamespace(status="success", comment="same red package")]

    result = await verify_product(
        InputItem(
            title="Neutral widget", country="uk", site_name="tesco",
            image_urls=["https://img.test/a.jpg"],
        ),
        product_data(title="Neutral widget", image_urls=["https://img.test/b.jpg"]),
        vision_enabled=True,
        chat_model=chat,
        vision_runner=vision_runner,
    )
    assert result.vision_status == VisionStatus.SUCCESS
    assert "same red package" in chat.calls[0][1][1]

    async def failed_vision(_requests):
        raise TimeoutError("vision timeout")

    degraded_chat = FakeChat()
    degraded = await verify_product(
        InputItem(
            title="Neutral widget", country="uk", site_name="tesco",
            image_urls=["https://img.test/a.jpg"],
        ),
        product_data(title="Neutral widget", image_urls=["https://img.test/b.jpg"]),
        vision_enabled=True,
        chat_model=degraded_chat,
        vision_runner=failed_vision,
    )
    assert degraded.vision_status == VisionStatus.FAILED
    assert "vision timeout" in degraded_chat.calls[0][1][1]
    assert degraded.decision_source == DecisionSource.LLM


async def test_llm_failure_retries_then_surfaces_technical_error():
    chat = FakeChat("not json")
    with pytest.raises(MatchingBatchError) as exc:
        await verify_product(
            InputItem(title="Neutral widget", country="uk", site_name="tesco"),
            product_data(title="Neutral widget"),
            chat_model=chat,
        )
    assert len(chat.calls) == 3
    assert 0 in exc.value.errors

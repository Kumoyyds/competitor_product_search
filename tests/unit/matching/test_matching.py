from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.matching.attributes import compare_multipacks, compare_variants, extract_multipack
from src.matching.service import (
    MatchRequest,
    MatchingBatchError,
    _positive_int,
    _vision_pairs,
    verify_product,
    verify_products,
)
from src.models import (
    DecisionNode,
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
    assert [node.node for node in result.trace] == [DecisionNode.GTIN]
    assert result.trace[0].terminal is True
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
    assert [node.node for node in result.trace] == [
        DecisionNode.GTIN,
        DecisionNode.VARIANT_RULE,
        DecisionNode.LLM,
    ]


async def test_variant_conflict_trace_stops_before_vision_and_llm():
    chat = FakeChat()
    result = await verify_product(
        InputItem(title="Product 100ml", country="uk", site_name="tesco"),
        product_data(title="Product 120ml"),
        vision_enabled=True,
        chat_model=chat,
    )
    assert result.verdict == ProductMatchVerdict.NO_MATCH
    assert [node.node for node in result.trace] == [
        DecisionNode.GTIN,
        DecisionNode.VARIANT_RULE,
    ]
    assert result.trace[-1].terminal is True
    assert chat.calls == []


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


def test_vision_image_caps_apply_per_side_after_dedupe():
    one, two, three = (f"https://img.test/{n}.jpg" for n in (1, 2, 3))
    request = MatchRequest(
        InputItem(
            title="Neutral widget", country="uk", site_name="tesco",
            image_urls=[one, two, three],
        ),
        product_data(image_urls=[one, two]),
    )
    # input cap truncates to the leading 2; scraping cap of 3 keeps all 2 available URLs.
    assert _vision_pairs([request], 2, 3) == [([one, two], [one, two])]

    duplicated = MatchRequest(
        InputItem(
            title="Neutral widget", country="uk", site_name="tesco",
            image_urls=[one, one, two],
        ),
        product_data(image_urls=[three]),
    )
    assert _vision_pairs([duplicated], 2, 2) == [([one, two], [three])]


@pytest.mark.parametrize("bad", [0, -1, True, 2.5, "2"])
@pytest.mark.parametrize(
    ("section", "key"),
    [("vision", "max_considered_num_input_image"), ("llm", "concurrency")],
)
def test_positive_int_rejects_non_positive_integers(section, key, bad):
    with pytest.raises(ValueError):
        _positive_int(section, key, bad, 2)


async def test_vision_evidence_uses_same_llm_prompt_and_failure_degrades():
    chat = FakeChat()

    async def vision_runner(_requests):
        return [SimpleNamespace(
            status="success",
            comment="same red package",
            set_a_images_used=1,
            set_b_images_used=1,
            dropped_urls=[],
            model="vision-test",
            prompt_tokens=101,
            completion_tokens=12,
            error_detail=None,
        )]

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
    assert [node.node for node in result.trace] == [
        DecisionNode.GTIN,
        DecisionNode.VARIANT_RULE,
        DecisionNode.VISION,
        DecisionNode.LLM,
    ]
    assert result.trace[2].detail["model"] == "vision-test"
    assert result.trace[2].detail["prompt_tokens"] == 101
    assert result.trace[-1].detail["attempts"] == 1
    assert result.trace[-1].detail["latency_ms"] >= 0

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
    trace = exc.value.errors[0].trace
    assert [node.node for node in trace] == [
        DecisionNode.GTIN,
        DecisionNode.VARIANT_RULE,
        DecisionNode.LLM,
    ]
    assert trace[-1].status == "error"
    assert trace[-1].terminal is True


async def test_batch_decisions_run_concurrently_within_the_configured_bound():
    """Unresolved items settle in parallel, but never more than `concurrency` at once."""

    class TrackingChat:
        def __init__(self) -> None:
            self.in_flight = 0
            self.peak = 0
            self.calls: list = []

        async def ainvoke(self, messages):
            self.calls.append(messages)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await asyncio.sleep(0.01)  # Hold the slot so siblings can pile up.
            self.in_flight -= 1
            return SimpleNamespace(content='{"verdict":"match","reasoning":"ok"}')

    chat = TrackingChat()
    requests = [
        MatchRequest(
            InputItem(title=f"Neutral widget {n}", country="uk", site_name="tesco"),
            product_data(title=f"Neutral widget {n}"),
        )
        for n in range(4)
    ]
    results = await verify_products(requests, concurrency=2, chat_model=chat)

    assert len(results) == 4
    assert len(chat.calls) == 4
    assert chat.peak == 2


async def test_batch_results_keep_input_order_under_concurrency():
    """Out-of-order completions must not reorder the returned verdicts."""
    order = [0.03, 0.0, 0.02, 0.01]

    class StaggeredChat:
        def __init__(self) -> None:
            self.seen = 0

        async def ainvoke(self, messages):
            delay = order[self.seen]
            self.seen += 1
            await asyncio.sleep(delay)
            verdict = "match" if delay > 0.015 else "no_match"
            return SimpleNamespace(
                content=f'{{"verdict":"{verdict}","reasoning":"slept {delay}"}}'
            )

    requests = [
        MatchRequest(
            InputItem(title=f"Neutral widget {n}", country="uk", site_name="tesco"),
            product_data(title=f"Neutral widget {n}"),
        )
        for n in range(4)
    ]
    results = await verify_products(requests, concurrency=4, chat_model=StaggeredChat())

    assert [result.verdict for result in results] == [
        ProductMatchVerdict.MATCH,
        ProductMatchVerdict.NO_MATCH,
        ProductMatchVerdict.MATCH,
        ProductMatchVerdict.NO_MATCH,
    ]

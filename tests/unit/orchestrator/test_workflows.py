from __future__ import annotations

import sqlite3
import json

import pandas as pd

import pytest

from src.matching import MatchingBatchError, MatchingError
from src.models import (
    DecisionSource,
    EvidenceStatus,
    InputItem,
    ProductMatchResult,
    ProductMatchVerdict,
)
from src.orchestrator.database import OrchestratorDB
from src.orchestrator.input import load_input
from src.orchestrator.workflow import rerun, run_new_input
from src.search.batch import SearchItemResult, SearchManyResult
from src.search.models import FinalVerdict, LayerTrace, MatchResult, RawCandidate
from tests._support.factories import product_data


def matched(title="Found title", url="https://example.test/product/1"):
    return MatchResult(
        verdict=FinalVerdict.MATCH,
        matched_candidate=RawCandidate(title=title, url=url),
        layer_trace=LayerTrace(),
        candidates_considered=1,
        reason="found",
    )


def verified(verdict=ProductMatchVerdict.MATCH, reason="same"):
    return ProductMatchResult(
        verdict=verdict,
        decision_source=DecisionSource.LLM,
        reasoning=reason,
        gtin_status=EvidenceStatus.UNKNOWN,
        variant_status=EvidenceStatus.UNKNOWN,
    )


async def test_new_input_records_row_validation_and_success(tmp_path, monkeypatch):
    db_path = tmp_path / "orchestrator.db"

    async def fake_search(requests, **_kwargs):
        return SearchManyResult(
            items=[SearchItemResult(request=requests[0], result=matched())],
            run_id="search-run",
            provider_calls={},
        )

    async def fake_scrape(_url):
        return product_data(title="Found title")

    async def fake_verify(requests, **_kwargs):
        return [verified() for _ in requests]

    monkeypatch.setattr("src.orchestrator.workflow.match_products", fake_search)
    monkeypatch.setattr("src.orchestrator.workflow.scrape", fake_scrape)
    monkeypatch.setattr("src.orchestrator.workflow.verify_products", fake_verify)
    result = await run_new_input(
        [
            InputItem(title="Good", country="uk", site_name="tesco"),
        ],
        db_path=db_path,
    )
    assert (result.valid, result.failed, result.status) == (1, 0, "completed")

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT search_title,matched_url FROM item_outcomes").fetchall() == [
            ("Found title", "https://example.test/product/1")
        ]
        assert conn.execute(
            "SELECT attempt_no,execution_path,verdict,decision_source "
            "FROM matching_decisions"
        ).fetchall() == [(1, "new_input", "match", "llm")]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


async def test_file_row_validation_failure_does_not_block_valid_sibling(tmp_path, monkeypatch):
    db_path = tmp_path / "orchestrator.db"
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps([
            {"title": "Good", "country": "uk", "site_name": "tesco"},
            {"title": "", "country": "uk", "site_name": "tesco"},
        ]),
        encoding="utf-8",
    )

    async def fake_search(requests, **_kwargs):
        assert len(requests) == 1
        return SearchManyResult(
            items=[SearchItemResult(request=requests[0], result=matched())],
            run_id="search-run", provider_calls={},
        )

    async def fake_scrape(_url):
        return product_data(title="Found title")

    async def fake_verify(requests, **_kwargs):
        return [verified() for _ in requests]

    monkeypatch.setattr("src.orchestrator.workflow.match_products", fake_search)
    monkeypatch.setattr("src.orchestrator.workflow.scrape", fake_scrape)
    monkeypatch.setattr("src.orchestrator.workflow.verify_products", fake_verify)
    result = await run_new_input(path, db_path=db_path)
    assert (result.valid, result.failed, result.status) == (1, 1, "completed_with_failures")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT fail_node FROM failure_results").fetchall() == [("input",)]
    finally:
        conn.close()


async def test_structural_json_error_is_rejected_before_batch_creation(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    path = tmp_path / "items.json"
    path.write_text('{"title":"not an array"}', encoding="utf-8")
    with pytest.raises(ValueError, match="array of objects"):
        await run_new_input(path, db_path=db_path)
    assert not db_path.exists()


async def test_search_no_match_stores_null_search_title(tmp_path, monkeypatch):
    db_path = tmp_path / "orchestrator.db"

    async def fake_search(requests, **_kwargs):
        result = MatchResult(
            verdict=FinalVerdict.NO_MATCH,
            matched_candidate=None,
            layer_trace=LayerTrace(),
            candidates_considered=3,
            reason="candidates rejected",
        )
        return SearchManyResult(
            items=[SearchItemResult(request=requests[0], result=result)],
            run_id="search-run", provider_calls={},
        )

    monkeypatch.setattr("src.orchestrator.workflow.match_products", fake_search)
    result = await run_new_input(
        [InputItem(title="Missing", country="uk", site_name="tesco")],
        db_path=db_path,
    )
    assert result.failed == 1
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT fail_node,search_title FROM item_outcomes"
        ).fetchall() == [("search", None)]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("technical", "expected_verdict", "expected_kind"),
    [
        (False, "no_match", "no_match"),
        (True, "error", "technical_error"),
    ],
)
async def test_new_input_records_matching_no_match_and_technical_error(
    tmp_path, monkeypatch, technical, expected_verdict, expected_kind
):
    db_path = tmp_path / f"orchestrator-{expected_verdict}.db"

    async def fake_search(requests, **_kwargs):
        return SearchManyResult(
            items=[SearchItemResult(request=requests[0], result=matched())],
            run_id="search-run",
            provider_calls={},
        )

    async def fake_scrape(_url):
        return product_data(title="Found title")

    async def fake_verify(_requests, **_kwargs):
        if technical:
            raise MatchingBatchError([None], {0: MatchingError("model timeout")})
        return [verified(ProductMatchVerdict.NO_MATCH, "different product")]

    monkeypatch.setattr("src.orchestrator.workflow.match_products", fake_search)
    monkeypatch.setattr("src.orchestrator.workflow.scrape", fake_scrape)
    monkeypatch.setattr("src.orchestrator.workflow.verify_products", fake_verify)
    result = await run_new_input(
        [InputItem(title="Input", country="uk", site_name="tesco")],
        db_path=db_path,
    )
    assert result.failed == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT verdict,decision_source FROM matching_decisions"
        ).fetchall() == [
            (expected_verdict, "technical_error" if technical else "llm")
        ]
        assert conn.execute(
            "SELECT failure_kind FROM failure_results"
        ).fetchall() == [(expected_kind,)]
    finally:
        conn.close()


async def test_rerun_creates_derived_batch_and_unchanged_identity_skips_search_match(tmp_path, monkeypatch):
    db_path = tmp_path / "orchestrator.db"
    db = OrchestratorDB(db_path)
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    item = InputItem(title="Input", country="uk", site_name="tesco")
    item_id = db.add_item(root, row_index=0, raw=item.model_dump(), item=item)
    old = product_data(title="Stable title")
    db.record_valid(
        item_id, old, search_title="Search title",
        execution_path="new_input",
    )
    db.finish_batch(root)
    db.close()

    calls = {"search": 0, "match": 0}

    async def fake_scrape(_url):
        return product_data(title="Stable title")

    async def no_search(*_args, **_kwargs):
        calls["search"] += 1
        raise AssertionError("search must not run")

    async def no_match(*_args, **_kwargs):
        calls["match"] += 1
        raise AssertionError("matching must not run")

    monkeypatch.setattr("src.orchestrator.workflow.scrape", fake_scrape)
    monkeypatch.setattr("src.orchestrator.workflow.match_products", no_search)
    monkeypatch.setattr("src.orchestrator.workflow.verify_products", no_match)
    result = await rerun(root, db_path=db_path)
    assert result.batch_id == f"{root}-r1"
    assert result.valid == 1
    assert calls == {"search": 0, "match": 0}
    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT d.execution_path,verdict,decision_source,decision_process "
            "FROM matching_decisions AS d JOIN batch_items AS i USING(item_id) "
            "WHERE i.batch_id=?",
            (result.batch_id,),
        ).fetchall()[0][:3] == ("stored_url", "match", "identity_guard")
        process = json.loads(check.execute(
            "SELECT decision_process FROM matching_decisions AS d "
            "JOIN batch_items AS i USING(item_id) WHERE i.batch_id=?",
            (result.batch_id,),
        ).fetchone()[0])
        assert process["terminated_at"] == "identity_guard"
    finally:
        check.close()


async def test_rerun_missing_title_rejects_before_child_creation(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    db = OrchestratorDB(db_path)
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    item = InputItem(title="Input", country="uk", site_name="tesco")
    item_id = db.add_item(root, row_index=0, raw=item.model_dump(), item=item)
    old = product_data(title="Stable title")
    db.record_valid(
        item_id, old, search_title="Known title",
        execution_path="new_input",
    )
    db.finish_batch(root)
    db.close()

    with pytest.raises(ValueError, match="search_title not found"):
        await rerun(root, search_titles=["missing"], db_path=db_path)
    check = OrchestratorDB(db_path)
    try:
        assert check.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    finally:
        check.close()


@pytest.mark.parametrize("suffix", [".xlsx", ".csv", ".json"])
def test_input_formats_parse_image_array_single_url_and_region(tmp_path, suffix):
    rows = [
        {
            "title": "One",
            "region": "UK",
            "site_name": "Tesco",
            "gtin": "04006381333931",
            "image_urls": '["https://img.test/1.jpg", "https://img.test/2.jpg"]',
        },
        {
            "title": "Two",
            "region": "UK",
            "site_name": "Tesco",
            "image_urls": "https://img.test/3.jpg",
        },
    ]
    path = tmp_path / f"input{suffix}"
    if suffix == ".xlsx":
        pd.DataFrame(rows).to_excel(path, index=False)
    elif suffix == ".csv":
        pd.DataFrame(rows).to_csv(path, index=False)
    else:
        path.write_text(json.dumps(rows), encoding="utf-8")
    parsed, source = load_input(path)
    assert source == str(path)
    assert parsed[0].item.country == "uk"
    assert parsed[0].item.gtin == "04006381333931"
    assert parsed[0].item.image_urls == [
        "https://img.test/1.jpg",
        "https://img.test/2.jpg",
    ]
    assert parsed[1].item.image_urls == ["https://img.test/3.jpg"]


async def test_rerun_identity_no_match_falls_back_once_without_failure_row(tmp_path, monkeypatch):
    db_path = tmp_path / "orchestrator.db"
    db = OrchestratorDB(db_path)
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    item = InputItem(title="Input product", country="uk", site_name="tesco")
    item_id = db.add_item(root, row_index=0, raw=item.model_dump(), item=item)
    old = product_data(title="Old identity", url="https://example.test/old")
    db.record_valid(
        item_id, old, search_title="Old search title",
        execution_path="new_input",
    )
    db.finish_batch(root)
    db.close()

    scrape_urls = []

    async def fake_scrape(url):
        scrape_urls.append(url)
        if url.endswith("/old"):
            return product_data(title="Changed identity", url=url)
        return product_data(title="New identity", url=url)

    async def fake_search(requests, **_kwargs):
        return SearchManyResult(
            items=[
                SearchItemResult(
                    request=requests[0],
                    result=matched("New search title", "https://example.test/new"),
                )
            ],
            run_id="fallback-search",
            provider_calls={},
        )

    match_calls = 0

    async def fake_verify(requests, **_kwargs):
        nonlocal match_calls
        match_calls += 1
        verdict = ProductMatchVerdict.NO_MATCH if match_calls == 1 else ProductMatchVerdict.MATCH
        return [verified(verdict, "changed" if match_calls == 1 else "new URL matches")]

    monkeypatch.setattr("src.orchestrator.workflow.scrape", fake_scrape)
    monkeypatch.setattr("src.orchestrator.workflow.match_products", fake_search)
    monkeypatch.setattr("src.orchestrator.workflow.verify_products", fake_verify)
    result = await rerun(root, db_path=db_path)
    assert result.valid == 1 and result.failed == 0
    assert scrape_urls == ["https://example.test/old", "https://example.test/new"]
    assert match_calls == 2

    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT execution_path,search_title,matched_url FROM item_outcomes WHERE batch_id=?",
            (result.batch_id,),
        ).fetchall() == [("fallback", "New search title", "https://example.test/new")]
        assert check.execute(
            "SELECT COUNT(*) FROM item_outcomes WHERE batch_id=? AND failure_id IS NOT NULL",
            (result.batch_id,),
        ).fetchone()[0] == 0
        assert check.execute(
            "SELECT attempt_no,d.execution_path,verdict FROM matching_decisions AS d "
            "JOIN batch_items USING(item_id) WHERE batch_id=? ORDER BY attempt_no",
            (result.batch_id,),
        ).fetchall() == [
            (1, "identity_revalidation", "no_match"),
            (2, "fallback", "match"),
        ]
    finally:
        check.close()


async def test_rerun_stored_scrape_failure_fallback_failure_records_real_node_once(tmp_path, monkeypatch):
    db_path = tmp_path / "orchestrator.db"
    db = OrchestratorDB(db_path)
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    item = InputItem(title="Input product", country="uk", site_name="tesco")
    item_id = db.add_item(root, row_index=0, raw=item.model_dump(), item=item)
    old = product_data(title="Old identity", url="https://example.test/old")
    db.record_valid(
        item_id, old, search_title="Old search title",
        execution_path="new_input",
    )
    db.finish_batch(root)
    db.close()

    calls = {"scrape": 0, "search": 0}

    async def failed_scrape(_url):
        calls["scrape"] += 1
        raise TimeoutError("stored URL timed out")

    async def no_match_search(requests, **_kwargs):
        calls["search"] += 1
        result = MatchResult(
            verdict=FinalVerdict.NO_MATCH,
            matched_candidate=None,
            layer_trace=LayerTrace(),
            candidates_considered=2,
            reason="fallback search found no product",
        )
        return SearchManyResult(
            items=[SearchItemResult(request=requests[0], result=result)],
            run_id="fallback-search",
            provider_calls={},
        )

    monkeypatch.setattr("src.orchestrator.workflow.scrape", failed_scrape)
    monkeypatch.setattr("src.orchestrator.workflow.match_products", no_match_search)
    result = await rerun(root, db_path=db_path)
    assert (result.valid, result.failed) == (0, 1)
    assert calls == {"scrape": 1, "search": 1}

    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT operation,fail_node,search_title FROM item_outcomes "
            "JOIN batches USING(batch_id) WHERE batch_id=?",
            (result.batch_id,),
        ).fetchall() == [("rerun", "search", None)]
        trace = json.loads(check.execute(
            "SELECT stage_trace FROM batch_items WHERE batch_id=?", (result.batch_id,)
        ).fetchone()[0])
        assert trace[0]["stage"] == "stored_url_scraping"
    finally:
        check.close()

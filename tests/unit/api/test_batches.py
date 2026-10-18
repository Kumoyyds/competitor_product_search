from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.settings import ApiSettings
from src.models import DecisionSource, EvidenceStatus, InputItem, ProductMatchResult, ProductMatchVerdict
from src.orchestrator.database import OrchestratorDB
from src.search.batch import SearchItemResult, SearchManyResult
from src.search.models import FinalVerdict, LayerTrace, MatchResult, RawCandidate
from tests._support.factories import product_data


def _settings(tmp_path, *, max_concurrent_batches: int = 1) -> ApiSettings:
    return ApiSettings(
        db_path=tmp_path / "orchestrator.db",
        upload_dir=tmp_path / "uploads",
        max_concurrent_batches=max_concurrent_batches,
        default_concurrency=2,
    )


def _items(*titles: str) -> dict:
    return {
        "items": [
            {"title": title, "country": "uk", "site_name": "tesco"}
            for title in titles
        ]
    }


def _wait_for(client: TestClient, batch_id: str, status: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/batches/{batch_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] == status:
            return result
        time.sleep(0.02)
    pytest.fail(f"batch {batch_id} did not become {status}; last status was {result['status']}")


def _mock_success(monkeypatch):
    async def fake_search(requests, **_kwargs):
        return SearchManyResult(
            items=[
                SearchItemResult(
                    request=request,
                    result=MatchResult(
                        verdict=FinalVerdict.MATCH,
                        matched_candidate=RawCandidate(
                            title=f"Found {request.product_name}",
                            url=f"https://example.test/product/{index}",
                        ),
                        layer_trace=LayerTrace(),
                        candidates_considered=1,
                        reason="found",
                    ),
                )
                for index, request in enumerate(requests)
            ],
            run_id="search-run",
            provider_calls={},
        )

    async def fake_scrape(url):
        return product_data(url=url)

    async def fake_verify(requests, **_kwargs):
        return [
            ProductMatchResult(
                verdict=ProductMatchVerdict.MATCH,
                decision_source=DecisionSource.LLM,
                reasoning="same product",
                gtin_status=EvidenceStatus.UNKNOWN,
                variant_status=EvidenceStatus.UNKNOWN,
            )
            for _ in requests
        ]

    monkeypatch.setattr("src.orchestrator.workflow.match_products", fake_search)
    monkeypatch.setattr("src.orchestrator.workflow.scrape", fake_scrape)
    monkeypatch.setattr("src.orchestrator.workflow.verify_products", fake_verify)


@pytest.mark.parametrize("source", ["csv", "items"])
def test_submit_query_and_paginate_completed_batch(tmp_path, monkeypatch, source):
    _mock_success(monkeypatch)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        if source == "csv":
            accepted = client.post(
                "/batches/new-input",
                files={"file": ("products.csv", b"title,country,site_name\nOne,uk,tesco\nTwo,uk,tesco\n", "text/csv")},
            )
        else:
            accepted = client.post("/batches/new-input/items", json=_items("One", "Two"))
        assert accepted.status_code == 202, accepted.text
        batch_id = accepted.json()["batch_id"]
        assert accepted.json()["status_url"] == f"/batches/{batch_id}"

        batch = _wait_for(client, batch_id, "completed")
        assert (batch["total_items"], batch["finished_count"], batch["valid_count"]) == (2, 2, 2)
        assert (batch["pending_count"], batch["running_count"], batch["phase"]) == (0, 0, None)

        first = client.get(f"/batches/{batch_id}/items", params={"limit": 1, "offset": 0})
        second = client.get(f"/batches/{batch_id}/items", params={"limit": 1, "offset": 1})
        assert first.status_code == second.status_code == 200
        assert first.json()["total"] == second.json()["total"] == 2
        assert [first.json()["items"][0]["input_title"], second.json()["items"][0]["input_title"]] == ["One", "Two"]
        assert isinstance(first.json()["items"][0]["product_data"], dict)
        assert isinstance(first.json()["items"][0]["stage_trace"], list)
        assert client.get(f"/batches/{batch_id}/items", params={"status": "failed"}).json()["total"] == 0

        listing = client.get("/batches", params={"operation": "new_input", "status": "completed", "limit": 1})
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["batch_id"] == batch_id
        assert client.get("/batches/missing").status_code == 404
        assert client.get("/batches/missing/items").status_code == 404


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("products.json", b'{"title":"not an array"}'),
        ("products.csv", b"country,site_name\nuk,tesco\n"),
    ],
)
def test_structural_file_error_returns_400_without_batch(tmp_path, filename, contents):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.post("/batches/new-input", files={"file": (filename, contents)})
        assert response.status_code == 400, response.text
        assert client.get("/batches").json()["total"] == 0


def test_json_item_validation_rejects_entire_request(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/batches/new-input/items",
            json={"items": [_items("Good")["items"][0], {"title": "Bad", "country": "uk"}]},
        )
        assert response.status_code == 422
        assert client.get("/batches").json()["total"] == 0


def test_rerun_missing_batch_is_404_and_running_parent_is_409(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.post("/batches/missing/rerun", json={}).status_code == 404
        db = OrchestratorDB(settings.db_path)
        try:
            parent = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
            db.add_item(
                parent,
                row_index=0,
                raw=_items("Input")["items"][0],
                item=InputItem(title="Input", country="uk", site_name="tesco"),
            )
        finally:
            db.close()
        response = client.post(f"/batches/{parent}/rerun", json={})
        assert response.status_code == 409, response.text
        assert client.get("/batches").json()["total"] == 1


def test_rerun_keeps_lineage_and_inherits_vision_setting(tmp_path, monkeypatch):
    _mock_success(monkeypatch)
    with TestClient(create_app(_settings(tmp_path))) as client:
        original = client.post(
            "/batches/new-input/items",
            json={**_items("Input"), "vision_enabled": True},
        )
        assert original.status_code == 202, original.text
        parent_id = original.json()["batch_id"]
        _wait_for(client, parent_id, "completed")

        rerun = client.post(f"/batches/{parent_id}/rerun", json={})
        assert rerun.status_code == 202, rerun.text
        child_id = rerun.json()["batch_id"]
        child = _wait_for(client, child_id, "completed")
        assert child_id == f"{parent_id}-r1"
        assert (child["root_batch_id"], child["parent_batch_id"], child["rerun_no"]) == (parent_id, parent_id, 1)
        assert child["vision_enabled"] == 1
        assert (child["total_items"], child["valid_count"]) == (1, 1)
        assert client.get("/batches", params={"root_batch_id": parent_id}).json()["total"] == 2


def test_startup_marks_orphaned_running_batch_interrupted(tmp_path):
    settings = _settings(tmp_path)
    db = OrchestratorDB(settings.db_path)
    try:
        batch_id = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    finally:
        db.close()
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/batches/{batch_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "interrupted"
        assert response.json()["finished_at"] is not None


def test_single_batch_limit_queues_second_and_shutdown_interrupts_both(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    started = threading.Event()
    async def blocked_search(_requests, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("src.orchestrator.workflow.match_products", blocked_search)
    with TestClient(create_app(settings)) as client:
        first = client.post("/batches/new-input/items", json=_items("First"))
        assert first.status_code == 202, first.text
        first_id = first.json()["batch_id"]
        assert started.wait(timeout=5)
        second = client.post("/batches/new-input/items", json=_items("Second"))
        assert second.status_code == 202, second.text
        second_id = second.json()["batch_id"]
        assert client.get(f"/batches/{first_id}").json()["phase"] == "running"
        queued = client.get(f"/batches/{second_id}").json()
        assert (queued["status"], queued["phase"], queued["pending_count"]) == ("running", "queued", 1)

    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT batch_id, status FROM batches WHERE batch_id IN (?, ?)",
            (first_id, second_id),
        ).fetchall()
        assert dict(rows) == {first_id: "interrupted", second_id: "interrupted"}
    finally:
        conn.close()

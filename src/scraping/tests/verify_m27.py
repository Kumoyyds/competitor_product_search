"""Offline verification for M27 — shape-tolerant Bright Data polling.

Run from the repository root:
    python -m src.scraping.tests.verify_m27 | tee src/scraping/tests/verify_m27_output.log
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from unittest.mock import MagicMock, patch
from tests._support.http import FakeAsyncClient, FakeClock

from src.scraping.config import ScrapingConfig, set_config
from src.scraping.exceptions import BrightDataInfraError


from ._harness import FAILED, PASSED, SKIPPED, check, section, skip, run_main

TEST_CONFIG = {
    "bd_async_poll_max_seconds": 2,
    "bd_async_poll_interval_seconds": 1,
    "bright_data_key": "test-key",
    "db_path": "verify_m27.db",
}










_UNSET = object()


def response(
    status: int,
    *,
    body: str = "",
    content_type: str = "application/json",
    json_data: object = _UNSET,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    resp.headers = {"content-type": content_type}
    if json_data is not _UNSET:
        resp.json.return_value = json_data
    else:
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            resp.json.side_effect = exc
        else:
            resp.json.return_value = parsed
    return resp


def polling_patches(clock: FakeClock) -> tuple[patch, patch, patch]:
    return (
        patch("httpx.AsyncClient", new=FakeAsyncClient),
        patch("src.scraping.extraction.bright_data.time.monotonic", new=clock.monotonic),
        patch("src.scraping.extraction.bright_data.asyncio.sleep", new=clock.sleep),
    )


async def run_poll(client: object, job_id: str, responses: list[MagicMock]) -> tuple[dict, FakeClock]:
    clock = FakeClock()
    FakeAsyncClient.reset(responses)
    client_patch, time_patch, sleep_patch = polling_patches(clock)
    with client_patch, time_patch, sleep_patch:
        result = await client._poll(job_id)  # type: ignore[attr-defined]
    return result, clock


async def verify_single_line_dca_jsonl() -> None:
    section("M27.1 - Single-line Tesco DCA JSONL is terminal")
    from src.scraping.extraction.bright_data import BrightDataDCA

    record = {
        "product_name": "Tesco test",
        "current_price": 3.75,
        "in_stock": True,
    }
    body = json.dumps(record) + "\n"
    result, clock = await run_poll(
        BrightDataDCA("collector"),
        "tesco-job",
        [response(200, body=body, content_type="application/jsonl")],
    )
    check("Tesco JSONL record returned on first poll", result == record, result)
    check("No poll interval elapsed", clock.now == 0 and clock.sleep_calls == 0, clock.now)


async def verify_argos_mapping() -> None:
    section("M27.2 - Argos JSONL maps through both gates")
    from src.scraping.extraction.bright_data import BrightDataDCA
    from src.scraping.scrapers.sites.argos_dca import ArgosDCAScraper
    from src.scraping.validation import validate

    url = "https://www.argos.co.uk/product/4667243"
    record = {
        "product_title": "Argos test",
        "price": {"value": 140, "currency": "GBP"},
        "list_price": {"value": 176, "currency": "GBP"},
        "image_urls": ["https://example.test/image.jpg"],
        "in_stock": True,
        "input": {"url": url},
    }
    result, _ = await run_poll(
        BrightDataDCA("collector"),
        "argos-job",
        [response(200, body=json.dumps(record) + "\n", content_type="application/jsonl")],
    )
    scraper = ArgosDCAScraper()
    product, errors = validate(scraper._map_fields(result, url))
    check("Argos mapped result passes both gates", product is not None and not errors, errors)
    check(
        "Argos current/list prices preserved",
        product is not None and str(product.price) == "140" and str(product.list_price) == "176",
        f"price={product.price if product else None}, "
        f"list_price={product.list_price if product else None}",
    )


async def verify_multiline_jsonl() -> None:
    section("M27.3 - Multi-line JSONL returns first record")
    from src.scraping.extraction.bright_data import BrightDataDCA

    first = {"product_name": "first", "current_price": 1}
    second = {"product_name": "second", "current_price": 2}
    body = f"{json.dumps(first)}\nnot-json\n{json.dumps(second)}\n"
    result, _ = await run_poll(
        BrightDataDCA("collector"),
        "multi-job",
        [response(200, body=body, content_type="application/x-ndjson")],
    )
    check("First valid JSONL record returned", result == first, result)


async def verify_202_pending() -> None:
    section("M27.4 - HTTP 202 remains pending")
    from src.scraping.extraction.bright_data import BrightDataDCA

    ready = {"product_name": "ready"}
    result, clock = await run_poll(
        BrightDataDCA("collector"),
        "pending-202",
        [
            response(202, body='{"status":"building","message":"wait"}'),
            response(200, body=json.dumps(ready)),
        ],
    )
    check("202 followed by ready body succeeds", result == ready, result)
    check("Exactly one interval elapsed", clock.sleep_calls == 1 and clock.now == 1, clock.now)


async def verify_200_pending() -> None:
    section("M27.5 - HTTP 200 status envelope remains pending")
    from src.scraping.extraction.bright_data import BrightDataDCA

    ready = {"product_name": "ready"}
    result, clock = await run_poll(
        BrightDataDCA("collector"),
        "pending-200",
        [
            response(200, body='{"status":"BUILDING"}'),
            response(200, body=json.dumps(ready)),
        ],
    )
    check("200 building envelope followed by record succeeds", result == ready, result)
    check("Pending status is case-insensitive", clock.sleep_calls == 1, clock.sleep_calls)


async def verify_failed_status() -> None:
    section("M27.6 - Failed status raises immediately")
    from src.scraping.extraction.bright_data import BrightDataDCA

    clock = FakeClock()
    FakeAsyncClient.reset([response(200, body='{"status":"failed","error":"collector"}')])
    client_patch, time_patch, sleep_patch = polling_patches(clock)
    with client_patch, time_patch, sleep_patch:
        try:
            await BrightDataDCA("collector")._poll("failed-job")
            check("Failed envelope raises BrightDataInfraError", False, "no exception")
        except BrightDataInfraError as exc:
            check("Failed envelope raises BrightDataInfraError", "failed-job" in str(exc), exc)
    check("Failure did not sleep", clock.sleep_calls == 0, clock.sleep_calls)


async def verify_empty_and_bad_bodies() -> None:
    section("M27.7 - Empty and undecodable bodies are not terminal")
    from src.scraping.extraction.bright_data import BrightDataDCA

    cases = [
        ("empty body", response(200, body="")),
        ("empty list", response(200, body="[]")),
        ("undecodable body", response(200, body="not-json")),
    ]
    for label, initial in cases:
        ready = {"product_name": label}
        result, clock = await run_poll(
            BrightDataDCA("collector"),
            f"{label}-job",
            [initial, response(200, body=json.dumps(ready))],
        )
        check(f"{label} keeps polling", result == ready and clock.sleep_calls == 1, result)


async def verify_timeout_diagnostics_and_no_retrigger() -> None:
    section("M27.8 - Timeout diagnostics and one-trigger invariant")
    from src.scraping.extraction.bright_data import BrightDataDCA

    clock = FakeClock()
    pending = response(202, body='{"status":"building","message":"wait"}')
    FakeAsyncClient.reset(
        [response(200, body='{"collection_id":"timeout-job"}')] + [pending] * 3
    )
    client_patch, time_patch, sleep_patch = polling_patches(clock)
    with client_patch, time_patch, sleep_patch:
        try:
            await BrightDataDCA("collector").fetch("https://example.test/product")
            check("Genuine timeout raises BrightDataInfraError", False, "no exception")
        except BrightDataInfraError as exc:
            message = str(exc)
            check("Genuine timeout raises BrightDataInfraError", "timed out after" in message, message)
            check("Timeout includes status and body preview", "last_status=202" in message and "building" in message, message)
    posts = sum(method == "POST" for method, _ in FakeAsyncClient.calls)
    check("Timeout path triggers exactly once", posts == 1, posts)


async def verify_auth_failure() -> None:
    section("M27.9 - Permanent auth failure raises immediately")
    from src.scraping.extraction.bright_data import BrightDataDCA

    clock = FakeClock()
    FakeAsyncClient.reset([response(401, body='{"error":"unauthorized"}')])
    client_patch, time_patch, sleep_patch = polling_patches(clock)
    with client_patch, time_patch, sleep_patch:
        try:
            await BrightDataDCA("collector")._poll("auth-job")
            check("401 raises BrightDataInfraError", False, "no exception")
        except BrightDataInfraError as exc:
            check("401 raises BrightDataInfraError", exc.status_code == 401, exc)
    check("401 consumes no poll interval", clock.sleep_calls == 0, clock.sleep_calls)


async def verify_datasets_regressions() -> None:
    section("M27.10 - Datasets array and status-bearing records")
    from src.scraping.extraction.bright_data import BrightDataDatasets

    array_record = {"title": "Amazon array", "price": 10}
    result, clock = await run_poll(
        BrightDataDatasets("dataset"),
        "array-job",
        [response(200, body=json.dumps([array_record]))],
    )
    check("Datasets JSON array still returns first record", result == array_record, result)
    check("Datasets ready array is immediate", clock.sleep_calls == 0, clock.sleep_calls)

    status_record = {"title": "Product", "status": "available", "price": 12}
    result, clock = await run_poll(
        BrightDataDatasets("dataset"),
        "status-record-job",
        [response(200, body=json.dumps(status_record))],
    )
    check("Unknown status value belongs to a ready record", result == status_record, result)
    check("Unknown status record is immediate", clock.sleep_calls == 0, clock.sleep_calls)


async def run() -> None:
    set_config(ScrapingConfig(**TEST_CONFIG))
    verifiers = [
        verify_single_line_dca_jsonl,
        verify_argos_mapping,
        verify_multiline_jsonl,
        verify_202_pending,
        verify_200_pending,
        verify_failed_status,
        verify_empty_and_bad_bodies,
        verify_timeout_diagnostics_and_no_retrigger,
        verify_auth_failure,
        verify_datasets_regressions,
    ]
    for verifier in verifiers:
        try:
            await verifier()
        except Exception:
            FAILED.append((verifier.__name__, "EXCEPTION"))
            print(f"  [EXCEPTION] {verifier.__name__}")
            traceback.print_exc()


def main() -> int:
    return run_main(
        run,
        title=(
            "Verification script for M27 — shape-tolerant Bright Data polling\n"
            "(offline, mocked httpx.AsyncClient, no real API calls)"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())

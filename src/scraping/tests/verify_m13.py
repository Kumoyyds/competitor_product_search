"""Verification script for M13 — Amazon/Tesco DCA polling fix.

Proves the core property: at most one BD trigger per URL, even on poll timeout.
Uses mocked httpx.AsyncClient; no real API calls.

Run from repo root:
    python -m src.scraping.tests.verify_m13 | tee src/scraping/tests/verify_m13_output.log
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch
from tests._support.http import FakeAsyncClient

# Track results
from ._harness import FAILED, PASSED, check, run_main, section, use_temp_scrape_db

DB_PATH = Path(use_temp_scrape_db("verify_m13"))

# Test config — fast timeouts for tests (no real waiting)
# interval=0.1 means ~10 polls per second; max=1 means ~10 GETs before deadline
TEST_CONFIG = {
    "bd_async_poll_max_seconds": 1,
    "bd_async_poll_interval_seconds": 0.1,
    "extraction_retry_count": 2,
    "extraction_retry_interval": 0.01,  # fast retry in tests
    "bright_data_key": "test-key",
    "db_path": str(DB_PATH),
}






# ---------------------------------------------------------------------------
# Fake httpx client for mocking BD behavior
# ---------------------------------------------------------------------------



def _make_response(status_code, json_data=None, text=""):
    """Create a fake httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {}  # Real dict, not MagicMock — avoids Mock leak into _check_infra_error
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.return_value = None
    return resp


# ---------------------------------------------------------------------------
# M13.1 — Happy path: snapshot ready on 3rd poll
# ---------------------------------------------------------------------------

async def verify_datasets_happy_path() -> None:
    section("M13.1 - Datasets happy path: ready on 3rd poll")
    from src.scraping.extraction.bright_data import BrightDataDatasets
    from src.scraping.config import set_config, ScrapingConfig

    # Queue: POST→200+id, GET→202, GET→202, GET→200+list
    response_queue = [
        _make_response(200, {"snapshot_id": "snap123"}),
        _make_response(202),
        _make_response(202),
        _make_response(200, [{"title": "Test Product", "price": 10.99}]),
    ]

    set_config(ScrapingConfig(**TEST_CONFIG))
    FakeAsyncClient.set_shared_queue(response_queue)

    with patch("httpx.AsyncClient", new=FakeAsyncClient):
        client = BrightDataDatasets(dataset_id="gd_test")
        result = await client.fetch("https://www.amazon.co.uk/dp/TEST")

    # Queue depleted: POST + 3 GETs = 4 items consumed
    check("Response queue depleted (4 items consumed)", len(response_queue) == 0, f"{len(response_queue)} remaining")
    check("Returned data has title", result.get("title") == "Test Product", result.get("title"))


# ---------------------------------------------------------------------------
# M13.2 — Poll timeout: never ready (core bug proof)
# ---------------------------------------------------------------------------

async def verify_datasets_poll_timeout() -> None:
    section("M13.2 - Datasets poll timeout: infinite 202s (core bug proof)")
    from src.scraping.extraction.bright_data import BrightDataDatasets
    from src.scraping.exceptions import BrightDataInfraError
    from src.scraping.config import set_config, ScrapingConfig

    code_calls = []  # track all POST/GET made by the code under test

    # Queue: POST→200+id, then many 202s
    # With 1s budget + 0.1s interval, ~10 GETs before deadline → 14 202s is enough
    response_queue = [_make_response(200, {"snapshot_id": "snap456"})] + [_make_response(202)] * 14

    set_config(ScrapingConfig(**TEST_CONFIG))
    FakeAsyncClient.set_shared_queue(response_queue)
    FakeAsyncClient.set_shared_tracker(code_calls)

    with patch("httpx.AsyncClient", new=FakeAsyncClient):
        client = BrightDataDatasets(dataset_id="gd_test")
        try:
            await client.fetch("https://www.amazon.co.uk/dp/TEST")
            check("Raised BrightDataInfraError on timeout", False, "no exception raised")
        except BrightDataInfraError as e:
            check("Raised BrightDataInfraError on timeout", True, str(e))
            check("Error message mentions timeout", "timed out" in str(e).lower(), str(e))

    # Core bug proof: exactly 1 POST (no re-trigger)
    post_count = sum(1 for method, _ in code_calls if method == "POST")
    get_count = sum(1 for method, _ in code_calls if method == "GET")
    check("Exactly 1 POST (no re-trigger)", post_count == 1, f"got {post_count} POSTs")
    check("Multiple GETs before timeout", get_count >= 2, f"got {get_count} GETs")

    # Clean up class-level state
    del FakeAsyncClient._shared_tracker


# ---------------------------------------------------------------------------
# M13.3 — Snapshot status=failed
# ---------------------------------------------------------------------------

async def verify_datasets_status_failed() -> None:
    section("M13.3 - Datasets snapshot status=failed")
    from src.scraping.extraction.bright_data import BrightDataDatasets
    from src.scraping.exceptions import BrightDataInfraError
    from src.scraping.config import set_config, ScrapingConfig

    response_queue = [
        _make_response(200, {"snapshot_id": "snap789"}),
        _make_response(200, {"status": "failed", "error": "bot detected"}),
    ]

    set_config(ScrapingConfig(**TEST_CONFIG))
    FakeAsyncClient.set_shared_queue(response_queue)

    with patch("httpx.AsyncClient", new=FakeAsyncClient):
        client = BrightDataDatasets(dataset_id="gd_test")
        try:
            await client.fetch("https://www.amazon.co.uk/dp/TEST")
            check("Raised BrightDataInfraError on status=failed", False, "no exception raised")
        except BrightDataInfraError as e:
            check("Raised BrightDataInfraError on status=failed", True, str(e))

    check("Response queue depleted (2 items consumed)", len(response_queue) == 0, f"{len(response_queue)} remaining")


# ---------------------------------------------------------------------------
# M13.4 — Trigger retry then success (regression via with_extraction_retry)
# ---------------------------------------------------------------------------

async def verify_datasets_trigger_retry() -> None:
    section("M13.4 - Datasets trigger retry then success (regression)")
    from src.scraping.extraction.bright_data import BrightDataDatasets
    from src.scraping.extraction.retry import with_extraction_retry
    from src.scraping.config import set_config, ScrapingConfig

    code_calls = []
    # Queue: 2 failed POSTs (502), 1 success POST (200+id), 1 success GET (200+data)
    response_queue = [
        _make_response(502, text="Bad Gateway"),
        _make_response(502, text="Bad Gateway"),
        _make_response(200, {"snapshot_id": "snap101"}),
        _make_response(200, [{"title": "Retried Success", "price": 5.99}]),
    ]

    set_config(ScrapingConfig(**TEST_CONFIG))
    FakeAsyncClient.set_shared_queue(response_queue)
    FakeAsyncClient.set_shared_tracker(code_calls)

    with patch("httpx.AsyncClient", new=FakeAsyncClient):
        client = BrightDataDatasets(dataset_id="gd_test")
        # Mirror what AmazonUKScraper._fetch_json does: retry only _trigger, not _poll
        snapshot_id = await with_extraction_retry(client._trigger, "https://www.amazon.co.uk/dp/TEST")
        result = await client._poll(snapshot_id)

    post_count = sum(1 for method, _ in code_calls if method == "POST")
    get_count = sum(1 for method, _ in code_calls if method == "GET")
    check("3 POSTs (trigger retry preserved)", post_count == 3, f"got {post_count} POSTs")
    check("1 GET (poll after successful trigger)", get_count == 1, f"got {get_count} GETs")
    check("Returned data has title", result.get("title") == "Retried Success", result.get("title"))

    del FakeAsyncClient._shared_tracker


# ---------------------------------------------------------------------------
# M13.5 — Trigger all-fail (via with_extraction_retry)
# ---------------------------------------------------------------------------

async def verify_datasets_trigger_all_fail() -> None:
    section("M13.5 - Datasets trigger all-fail")
    from src.scraping.extraction.bright_data import BrightDataDatasets
    from src.scraping.extraction.retry import with_extraction_retry
    from src.scraping.exceptions import BrightDataInfraError
    from src.scraping.config import set_config, ScrapingConfig

    code_calls = []
    # Queue: 3 POST→502 (one per retry attempt; extraction_retry_count=2 → 3 total)
    response_queue = [_make_response(502, text="Bad Gateway")] * 3

    set_config(ScrapingConfig(**TEST_CONFIG))
    FakeAsyncClient.set_shared_queue(response_queue)
    FakeAsyncClient.set_shared_tracker(code_calls)

    with patch("httpx.AsyncClient", new=FakeAsyncClient):
        client = BrightDataDatasets(dataset_id="gd_test")
        try:
            await with_extraction_retry(client._trigger, "https://www.amazon.co.uk/dp/TEST")
            check("Raised BrightDataInfraError after all trigger failures", False, "no exception raised")
        except BrightDataInfraError as e:
            check("Raised BrightDataInfraError after all trigger failures", True, str(e))

    post_count = sum(1 for method, _ in code_calls if method == "POST")
    get_count = sum(1 for method, _ in code_calls if method == "GET")
    check("3 POSTs (all trigger attempts)", post_count == 3, f"got {post_count} POSTs")
    check("0 GETs (no successful trigger to poll)", get_count == 0, f"got {get_count} GETs")

    del FakeAsyncClient._shared_tracker


# ---------------------------------------------------------------------------
# M13.6 — DCA happy path
# ---------------------------------------------------------------------------

async def verify_dca_happy_path() -> None:
    section("M13.6 - DCA happy path")
    from src.scraping.extraction.bright_data import BrightDataDCA
    from src.scraping.config import set_config, ScrapingConfig

    # DCA uses collection_id, same pattern
    response_queue = [
        _make_response(200, {"collection_id": "coll123"}),
        _make_response(202),
        _make_response(202),
        _make_response(200, [{"product_name": "DCA Product", "current_price": 8.99}]),
    ]

    set_config(ScrapingConfig(**TEST_CONFIG))
    FakeAsyncClient.set_shared_queue(response_queue)

    with patch("httpx.AsyncClient", new=FakeAsyncClient):
        client = BrightDataDCA(collector_id="c_test")
        result = await client.fetch("https://www.tesco.com/shop/test")

    check("Returned data has product_name", result.get("product_name") == "DCA Product", result.get("product_name"))
    check("Response queue depleted (4 items consumed)", len(response_queue) == 0, f"{len(response_queue)} remaining")


# ---------------------------------------------------------------------------
# M13.7 — DCA poll timeout
# ---------------------------------------------------------------------------

async def verify_dca_poll_timeout() -> None:
    section("M13.7 - DCA poll timeout")
    from src.scraping.extraction.bright_data import BrightDataDCA
    from src.scraping.exceptions import BrightDataInfraError
    from src.scraping.config import set_config, ScrapingConfig

    code_calls = []
    response_queue = [_make_response(200, {"collection_id": "coll456"})] + [_make_response(202)] * 14

    set_config(ScrapingConfig(**TEST_CONFIG))
    FakeAsyncClient.set_shared_queue(response_queue)
    FakeAsyncClient.set_shared_tracker(code_calls)

    with patch("httpx.AsyncClient", new=FakeAsyncClient):
        client = BrightDataDCA(collector_id="c_test")
        try:
            await client.fetch("https://www.tesco.com/shop/test")
            check("Raised BrightDataInfraError on timeout", False, "no exception raised")
        except BrightDataInfraError as e:
            check("Raised BrightDataInfraError on timeout", True, str(e))

    post_count = sum(1 for method, _ in code_calls if method == "POST")
    check("Exactly 1 POST (no re-trigger)", post_count == 1, f"got {post_count} POSTs")

    del FakeAsyncClient._shared_tracker


# ---------------------------------------------------------------------------
# M13.8 — HTML route regression: with_extraction_retry unchanged
# ---------------------------------------------------------------------------

async def verify_with_extraction_retry_unchanged() -> None:
    section("M13.8 - HTML route regression: with_extraction_retry unchanged")
    from src.scraping.extraction.retry import with_extraction_retry
    from src.scraping.exceptions import BrightDataInfraError

    call_count = []

    async def _fn_raises_twice_then_returns(*args, **kwargs):
        call_count.append(1)
        if len(call_count) <= 2:
            raise BrightDataInfraError("test error")
        return "ok"

    result = await with_extraction_retry(_fn_raises_twice_then_returns)
    check("with_extraction_retry calls fn 3 times", len(call_count) == 3, str(len(call_count)))
    check("with_extraction_retry returns final result", result == "ok", result)


# ---------------------------------------------------------------------------
# M13.9 — First poll is immediate (no sleep before first GET)
# ---------------------------------------------------------------------------

async def verify_first_poll_immediate() -> None:
    section("M13.9 - First poll is immediate (no sleep before first GET)")
    from src.scraping.extraction.bright_data import BrightDataDatasets
    from src.scraping.config import set_config, ScrapingConfig

    # Queue: POST→200+id, GET→200+list (ready immediately)
    response_queue = [
        _make_response(200, {"snapshot_id": "snap789"}),
        _make_response(200, [{"title": "Immediate", "price": 3.99}]),
    ]

    set_config(ScrapingConfig(**TEST_CONFIG))
    FakeAsyncClient.set_shared_queue(response_queue)

    start = time.monotonic()
    with patch("httpx.AsyncClient", new=FakeAsyncClient):
        client = BrightDataDatasets(dataset_id="gd_test")
        result = await client.fetch("https://www.amazon.co.uk/dp/TEST")
    elapsed = time.monotonic() - start

    check("Returned data has title", result.get("title") == "Immediate", result.get("title"))
    # 0.1s is the poll interval — a first-GET-before-sleep would finish well under that
    check(
        f"First poll was immediate (no sleep before first GET, elapsed {elapsed:.3f}s)",
        elapsed < 0.09,
        f"{elapsed:.3f}s"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run() -> None:
    verifiers = [
        verify_datasets_happy_path,
        verify_datasets_poll_timeout,
        verify_datasets_status_failed,
        verify_datasets_trigger_retry,
        verify_datasets_trigger_all_fail,
        verify_dca_happy_path,
        verify_dca_poll_timeout,
        verify_with_extraction_retry_unchanged,
        verify_first_poll_immediate,
    ]

    for fn in verifiers:
        try:
            await fn()
        except Exception:
            FAILED.append((fn.__name__, "EXCEPTION"))
            print(f"  [EXCEPTION] {fn.__name__}")
            traceback.print_exc()


def main() -> int:
    return run_main(
        run,
        title=(
            "Verification script for M13 — Amazon/Tesco DCA polling fix\n"
            "(offline, mocked httpx.AsyncClient, no real API calls)"
        ),
        width=70,
    )


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping, Optional

import httpx

from ..config import get_config
from ..exceptions import BrightDataInfraError

logger = logging.getLogger(__name__)

_INFRA_STATUS_CODES = {407, 429, 500, 502, 503, 504}
_BD_API_BASE = "https://api.brightdata.com"

# Body content markers indicating an upstream server error page (nginx, CDN,
# Cloudflare, etc.) — appear only in gateway/error HTML, never in real product
# pages. Used to catch the "1000-5000 byte upstream error" gap where the body
# is too large for _MIN_HTML_BODY_LENGTH to fire but too small for detect_invalid_page
# to legitimately classify.
_UPSTREAM_ERROR_MARKERS = (
    "500 Internal Server Error",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway Timeout",
    "Gateway Time-out",  # nginx variant
)

# Bodies below this length on an HTTP 200 are treated as infra failures
# (proxy pool cycle, JS challenge stub, or upstream anti-bot serving a blank
# page). Real product pages ship at least a few KB of SPA shell even for
# "not found" errors. Setting this above 500 catches observed BrightData flakes
# (0 chars and 122 chars) while staying well below detection.py's 5000-char
# `page_length` bar for genuinely short pages.
_MIN_HTML_BODY_LENGTH = 1000


def _check_infra_error(
    status_code: int,
    body: str,
    headers: Optional[Mapping[str, str]] = None,
    expect_html: bool = False,
) -> None:
    if status_code in _INFRA_STATUS_CODES:
        raise BrightDataInfraError(
            f"Bright Data infra error: HTTP {status_code} — {body[:200]}",
            status_code=status_code,
        )
    # BrightData explicitly signals fetch problems via response headers.
    # `x-brd-error-code` is the authoritative signal (observed values include
    # `min_size`, `reject_block`, `networkidle_event_timeout`,
    # `bucket_rate_limit`). Applies to all BD endpoints (Unlocker, Datasets
    # trigger, DCA trigger).
    if headers is not None:
        bd_error = headers.get("x-brd-error-code")
        if bd_error:
            bd_message = headers.get("x-brd-error-message", "")
            # Soft-tolerate: when Web Unlocker returned substantial HTML
            # alongside the error header, BD's flag may be a soft warning
            # (e.g. `min_size` on a shorter-than-expected but real error
            # page, or a partial-render `networkidle_event_timeout`). Let
            # detect_invalid_page + repair ladder Turn A do the final
            # classification via structural / LLM signals instead of failing
            # hard here. Log at WARN so operator visibility into BD upstream
            # health is preserved even when downstream returns invalid_target.
            # Only applies when expect_html=True (HTML endpoints); JSON
            # trigger endpoints always fail fast on any error header because
            # their bodies are legitimately tiny.
            if expect_html and status_code == 200 and len(body) >= _MIN_HTML_BODY_LENGTH:
                logger.warning(
                    "Bright Data upstream soft error tolerated: "
                    "x-brd-error-code=%s%s (body has %d chars, letting "
                    "detect_invalid_page decide)",
                    bd_error,
                    f" ({bd_message})" if bd_message else "",
                    len(body),
                )
            else:
                raise BrightDataInfraError(
                    f"Bright Data upstream error: x-brd-error-code={bd_error}"
                    + (f" ({bd_message})" if bd_message else ""),
                    status_code=status_code,
                )
    # Body-length fallback: some BrightData response paths omit the error
    # header even when the body is a JS challenge / anti-bot stub. Only apply
    # this to endpoints that are supposed to return a full HTML page — the
    # Datasets/DCA trigger endpoints legitimately return small JSON payloads
    # like {"snapshot_id": "..."} which are well under this threshold.
    if expect_html and status_code == 200 and len(body) < _MIN_HTML_BODY_LENGTH:
        raise BrightDataInfraError(
            f"Bright Data returned near-empty body: {len(body)} chars "
            f"(min {_MIN_HTML_BODY_LENGTH})",
            status_code=status_code,
        )
    # Upstream server error content: nginx/CDN/Cloudflare 5xx error pages that
    # come in between _MIN_HTML_BODY_LENGTH (1000) and detect_invalid_page's
    # own 5000-char threshold slip through both — the body is too long for the
    # empty-body check, too short to be a real product page, and if we hand it
    # to detection it gets labelled invalid_target (terminal, no retry). Catch
    # them here by their well-known status-line text so with_extraction_retry
    # pauses 2s and tries again.
    if expect_html and status_code == 200 and len(body) < 5000:
        for marker in _UPSTREAM_ERROR_MARKERS:
            if marker in body:
                raise BrightDataInfraError(
                    f"Upstream server error in response body: {marker!r} "
                    f"(body {len(body)} chars)",
                    status_code=status_code,
                )


class BrightDataUnlocker:
    """Web Unlocker client for raw HTML extraction (Argos/Tesco)."""

    def __init__(
        self,
        zone: str = "web_unlocker1",
        country: Optional[str] = None,
    ):
        self._zone = zone
        self._country = country

    async def fetch(self, url: str) -> tuple[int, str]:
        cfg = get_config()
        payload: dict[str, Any] = {
            "zone": self._zone,
            "url": url,
            "format": "raw",
            "method": "GET",
        }
        if self._country:
            payload["country"] = self._country

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_BD_API_BASE}/request",
                json=payload,
                headers={
                    "Authorization": f"Bearer {cfg.bright_data_key}",
                    "Content-Type": "application/json",
                },
            )
        _check_infra_error(resp.status_code, resp.text, resp.headers, expect_html=True)
        return resp.status_code, resp.text


class BrightDataDatasets:
    """Datasets API v3 client for structured JSON (Amazon)."""

    def __init__(self, dataset_id: str = "gd_l7q7dkf244hwjntr0"):
        self._dataset_id = dataset_id

    async def _trigger(self, url: str, **extra_input: str) -> str:
        """Trigger a snapshot and return its ID. Retried on transient failures."""
        cfg = get_config()
        headers = {
            "Authorization": f"Bearer {cfg.bright_data_key}",
            "Content-Type": "application/json",
        }

        input_item: dict[str, str] = {"url": url, **extra_input}

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_BD_API_BASE}/datasets/v3/trigger",
                json=[input_item],
                headers=headers,
                params={
                    "dataset_id": self._dataset_id,
                    "include_errors": "true",
                },
            )
            _check_infra_error(resp.status_code, resp.text, resp.headers)
            if resp.status_code != 200:
                raise BrightDataInfraError(
                    f"Datasets trigger failed: {resp.status_code} {resp.text[:200]}",
                    status_code=resp.status_code,
                )
            return resp.json()["snapshot_id"]

    async def _poll(self, snapshot_id: str) -> dict[str, Any]:
        """Poll a snapshot until ready or deadline, raising on failure."""
        cfg = get_config()
        headers = {
            "Authorization": f"Bearer {cfg.bright_data_key}",
            "Content-Type": "application/json",
        }
        deadline = time.monotonic() + cfg.bd_async_poll_max_seconds

        async with httpx.AsyncClient(timeout=60) as client:
            # poll until ready — first GET is immediate (no sleep before it)
            while True:
                try:
                    status_resp = await client.get(
                        f"{_BD_API_BASE}/datasets/v3/snapshot/{snapshot_id}",
                        headers=headers,
                    )
                except httpx.HTTPError as e:
                    # transient network/timeout on a single poll — keep polling
                    logger.debug("Transient poll error (retrying): %s", e)
                else:
                    if status_resp.status_code == 200:
                        data = status_resp.json()
                        if isinstance(data, list) and data:
                            return data[0]
                        if isinstance(data, dict):
                            status = data.get("status")
                            if status is None:
                                return data
                            if status in ("failed", "error"):
                                raise BrightDataInfraError(
                                    f"Snapshot failed: {data}",
                                    status_code=status_resp.status_code,
                                )
                    # non-200 (including 202 "still running") — keep polling

                # check deadline before sleeping
                if time.monotonic() >= deadline:
                    elapsed = time.monotonic() - (deadline - cfg.bd_async_poll_max_seconds)
                    raise BrightDataInfraError(
                        f"Snapshot polling timed out after {elapsed:.0f}s (id={snapshot_id})"
                    )

                await asyncio.sleep(cfg.bd_async_poll_interval_seconds)

    async def fetch(self, url: str, **extra_input: str) -> dict[str, Any]:
        """Convenience wrapper: trigger then poll. Public API for direct test callers."""
        snapshot_id = await self._trigger(url, **extra_input)
        return await self._poll(snapshot_id)


class BrightDataDCA:
    """Data Collection API client for Tesco (backup route)."""

    def __init__(self, collector_id: str = "c_mr6mrw40d614thtpd"):
        self._collector_id = collector_id

    async def _trigger(self, url: str) -> str:
        """Trigger a collection and return its ID. Retried on transient failures."""
        cfg = get_config()
        headers = {
            "Authorization": f"Bearer {cfg.bright_data_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_BD_API_BASE}/dca/trigger",
                json=[{"url": url}],
                headers=headers,
                params={"collector": self._collector_id, "queue_next": "1"},
            )
            _check_infra_error(resp.status_code, resp.text, resp.headers)
            if resp.status_code != 200:
                raise BrightDataInfraError(
                    f"DCA trigger failed: {resp.status_code} {resp.text[:200]}",
                    status_code=resp.status_code,
                )
            return resp.json()["collection_id"]

    async def _poll(self, collection_id: str) -> dict[str, Any]:
        """Poll a collection until ready or deadline, raising on failure."""
        cfg = get_config()
        headers = {
            "Authorization": f"Bearer {cfg.bright_data_key}",
            "Content-Type": "application/json",
        }
        deadline = time.monotonic() + cfg.bd_async_poll_max_seconds

        async with httpx.AsyncClient(timeout=60) as client:
            # poll until ready — first GET is immediate (no sleep before it)
            while True:
                try:
                    status_resp = await client.get(
                        f"{_BD_API_BASE}/dca/dataset",
                        headers=headers,
                        params={"id": collection_id},
                    )
                except httpx.HTTPError as e:
                    # transient network/timeout on a single poll — keep polling
                    logger.debug("Transient DCA poll error (retrying): %s", e)
                else:
                    if status_resp.status_code == 200:
                        data = status_resp.json()
                        # DCA's terminal detection: only list check (no status field)
                        if isinstance(data, list) and data:
                            return data[0]
                    # non-200 (including 202 "still running") — keep polling

                # check deadline before sleeping
                if time.monotonic() >= deadline:
                    elapsed = time.monotonic() - (deadline - cfg.bd_async_poll_max_seconds)
                    raise BrightDataInfraError(
                        f"DCA collection polling timed out after {elapsed:.0f}s (id={collection_id})"
                    )

                await asyncio.sleep(cfg.bd_async_poll_interval_seconds)

    async def fetch(self, url: str) -> dict[str, Any]:
        """Convenience wrapper: trigger then poll. Public API for direct test callers."""
        collection_id = await self._trigger(url)
        return await self._poll(collection_id)

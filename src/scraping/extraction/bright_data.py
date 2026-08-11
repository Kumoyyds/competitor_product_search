from __future__ import annotations

import asyncio
import json
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

_POLL_PENDING_STATUSES = {
    "building",
    "pending",
    "running",
    "collecting",
    "queued",
    "scheduled",
    "started",
    "in_progress",
}
_POLL_FAILED_STATUSES = {
    "failed",
    "error",
    "cancelled",
    "canceled",
    "aborted",
}


def _parse_poll_records(resp: httpx.Response) -> list[Any] | None:
    """Normalize JSON/JSONL poll responses into a non-empty record list."""
    body = resp.text
    content_type = resp.headers.get("content-type", "").lower()
    non_empty_lines = [line for line in body.splitlines() if line.strip()]

    if "jsonl" in content_type or "ndjson" in content_type or len(non_empty_lines) > 1:
        records: list[Any] = []
        for line in non_empty_lines:
            try:
                records.append(json.loads(line))
            except (ValueError, json.JSONDecodeError):
                continue
        data: Any = records
    else:
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            return None

    if isinstance(data, list) and data:
        return data
    if isinstance(data, dict):
        return [data]
    return None


def _classify_poll_record(record: Any) -> str:
    """Classify a poll record as pending, failed, or ready."""
    if not isinstance(record, dict):
        return "ready"
    status = record.get("status")
    if not isinstance(status, str):
        return "ready"
    normalized = status.strip().lower()
    if normalized in _POLL_PENDING_STATUSES:
        return "pending"
    if normalized in _POLL_FAILED_STATUSES:
        return "failed"
    return "ready"


async def _poll_until_ready(
    get_url: str,
    params: Mapping[str, str] | None,
    job_id: str,
    kind: str,
) -> dict[str, Any]:
    """Poll one Bright Data async job without ever re-triggering it."""
    cfg = get_config()
    headers = {
        "Authorization": f"Bearer {cfg.bright_data_key}",
        "Content-Type": "application/json",
    }
    started_at = time.monotonic()
    deadline = started_at + cfg.bd_async_poll_max_seconds
    polls = 0
    last_status: int | None = None
    last_body_preview = ""
    logged_nonterminal_200 = False

    async with httpx.AsyncClient(timeout=60) as client:
        # First GET is immediate. A poll failure never creates a new job.
        while True:
            polls += 1
            try:
                status_resp = await client.get(
                    get_url,
                    headers=headers,
                    params=params,
                )
            except httpx.HTTPError as e:
                # Tolerate a transient failure of one GET within the same
                # wall-clock budget; the next iteration polls the same job.
                logger.debug("Transient %s poll error (retrying): %s", kind, e)
            else:
                last_status = status_resp.status_code
                last_body_preview = status_resp.text[:200]

                if status_resp.status_code in (401, 403):
                    raise BrightDataInfraError(
                        f"{kind} polling authorization failed: HTTP "
                        f"{status_resp.status_code} (id={job_id}, "
                        f"body={last_body_preview!r})",
                        status_code=status_resp.status_code,
                    )

                if status_resp.status_code == 200:
                    records = _parse_poll_records(status_resp)
                    if records:
                        classification = _classify_poll_record(records[0])
                        if classification == "ready":
                            record = records[0]
                            if isinstance(record, dict):
                                return record
                            raise BrightDataInfraError(
                                f"{kind} returned a non-object record "
                                f"(id={job_id}, record={record!r})",
                                status_code=status_resp.status_code,
                            )
                        if classification == "failed":
                            raise BrightDataInfraError(
                                f"{kind} failed (id={job_id}): {records[0]}",
                                status_code=status_resp.status_code,
                            )

                    log = logger.info if not logged_nonterminal_200 else logger.debug
                    log(
                        "%s poll returned non-terminal HTTP 200 "
                        "(content-type=%s, body=%r)",
                        kind,
                        status_resp.headers.get("content-type", ""),
                        last_body_preview,
                    )
                    logged_nonterminal_200 = True
                # Other statuses, including 202, remain pending.

            if time.monotonic() >= deadline:
                elapsed = time.monotonic() - started_at
                raise BrightDataInfraError(
                    f"{kind} polling timed out after {elapsed:.0f}s "
                    f"(id={job_id}, polls={polls}, last_status={last_status}, "
                    f"last_body={last_body_preview!r})"
                )

            await asyncio.sleep(cfg.bd_async_poll_interval_seconds)


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
        # Decode explicitly — httpx mis-guesses charset on some sites (e.g.
        # Tesco is served as UTF-8 but httpx guesses Latin-1, turning '£' into
        # 'Â£').  Honor an explicitly declared Content-Type charset if present;
        # otherwise default to UTF-8 (every modern retailer uses it).
        enc = resp.charset_encoding or "utf-8"
        html = resp.content.decode(enc, errors="replace")
        return resp.status_code, html


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
        return await _poll_until_ready(
            f"{_BD_API_BASE}/datasets/v3/snapshot/{snapshot_id}",
            None,
            snapshot_id,
            "Snapshot",
        )

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
        return await _poll_until_ready(
            f"{_BD_API_BASE}/dca/dataset",
            {"id": collection_id},
            collection_id,
            "DCA collection",
        )

    async def fetch(self, url: str) -> dict[str, Any]:
        """Convenience wrapper: trigger then poll. Public API for direct test callers."""
        collection_id = await self._trigger(url)
        return await self._poll(collection_id)

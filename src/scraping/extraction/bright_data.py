from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

import httpx

from ..config import get_config
from ..exceptions import BrightDataInfraError

logger = logging.getLogger(__name__)

_INFRA_STATUS_CODES = {407, 429, 503}
_BD_API_BASE = "https://api.brightdata.com"

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
    # `min_size`, `reject_block`, `networkidle_event_timeout`). Trust this over
    # heuristic body-length inspection because BD flags failures the moment
    # the upstream returns a stub/challenge, before we even see the body.
    # Applies to all BD endpoints (Unlocker, Datasets trigger, DCA trigger).
    if headers is not None:
        bd_error = headers.get("x-brd-error-code")
        if bd_error:
            bd_message = headers.get("x-brd-error-message", "")
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

    async def fetch(self, url: str, **extra_input: str) -> dict[str, Any]:
        cfg = get_config()
        headers = {
            "Authorization": f"Bearer {cfg.bright_data_key}",
            "Content-Type": "application/json",
        }

        input_item: dict[str, str] = {"url": url, **extra_input}

        async with httpx.AsyncClient(timeout=120) as client:
            # trigger
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
            snapshot_id = resp.json()["snapshot_id"]

            # poll until ready
            for _ in range(30):
                await asyncio.sleep(4)
                status_resp = await client.get(
                    f"{_BD_API_BASE}/datasets/v3/snapshot/{snapshot_id}",
                    headers=headers,
                )
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

            raise BrightDataInfraError("Snapshot polling timed out")


class BrightDataDCA:
    """Data Collection API client for Tesco (backup route)."""

    def __init__(self, collector_id: str = "c_mr6mrw40d614thtpd"):
        self._collector_id = collector_id

    async def fetch(self, url: str) -> dict[str, Any]:
        cfg = get_config()
        headers = {
            "Authorization": f"Bearer {cfg.bright_data_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            # trigger
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
            collection_id = resp.json()["collection_id"]

            # poll until ready
            for _ in range(30):
                await asyncio.sleep(4)
                status_resp = await client.get(
                    f"{_BD_API_BASE}/dca/dataset",
                    headers=headers,
                    params={"id": collection_id},
                )
                if status_resp.status_code == 200:
                    data = status_resp.json()
                    if isinstance(data, list) and data:
                        return data[0]

            raise BrightDataInfraError("DCA collection polling timed out")

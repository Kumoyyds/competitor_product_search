from __future__ import annotations

import asyncio
import os
from typing import Any

import aiohttp
from dotenv import load_dotenv

from ..models import RawCandidate
from .base import BudgetExhausted, SearchProvider, SearchProviderError


_ENDPOINT = "https://google.serper.dev/search"

# Map public-API country-code arguments to Serper's ISO 3166-1 alpha-2 gl param.
# Serper uses "gb" not "uk" for the United Kingdom.
_COUNTRY_TO_GL: dict[str, str] = {
    "uk": "gb",  # United Kingdom  (input "uk" → Serper expects "gb")
    "gb": "gb",  # United Kingdom
    "de": "de",  # Germany
    "fr": "fr",  # France
    "nl": "nl",  # Netherlands
    "us": "us",  # United States
    "es": "es",  # Spain
    "it": "it",  # Italy
    "jp": "jp",  # Japan
    "au": "au",  # Australia
    "ca": "ca",  # Canada
    "br": "br",  # Brazil
    "pl": "pl",  # Poland
    "se": "se",  # Sweden
    "pt": "pt",  # Portugal
}


def _to_gl(country: str) -> str:
    code = country.lower().strip()
    return _COUNTRY_TO_GL.get(code, code)


class SerperProvider(SearchProvider):
    name = "serper"

    def __init__(
        self,
        api_key: str | None = None,
        max_calls: int | None = None,
        timeout_s: float = 20.0,
    ):
        load_dotenv()
        self._api_key = api_key or os.getenv("SERPER_KEY")
        if not self._api_key:
            raise SearchProviderError("SERPER_KEY not set in environment")
        self._max_calls = max_calls
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._calls = 0
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _reserve_call(self) -> None:
        async with self._lock:
            if self._max_calls is not None and self._calls >= self._max_calls:
                raise BudgetExhausted(
                    f"serper call budget exhausted ({self._calls}/{self._max_calls})"
                )
            self._calls += 1

    def calls_made(self) -> int:
        return self._calls

    async def search(self, query: str, k: int = 10, country: str = "gb") -> list[RawCandidate]:
        await self._reserve_call()
        session = await self._ensure_session()
        payload: dict[str, Any] = {"q": query, "num": k, "gl": _to_gl(country)}
        headers = {"X-API-KEY": self._api_key, "Content-Type": "application/json"}
        try:
            async with session.post(_ENDPOINT, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise SearchProviderError(f"serper {resp.status}: {body[:200]}")
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise SearchProviderError(f"serper network error: {e}") from e

        out: list[RawCandidate] = []
        for item in (data.get("organic") or [])[:k]:
            link = item.get("link")
            title = item.get("title")
            if not link or not title:
                continue
            out.append(RawCandidate(title=title, url=link, snippet=item.get("snippet") or ""))
        return out

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

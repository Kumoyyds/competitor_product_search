from __future__ import annotations

import asyncio
import time as _time
from typing import Any

from ..models import RawCandidate
from .base import BudgetExhausted, SearchProvider, SearchProviderError


# ---------------------------------------------------------------------------
# Map Serper-style ISO-3166-1 alpha-2 country codes to DDG region strings.
# DDG uses the format "{country}-{language}" (e.g. "de-de", "uk-en").
# Fallback: "{code}-en" when no explicit mapping exists.
# ---------------------------------------------------------------------------
_COUNTRY_TO_REGION: dict[str, str] = {
    "uk": "uk-en",  # United Kingdom      — English
    "gb": "uk-en",  # United Kingdom      — English (alias of "uk")
    "de": "de-de",  # Germany             — German
    "fr": "fr-fr",  # France              — French
    "us": "us-en",  # United States       — English
    "nl": "nl-nl",  # Netherlands         — Dutch
    "jp": "jp-ja",  # Japan               — Japanese
    "es": "es-es",  # Spain               — Spanish
    "it": "it-it",  # Italy               — Italian
    "pt": "pt-pt",  # Portugal            — Portuguese
    "se": "se-sv",  # Sweden              — Swedish
    "pl": "pl-pl",  # Poland              — Polish
    "br": "br-pt",  # Brazil              — Portuguese
    "au": "au-en",  # Australia           — English
    "ca": "ca-en",  # Canada              — English
}


def _to_region(country: str) -> str:
    """Convert an ISO country code to a DDG ``region`` value."""
    code = country.lower().strip()
    region = _COUNTRY_TO_REGION.get(code)
    if region is not None:
        return region
    # Best-effort fallback
    if "-" in code:
        return code  # already looks like a region string
    return f"{code}-en"


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect rate-limit-shaped errors from the ddgs library (HTTP 429 etc.)."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in msg
        for marker in ("ratelimit", "rate limit", "429", "too many requests")
    )


class DuckDuckGoProvider(SearchProvider):
    """Search provider backed by DuckDuckGo (free — no API key required).

    Uses the ``ddgs`` library (v9+) internally.  Because that library is
    synchronous, every ``search()`` call is offloaded to a thread via
    :func:`asyncio.to_thread` so the event loop stays responsive.

    A minimum inter-call interval (default 1.0 s) is enforced to avoid
    triggering DuckDuckGo's rate-limiter.  Tune with *min_interval_s*.
    """

    name = "duckduckgo"

    def __init__(
        self,
        max_calls: int | None = None,
        timeout_s: float = 30.0,
        min_interval_s: float = 1.0,
    ):
        self._max_calls = max_calls
        self._timeout = timeout_s
        self._min_interval = min_interval_s
        self._calls = 0
        self._lock = asyncio.Lock()
        self._last_call_s: float = 0.0
        self._ddgs: Any = None  # lazy-init DDGS instance

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_ddgs(self):
        """Lazy-init the DDGS client (the import is moderately heavy)."""
        if self._ddgs is None:
            from ddgs import DDGS

            self._ddgs = DDGS()
        return self._ddgs

    async def _reserve_call(self) -> None:
        """Claim a call slot or raise BudgetExhausted."""
        async with self._lock:
            if self._max_calls is not None and self._calls >= self._max_calls:
                raise BudgetExhausted(
                    f"duckduckgo call budget exhausted "
                    f"({self._calls}/{self._max_calls})"
                )
            self._calls += 1

    async def _enforce_interval(self) -> None:
        """Sleep until *min_interval_s* has elapsed since the last call."""
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = _time.monotonic()
            wait = self._last_call_s + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_s = _time.monotonic()

    # ------------------------------------------------------------------
    # SearchProvider interface
    # ------------------------------------------------------------------

    def calls_made(self) -> int:
        return self._calls

    async def search(
        self, query: str, k: int = 10, country: str = "uk"
    ) -> list[RawCandidate]:
        """Execute a search against DuckDuckGo.

        Parameters
        ----------
        query:
            The search string (product name + retailer keywords, etc.).
        k:
            Maximum number of results to return (passed as *max_results* to DDGS).
        country:
            ISO-3166-1 alpha-2 country code (e.g. ``"de"``, ``"uk"``).
            Mapped internally to a DDG region string like ``"de-de"``.
        """
        await self._reserve_call()
        await self._enforce_interval()

        region = _to_region(country)
        ddgs = self._get_ddgs()

        # Retry up to 3 attempts on rate-limit errors (1s pause between).
        # Non-rate-limit errors propagate immediately.
        last_exc: BaseException | None = None
        results: list[dict[str, str]] | None = None
        for attempt in range(3):
            try:
                results = await asyncio.to_thread(
                    lambda: list(ddgs.text(query, max_results=k, region=region))
                )
                break
            except Exception as exc:
                last_exc = exc
                if not _is_rate_limit_error(exc):
                    raise SearchProviderError(
                        f"duckduckgo search error: {type(exc).__name__}: {exc}"
                    ) from exc
                if attempt < 2:
                    await asyncio.sleep(1.0)
                    continue
        if results is None:
            raise SearchProviderError(
                f"duckduckgo rate-limit retry exhausted after 3 attempts: "
                f"{type(last_exc).__name__}: {last_exc}"
            ) from last_exc

        out: list[RawCandidate] = []
        for item in results:
            title = item.get("title")
            href = item.get("href")
            if not title or not href:
                continue
            out.append(
                RawCandidate(
                    title=title,
                    url=href,
                    snippet=item.get("body") or "",
                )
            )
        return out

    async def aclose(self) -> None:
        """Release the DDGS handle (no persistent connection to close)."""
        self._ddgs = None

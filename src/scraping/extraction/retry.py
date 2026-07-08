from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

from ..config import get_config

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def with_extraction_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Retry extraction with configured count and interval (D7).

    Extraction retries are independent from repair budget.
    """
    cfg = get_config()
    last_error: Exception | None = None

    for attempt in range(1 + cfg.extraction_retry_count):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < cfg.extraction_retry_count:
                logger.warning(
                    "Extraction attempt %d failed: %s. Retrying in %.1fs...",
                    attempt + 1,
                    e,
                    cfg.extraction_retry_interval,
                )
                await asyncio.sleep(cfg.extraction_retry_interval)
            else:
                logger.error(
                    "Extraction failed after %d attempts: %s",
                    attempt + 1,
                    e,
                )

    raise last_error  # type: ignore[misc]

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ScrapeFailed(Exception):
    """Terminal failure: scraper exhausted all its own means."""

    site: str
    url: str
    scraper_name: str
    failed_stage: str
    signature: tuple[str, str, str] = ("", "", "")
    snapshot: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"ScrapeFailed(site={self.site}, scraper={self.scraper_name}, "
            f"stage={self.failed_stage}, url={self.url})"
        )


class BrightDataInfraError(Exception):
    """Bright Data infrastructure failure (D21): no retry, immediate alert."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ColdStartInputError(ValueError):
    """Cold-start workbook does not satisfy the declared input contract."""

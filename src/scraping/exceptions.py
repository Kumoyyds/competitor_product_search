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
        detail = ""
        rule = self.signature[1] if len(self.signature) > 1 else ""
        if rule:
            detail += f", rule={rule}"
        if self.errors:
            detail += f", error={self.errors[0][:200]}"
        return (
            f"ScrapeFailed(site={self.site}, scraper={self.scraper_name}, "
            f"stage={self.failed_stage}, url={self.url}{detail})"
        )


def format_scrape_signature(
    site: str,
    signature: tuple[str, str, str] | None = None,
    fallback_rule: str = "unknown",
) -> str:
    """Compose ``{site}|{field_or_rule}|{parser_version}`` consistently."""
    field_or_rule = (
        signature[1]
        if signature and len(signature) > 1 and signature[1]
        else fallback_rule
    )
    parser_version = signature[2] if signature and len(signature) > 2 else ""
    return f"{site}|{field_or_rule}|{parser_version}"


class BrightDataInfraError(Exception):
    """Bright Data infrastructure failure (D21): no retry, immediate alert."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SandboxSpawnError(OSError):
    """The parser sandbox subprocess could not be started."""


class ColdStartInputError(ValueError):
    """Cold-start workbook does not satisfy the declared input contract."""

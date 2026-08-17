from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from src.scraping.models import ProductData
from src.search.models import CandidateEval, RawCandidate


def product_data(**overrides: Any) -> ProductData:
    values: dict[str, Any] = {
        "url": "https://example.test/product/1",
        "website": "example",
        "scraped_at": datetime.now(timezone.utc),
        "source_type": "html",
        "title": "Test Product",
        "price": Decimal("19.99"),
        "currency": "GBP",
        "in_stock": True,
        "image_urls": [],
    }
    values.update(overrides)
    return ProductData(**values)


def raw_candidate(**overrides: Any) -> RawCandidate:
    values = {
        "title": "Product",
        "url": "https://example.test/product/1",
        "snippet": "",
    }
    values.update(overrides)
    return RawCandidate(**values)


def candidate(**overrides: Any) -> CandidateEval:
    raw = overrides.pop("raw", None) or raw_candidate()
    return CandidateEval(raw=raw, **overrides)


def sku_workbook(tmp_path: Path, rows: list[dict[str, Any]], name: str = "input.xlsx") -> Path:
    path = tmp_path / name
    pd.DataFrame(rows).to_excel(path, index=False)
    return path

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_scraping_database(monkeypatch, tmp_path):
    """Keep future scraping pytest tests away from the developer runtime DB."""
    monkeypatch.setenv("SCRAPING_DB_PATH", str(tmp_path / "scraping.db"))

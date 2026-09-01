from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_databases(monkeypatch, tmp_path):
    """Keep pytest away from the developer scraping and orchestrator databases."""
    monkeypatch.setenv("SCRAPING_DB_PATH", str(tmp_path / "scraping.db"))
    monkeypatch.setenv("ORCHESTRATOR_DB_PATH", str(tmp_path / "orchestrator.db"))

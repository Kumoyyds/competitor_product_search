import pytest


@pytest.fixture(autouse=True)
def disable_implicit_search_db(monkeypatch):
    """Unit tests opt into persistence explicitly and never touch the cwd DB."""
    monkeypatch.setattr("src.search.pipeline.get_db", lambda: None)
    monkeypatch.setattr("src.search.batch.get_db", lambda: None)

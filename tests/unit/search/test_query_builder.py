from __future__ import annotations

import pytest

from src.search import config
from src.search.layers.query_builder import build_queries


def _configure(monkeypatch, *, modes=None, strip_parens=True, domains=None):
    data = {
        "search": {
            "query_mode": modes or {},
            "strip_parens": strip_parens,
        },
        "domain_map": domains or {"tesco": "tesco.com"},
    }
    monkeypatch.setattr(config, "load_config", lambda: data)


def test_keyword_mode_uses_website_key(monkeypatch):
    _configure(monkeypatch, modes={"serper": "keyword"})

    assert build_queries("Tea 80 Bags", "tesco", provider_name="serper") == [
        "Tea 80 Bags tesco"
    ]


def test_sitename_mode_uses_domain(monkeypatch):
    _configure(monkeypatch, modes={"duckduckgo": "sitename"})

    assert build_queries("Tea 80 Bags", "tesco", provider_name="duckduckgo") == [
        "Tea 80 Bags site:tesco.com"
    ]


def test_both_mode_has_stable_order(monkeypatch):
    _configure(monkeypatch, modes={"duckduckgo": "both"})

    assert build_queries("Tea 80 Bags", "tesco", provider_name="duckduckgo") == [
        "Tea 80 Bags tesco",
        "Tea 80 Bags site:tesco.com",
    ]


def test_strip_parens_adds_each_form_and_deduplicates(monkeypatch):
    _configure(monkeypatch, modes={"duckduckgo": "both"})

    assert build_queries(
        "Tea (New) 80 Bags", "tesco", provider_name="duckduckgo"
    ) == [
        "Tea (New) 80 Bags tesco",
        "Tea (New) 80 Bags site:tesco.com",
        "Tea 80 Bags tesco",
        "Tea 80 Bags site:tesco.com",
    ]
    assert build_queries("Tea 80 Bags", "tesco", provider_name="duckduckgo") == [
        "Tea 80 Bags tesco",
        "Tea 80 Bags site:tesco.com",
    ]


def test_strip_parens_can_be_disabled(monkeypatch):
    _configure(monkeypatch, modes={"duckduckgo": "both"}, strip_parens=False)

    assert build_queries(
        "Tea (New) 80 Bags", "tesco", provider_name="duckduckgo"
    ) == [
        "Tea (New) 80 Bags tesco",
        "Tea (New) 80 Bags site:tesco.com",
    ]


def test_unknown_provider_defaults_to_keyword(monkeypatch):
    _configure(monkeypatch)

    assert build_queries("Tea", "tesco", provider_name="new-engine") == ["Tea tesco"]


def test_sitename_without_domain_falls_back_to_keyword(monkeypatch):
    _configure(
        monkeypatch,
        modes={"duckduckgo": "sitename"},
        domains={"argos": "argos.co.uk"},
    )

    assert build_queries("Tea", "tesco", provider_name="duckduckgo") == ["Tea tesco"]


def test_invalid_mode_is_rejected(monkeypatch):
    _configure(monkeypatch, modes={"duckduckgo": "typo"})

    with pytest.raises(ValueError, match="invalid search.query_mode"):
        build_queries("Tea", "tesco", provider_name="duckduckgo")

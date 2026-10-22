from __future__ import annotations

import pytest

from src.scraping import router


def _register(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    monkeypatch.setattr(router, "HOST_TO_SITE", dict(mapping))


def test_exact_host_match_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, {"tesco.com": "bare", "www.tesco.com": "explicit"})
    assert router.resolve_site("https://www.tesco.com/shop/en-GB/products/123") == "explicit"
    assert router.resolve_site("https://tesco.com/x") == "bare"


def test_bare_domain_entry_covers_www_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bare-domain line is enough: www.onbuy.com falls back to onbuy.com."""
    _register(monkeypatch, {"onbuy.com": "onbuy"})
    assert router.resolve_site("https://www.onbuy.com/gb/p/x") == "onbuy"
    assert router.resolve_site("https://onbuy.com/gb/p/x") == "onbuy"


def test_www_entry_does_not_cover_bare_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is one-way: it never adds a www. prefix."""
    _register(monkeypatch, {"www.waitrose.com": "waitrose"})
    with pytest.raises(ValueError):
        router.resolve_site("https://waitrose.com/x")


def test_fallback_is_case_insensitive_and_ignores_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, {"onbuy.com": "onbuy"})
    assert router.resolve_site("https://WWW.OnBuy.COM/gb/p/x") == "onbuy"
    assert router.resolve_site("https://www.onbuy.com:443/gb/p/x") == "onbuy"


def test_other_subdomains_still_need_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a leading www. is stripped — this is not suffix matching."""
    _register(monkeypatch, {"onbuy.com": "onbuy"})
    with pytest.raises(ValueError):
        router.resolve_site("https://ie.onbuy.com/gb/p/x")


def test_www_strip_requires_a_remaining_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """www.com must not degrade to the bare TLD com."""
    _register(monkeypatch, {"com": "bogus"})
    with pytest.raises(ValueError):
        router.resolve_site("https://www.com/x")


def test_unknown_host_error_points_at_hosts_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    _register(monkeypatch, {"onbuy.com": "onbuy"})
    with pytest.raises(ValueError, match="hosts.yaml"):
        router.resolve_site("https://unknown-site.example.com/x")

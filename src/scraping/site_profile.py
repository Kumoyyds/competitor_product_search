"""Declarative per-site page-type constraints and cold-start policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import get_config

_SITES_YAML = Path(__file__).parent / "sites.yaml"


def _load_profiles() -> dict[str, dict[str, Any]]:
    """Load site profiles, normalizing site keys for stable lookup."""
    with open(_SITES_YAML, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {
        str(site).strip().lower(): profile
        for site, profile in raw.items()
        if isinstance(profile, dict)
    }


SITE_PROFILES: dict[str, dict[str, Any]] = _load_profiles()


def reload_profiles() -> None:
    """Reload profiles from disk (useful for tests and hot-reload)."""
    global SITE_PROFILES
    SITE_PROFILES = _load_profiles()


def _page_type_profile(site: str, page_type: str) -> dict[str, Any]:
    site_profile = SITE_PROFILES.get(site.strip().lower(), {})
    page_types = site_profile.get("page_types", {})
    if not isinstance(page_types, dict):
        return {}
    profile = page_types.get(page_type, {})
    return profile if isinstance(profile, dict) else {}


def page_type_available(site: str, page_type: str) -> bool:
    """Whether *page_type* can exist for *site*; undeclared values fail open."""
    return _page_type_profile(site, page_type).get("available", True) is not False


def is_mandatory_page_type(site: str, page_type: str) -> bool:
    """Return site-aware cold-start coverage policy for one page type."""
    if not page_type_available(site, page_type):
        return False
    profile = _page_type_profile(site, page_type)
    if "cold_start_required" in profile:
        return bool(profile["cold_start_required"])
    return get_config().is_mandatory_page_type(page_type)


def golden_min_for(site: str, page_type: str) -> int:
    """Minimum non-stale golden count required for this site/page type."""
    return 1 if is_mandatory_page_type(site, page_type) else 0


def membership_hints(site: str) -> set[str]:
    """Return declared membership-program hints for a site."""
    hints = _page_type_profile(site, "membership").get("hints", ())
    if isinstance(hints, str):
        hints = (hints,)
    return {str(hint).strip().lower() for hint in hints if str(hint).strip()}

from __future__ import annotations

import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from .. import config


_DEFAULT_STRIP_QUERY_PARAMS = [
    "srsltid",
    "gclid",
    "gbraid",
    "wbraid",
    "dclid",
    "fbclid",
    "msclkid",
    "ttclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "utm_*",
    "ref",
    "ref_",
]

_DEFAULT_PRODUCT_PATHS = {
    "tesco": r"/products/\d+",
    "argos": r"/product/\d+",
    "amazon": r"/(dp|gp/product)/[A-Z0-9]{10}",
    "amazon.co.uk": r"/(dp|gp/product)/[A-Z0-9]{10}",
    "amazon.nl": r"/(dp|gp/product)/[A-Z0-9]{10}",
}


def _strip_patterns() -> list[str]:
    configured = config.get(
        "url_rules", "strip_query_params", default=_DEFAULT_STRIP_QUERY_PARAMS
    )
    if not isinstance(configured, list):
        return _DEFAULT_STRIP_QUERY_PARAMS
    return [str(pattern).casefold() for pattern in configured]


def _is_tracking_param(name: str, patterns: list[str]) -> bool:
    normalized = name.casefold()
    return any(
        normalized.startswith(pattern[:-1]) if pattern.endswith("*")
        else normalized == pattern
        for pattern in patterns
    )


def clean_url(url: str) -> str:
    """Remove configured tracking parameters without rewriting other URL data."""
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return url

    if not parsed.query:
        # Reassembly also removes a bare trailing question mark.
        return urlunsplit(parsed)

    patterns = _strip_patterns()
    kept_parts: list[str] = []
    for part in parsed.query.split("&"):
        raw_name = part.partition("=")[0]
        try:
            name = unquote_plus(raw_name)
        except (UnicodeDecodeError, ValueError):
            name = raw_name
        if not _is_tracking_param(name, patterns):
            kept_parts.append(part)

    return urlunsplit(parsed._replace(query="&".join(kept_parts)))


def is_product_url(website: str, url: str) -> bool:
    """Return whether *url* matches the configured single-product path shape."""
    product_paths = config.get(
        "url_rules", "product_path", default=_DEFAULT_PRODUCT_PATHS
    )
    if not isinstance(product_paths, dict):
        product_paths = _DEFAULT_PRODUCT_PATHS

    pattern = product_paths.get(website.lower())
    if not pattern:
        return True

    try:
        path = urlsplit(url).path
        return re.search(str(pattern), path, re.IGNORECASE) is not None
    except (re.error, TypeError, ValueError):
        return False

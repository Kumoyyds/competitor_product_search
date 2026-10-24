"""Deterministic, bounded evidence from an unchanged raw HTML snapshot."""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from bs4 import BeautifulSoup

from .prepass import _extract_url_product_id, _resolve_site


@dataclass(frozen=True)
class Snippet:
    source: str
    path: str
    text: str
    position: int


def value_variants(value: Any) -> list[str]:
    """Return escaped numeric variants with complete numeric-token boundaries."""
    if value is None or isinstance(value, (bool, list, dict, tuple)):
        return []
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return []
    if not number.is_finite():
        return []
    plain = format(number, "f")
    variants = {plain, plain.replace(".", ",")}
    # Decimal scale is not evidence: 3.3 and 3.30 denote the same amount.
    canonical = format(number.normalize(), "f")
    variants.update((canonical, canonical.replace(".", ",")))
    try:
        cents = number == number.quantize(Decimal(".01"))
    except InvalidOperation:
        cents = False
    if cents:
        two = format(number, ".2f")
        grouped = format(number, ",.2f")
        variants.update((two, two.replace(".", ","), grouped,
                         grouped.translate(str.maketrans({",": ".", ".": ","}))))
    return [r"(?<![\d.,])" + re.escape(v) + r"(?![\d.,])"
            for v in sorted(variants, key=lambda s: (-len(s), s))]


def _pattern(value: Any) -> re.Pattern | None:
    variants = value_variants(value)
    if variants:
        return re.compile("|".join(variants))
    if isinstance(value, str) and value:
        return re.compile(re.escape(value), re.I)
    return None


def _display(raw: str) -> str:
    return html_lib.unescape(raw.replace(r'\"', '"'))


def _css(tag) -> str:
    chain = []
    while tag is not None and getattr(tag, "name", None) not in {None, "[document]"}:
        classes = ".".join(tag.get("class", [])[:3])
        chain.append(tag.name + ("." + classes if classes else ""))
        tag = tag.parent
    return " > ".join(reversed(chain))[-240:]


@lru_cache(maxsize=2)
def _index_html(raw: str):
    """Cache only shared snapshots, never a copy for each candidate parser."""
    soup = BeautifulSoup(raw, "lxml")
    blocks = []
    seen = set()
    for node in soup.find_all(string=True):
        if node.parent.name in {"script", "style", "noscript"}:
            continue
        # Follow nested inline wrappers until the first oversized ancestor.
        # Once one is too large, all outer ancestors exceed the same cap. A
        # seen ancestor also means its complete bounded chain was indexed.
        tag = node.parent
        while tag is not None and tag.name != "[document]":
            if id(tag) in seen:
                break
            seen.add(id(tag))
            outer = str(tag)
            if len(outer) > 600:
                break
            text = tag.get_text("", strip=True)
            position = raw.find(outer)
            if position < 0:
                position = raw.find(str(node))
            blocks.append((text, outer, _css(tag), max(0, position)))
            tag = tag.parent
    scripts = list(re.finditer(r"<script\b[^>]*>.*?</script\s*>", raw, re.I | re.S))
    return blocks, scripts


def locate_value_snippets(html: str, url: str, value: Any, *, keywords,
                          max_hits: int = 3) -> list[Snippet]:
    pattern = _pattern(value)
    if pattern is None or max_hits <= 0:
        return []
    blocks, scripts = _index_html(html)
    pid = _extract_url_product_id(url, _resolve_site(url))
    terms = [str(k).lower() for k in keywords]

    def rank(hit):
        near = html[max(0, hit.position - 2000):hit.position + 2000]
        return (not bool(pid and pid in near),
                not any(term in hit.text.lower() for term in terms), hit.position,
                hit.source != "DOM")

    # Keep only the best bounded set while scanning, even when a long state
    # script repeats the same amount thousands of times.
    hits: dict[str, Snippet] = {}

    def offer(hit):
        previous = hits.get(hit.text)
        if previous is None or rank(hit) < rank(previous):
            hits[hit.text] = hit
        if len(hits) > max_hits:
            worst = max(hits, key=lambda text: rank(hits[text]))
            del hits[worst]

    for text, outer, path, position in blocks:
        if pattern.search(text):
            offer(Snippet("DOM", path, _display(outer), position))
    # Raw lookup also covers attributes/meta, long DOM nodes, and escaped state.
    script_index = 0
    for match in pattern.finditer(html):
        position = match.start()
        while script_index < len(scripts) and scripts[script_index].end() <= position:
            script_index += 1
        in_script = (script_index < len(scripts) and scripts[script_index].start() <= position)
        source = "script" if in_script else "raw HTML / attribute"
        path = f"script[{script_index + 1}]" if in_script else f"offset {position}"
        start, end = max(0, position - 300), min(len(html), match.end() + 300)
        offer(Snippet(source, path, _display(html[start:end]), position))
    return sorted(hits.values(), key=rank)

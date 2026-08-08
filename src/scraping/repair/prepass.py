"""Price-aware context builder for the repair ladder and cold start.

Replaces raw HTML truncation with evidence-first budget allocation.  Three
independent evidence sources (DOM scan, meta description scan, JSON-LD
priceSpecification walk) are collected, anchored to the main product, and emitted
as a structured PriceContext — no DOM price subtree is truncated away.

Public API is a single function:

    build_price_aware_context(html: str, url: str, *, budget: int = 24000) -> PriceContext
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from ..site_profile import page_type_available

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BUDGET = 24000
_HEAD_BUDGET = 2000
_SNIPPET_MAX = 1500

# Currency amounts — primary signal, site-agnostic
_CURRENCY_RE = re.compile(
    r"""(?x)
    [£€$]\s?\d[\d,]*\.?\d*           # £19.99 style
    |
    \d[\d,]*\.\d{2}\s?(?:GBP|EUR|USD)  # 19.99 GBP style
    """
)

# Unit-price markers (routed to unit_price_evidence, not price_evidence)
_UNIT_PRICE_RE = re.compile(
    r"""(?ix)
    [£€$]\s?\d[\d,]*\.?\d*\s*/\s*(?:kg|litre|l|100\s*g|g|oz|lb|each|unit)
    |
    \d[\d,]*\.?\d*\s*(?:GBP|EUR|USD)\s*/\s*(?:kg|litre|l|100\s*g|g|oz|lb|each|unit)
    """
)

# Price-signal keywords — supplementary when a price is *not* matched by the
# currency regex but has an obvious price label.  Grown by data over time.
_PRICE_KEYWORD_SEED: set[str] = {
    "was", "rrp", "save", "off", "now", "regular price",
    "clubcard", "prime", "member", "loyalty",
    "会员", "原价", "现价", "优惠",
    "reduced", "clearance", "our price", "list price", "msrp", "uvp",
    "discount", "special price", "offer price", "promotion",
}

# Cross-sell / recommendation containers — deleted on double-hit
_CROSS_SELL_KEYWORDS: set[str] = {
    "recommend", "related", "similar", "carousel", "also-bought", "alsobought",
    "alternatives", "essential-extras", "essentialextras",
    "product-card", "productcard", "sponsored", "rail",
    "you-may-also", "customers-also", "frequently-bought", "compare-with",
    "cross-sell", "crosssell", "upsell", "bundle",
}

# Basket / guide-price decoy classes/tokens — zero amounts dropped
_ZERO_CLASS_TOKENS: set[str] = {
    "basket-guide-price", "guide-price", "price-value",
    "widget-price", "basket-price",
}
# data-auto / data-test values that indicate a basket/guide-price decoy
_ZERO_DATA_AUTO_TOKENS: set[str] = {
    "price-value", "guide-price", "basket-price", "basket-guide-price",
}

# JSON-LD paths to skip (shipping rates, not product prices)
_SKIP_JSONLD_PATHS: set[str] = {"shippingrate", "shipping_rate", "shippingdetails"}

# URL product-id extraction — per site; new sites add a line here
_URL_PID_PATTERNS: dict[str, str] = {
    "argos": r"/product/([A-Za-z0-9]+)",
    "amazon": r"/dp/([A-Z0-9]{10})",
    "tesco": r"/(?:shop|groceries)/[^/]+/products/(\d+)",
}

# Meta-tag name/property values to scan for price descriptions
_META_PRICE_NAMES: set[str] = {"description", "og:description", "twitter:description"}

# Regexes for meta free-text labelling
_META_REGULAR_PRICE_RE = re.compile(
    r"regular\s+price\s+(?:.?.?[£€$]?\s?)?(\d[\d,]*\.?\d*)", re.IGNORECASE
)
_META_MEMBER_PRICE_RE = re.compile(
    r"(?:clubcard|member|prime|loyalty|会员)\s+price\s+(?:is\s+)?(?:.?.?[£€$]?\s?)?(\d[\d,]*\.?\d*)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Promotion signal detector — seed lexicons (grown by data over time)
# ---------------------------------------------------------------------------

# Token hints for containers that wrap the main price presentation.
# These are CSS-class / data-attribute fragments — NOT site-specific strings.
# Every token below is observed across multiple retailers; new sites add their
# variants here as they are discovered.
_PROMOTION_CONTAINER_HINTS: set[str] = {
    "value-bar", "promotion", "price-block", "buybox", "pricing",
    "product-price", "price-container", "offer-price", "price__",
    "buy-box",  # hyphenated variant (Tesco data-auto="pdp-buy-box")
    "pdp-buy-box",  # explicit Tesco PDP buy-box marker
    "pdp-buybox",
}

# Loyalty points accrued by buying a product are not gated-price evidence, so
# programs used only for point collection (for example Nectar on Argos) do not
# belong in this generic gating lexicon.
# Token hints for membership / loyalty-program gating anywhere in the price
# container's class/id/data-* attributes or visible text.  A price gated by a
# named program is a `membership_price`, not a `list_price` discount.
# NOTE: these are *examples* — the structural check also inspects visible text
# for generic gating phrases ("only available with", "member price", etc.).
_MEMBERSHIP_GATING_HINTS: set[str] = {
    "clubcard", "member", "loyalty", "prime",
    "member-price", "membership-price", "loyalty-price",
    "clubcard-price", "clubcard-promotion", "member-only",
    "members-only", "sign-in-to-save", "logged-in-price",
    "会员", "vip-price", "reward-price",
}

# Token hints for a reference/struck price in the main container.  A
# higher struck/reference price with NO membership gating → discount.
_REFERENCE_PRICE_HINTS: set[str] = {
    "was", "rrp", "old", "before", "previous", "list", "reference",
    "original", "原价", "uvp", "prix-barré", "strike", "strikethrough",
    "line-through", "prev-price", "price-was", "price--was", "price-old",
    "compare-at", "compared-price",
}

# Visible-text gating phrases (site-agnostic patterns).  If any of these appear
# near a price in the main container it indicates membership gating.
_GATING_TEXT_MARKERS: list[str] = [
    "only available with",
    "member price",
    "members price",
    "membership price",
    "loyalty price",
    "sign in to save",
    "logged in price",
    "exclusive price",
    "会员价",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PriceEvidence:
    """One price candidate discovered by the pre-pass."""

    value: str
    currency: str
    raw_text: str
    label_text: str = ""
    css_hint: str = ""
    struck_through: bool = False
    source: str = "dom"  # "dom" | "meta" | "json_ld" | "dom_unit"
    source_path: str = ""  # dotted JSON-LD path / meta name / ""
    anchor_relation: str = "ambiguous"  # "inside_main" | "ambiguous"
    valid_for_member_tier: bool = False
    matches_canonical_title: bool = False
    snippet: str = ""  # ancestor subtree serialized, <= _SNIPPET_MAX chars


@dataclass
class PriceContext:
    """Structured context bundle fed to parser-gen prompts instead of raw HTML."""

    json_ld_blocks: list[str] = field(default_factory=list)
    price_evidence: list[PriceEvidence] = field(default_factory=list)
    unit_price_evidence: list[PriceEvidence] = field(default_factory=list)
    head_excerpt: str = ""
    main_excerpt: str = ""
    canonical_title: str = ""
    url_product_id: str | None = None
    h1_main_position: int | None = None
    promotion_signal: Optional[dict] = None  # set by _collect_promotion_signal


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_price_aware_context(
    html: str, url: str, *, budget: int = _DEFAULT_BUDGET
) -> PriceContext:
    """Build a PriceContext from raw HTML.

    Three evidence sources are collected, anchored to the main product, and
    cross-sell candidates are deleted before emission.  Budget is allocated
    evidence-first — no price subtree can be truncated away.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # Degrade gracefully — return minimal context if bs4 unavailable
        return _degraded_context(html, url)

    soup = BeautifulSoup(html, "lxml")

    # --- anchoring (run first — needed by evidence collectors) ---
    site = _resolve_site(url)
    url_product_id = _extract_url_product_id(url, site)
    canonical_title = _resolve_canonical_title(soup)
    h1_main = _find_main_h1(soup, canonical_title)
    h1_main_position = h1_main.sourceline if h1_main is not None else None

    ctx = PriceContext(
        canonical_title=canonical_title,
        url_product_id=url_product_id,
        h1_main_position=h1_main_position,
    )

    # --- evidence collection ---
    dom_evidence = _collect_dom_evidence(
        soup, ctx, site
    )
    meta_evidence = _collect_meta_evidence(soup)
    json_ld_blocks, jsonld_evidence = _collect_jsonld_evidence(html, url_product_id)

    all_evidence = dom_evidence + meta_evidence + jsonld_evidence

    # --- anchoring + cross-sell deletion ---
    _anchor_evidence(all_evidence, soup, ctx)

    # Split unit prices + dedup by (value, currency, source) to collapse
    # duplicate meta entries (description/og:description/twitter:description)
    seen_ev: set[tuple[str, str, str]] = set()
    price_evidence: list[PriceEvidence] = []
    unit_price_evidence: list[PriceEvidence] = []
    for ev in all_evidence:
        key = (ev.value, ev.currency, ev.source)
        if key in seen_ev:
            continue
        seen_ev.add(key)
        if ev.source == "dom_unit":
            unit_price_evidence.append(ev)
        else:
            price_evidence.append(ev)

    ctx.json_ld_blocks = json_ld_blocks
    ctx.price_evidence = price_evidence
    ctx.unit_price_evidence = unit_price_evidence

    # --- promotion signal (M15) ---
    # Pass JSON-LD-anchored price values so the container scorer can prefer
    # the buy-box that actually contains the real product price.
    trusted_vals: set[str] = {
        ev.value for ev in price_evidence if ev.anchor_relation == "inside_main"
    }
    ctx.promotion_signal = _collect_promotion_signal(soup, trusted_vals, site)

    # --- budget allocation ---
    ctx.head_excerpt, ctx.main_excerpt = _build_excerpts(
        soup, json_ld_blocks, price_evidence, budget
    )

    return ctx


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------


def _collect_dom_evidence(
    soup, ctx: PriceContext, site: str
) -> list[PriceEvidence]:
    """Scan DOM text nodes for currency amounts and price-related keywords.

    Excludes <script> and <style> subtrees so embedded JSON/JS is not ingested
    as phantom prices (see Gap 3 in evaluation).
    """
    from bs4 import NavigableString, Tag

    evidence: list[PriceEvidence] = []
    seen_values: set[tuple[str, str]] = set()  # dedup (value + source_path)

    # Walk every text node
    for text_node in soup.find_all(string=True):
        if not isinstance(text_node, NavigableString):
            continue
        # Exclude <script> / <style> content
        parent = getattr(text_node, "parent", None)
        if parent is not None and isinstance(parent, Tag):
            if parent.name in ("script", "style"):
                continue

        raw = text_node.strip()
        if not raw:
            continue

        matches = _CURRENCY_RE.findall(raw)
        is_unit = bool(_UNIT_PRICE_RE.search(raw))

        # Also check if this node or its ancestors carry a price keyword
        has_keyword = _has_price_keyword(text_node)

        if not matches and not has_keyword:
            continue

        # Determine struck-through
        struck = _ancestor_has_struck(text_node)

        # Build css_hint from ancestor classes / data-* attrs
        css_hint = _build_css_hint(text_node)

        # Extract label text (leading words before the amount)
        label = _extract_label(raw)

        # Build snippet (node + 1-2 parents)
        snippet = _build_snippet(text_node)

        for m in matches:
            # m is either the full match (regex group 0) or a tuple if there are groups
            value_text = m if isinstance(m, str) else (m[0] or m[1] if len(m) > 1 else str(m))
            currency = _guess_currency(value_text)
            numeric = _clean_numeric(value_text)

            if not numeric:
                continue

            # Emission filter: skip zero amounts from basket/guide-price decoys
            if _is_zero_decoy(text_node, numeric):
                continue

            dedup_key = (numeric, currency)
            if dedup_key in seen_values:
                continue
            seen_values.add(dedup_key)

            evidence.append(PriceEvidence(
                value=numeric,
                currency=currency,
                raw_text=raw[:200],
                label_text=label,
                css_hint=css_hint,
                struck_through=struck,
                source="dom_unit" if is_unit else "dom",
                source_path="",
                snippet=snippet,
            ))

    return evidence


def _collect_meta_evidence(soup) -> list[PriceEvidence]:
    """Scan meta description tags for currency amounts in free-text pricing.

    Required for fixtures like tesco_pc_membership where the regular price
    exists only in <meta name="description"> (and visible DOM).
    """
    evidence: list[PriceEvidence] = []

    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").lower()
        prop = (meta.get("property") or "").lower()
        if name not in _META_PRICE_NAMES and prop not in _META_PRICE_NAMES:
            continue

        content = meta.get("content", "")
        if not content:
            continue

        matches = _CURRENCY_RE.findall(content)
        if not matches:
            continue

        for m in matches:
            value_text = m if isinstance(m, str) else (m[0] or m[1] if len(m) > 1 else str(m))
            numeric = _clean_numeric(value_text)
            currency = _guess_currency(value_text)
            if not numeric:
                continue

            # Pre-tag label from surrounding text
            label = ""
            idx = content.find(value_text)
            if idx >= 0:
                surrounding = content[max(0, idx - 60):idx + len(value_text) + 40]
                if _META_REGULAR_PRICE_RE.search(surrounding):
                    label = "regular"
                elif _META_MEMBER_PRICE_RE.search(surrounding):
                    label = "member"

            source_path = f"meta:{name or prop}"
            evidence.append(PriceEvidence(
                value=numeric,
                currency=currency,
                raw_text=content[:200],
                label_text=label,
                source="meta",
                source_path=source_path,
                snippet=content[: _SNIPPET_MAX],
            ))

    return evidence


def _collect_jsonld_evidence(
    html: str, url_product_id: str | None
) -> tuple[list[str], list[PriceEvidence]]:
    """Extract verbatim JSON-LD blocks and walk for price/priceSpecification entries.

    Anchor: when the JSON-LD has multiple Offer nodes, prefer the one whose
    url/sku contains the url_product_id.  This prevents recommendation-product
    offers embedded inside schema.org @graph/ItemList from being surfaced as
    main-product prices (Gap 2 in evaluation).
    """
    blocks: list[str] = []
    evidence: list[PriceEvidence] = []

    raw_blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )

    for block_text in raw_blocks:
        stripped = block_text.strip()
        if not stripped:
            continue
        blocks.append(stripped)

        try:
            data = _json.loads(stripped)
        except (_json.JSONDecodeError, TypeError):
            continue

        # Walk the block for prices
        _walk_jsonld(data, "", url_product_id, evidence)

    return blocks, evidence


def _walk_jsonld(
    node, path: str, url_product_id: str | None, evidence: list[PriceEvidence]
) -> None:
    """Recursively walk a JSON-LD node for price/priceSpecification entries."""
    if isinstance(node, dict):
        # Check for priceSpecification with validForMemberTier
        price_spec = node.get("priceSpecification")
        if isinstance(price_spec, dict):
            spec_price = price_spec.get("price")
            is_member = (
                "validForMemberTier" in price_spec
                or "validForMemberTier" in node
            )
            if spec_price is not None and isinstance(spec_price, (int, float)):
                numeric = str(spec_price)
                ev = PriceEvidence(
                    value=numeric,
                    currency=node.get("priceCurrency", node.get("currency", "")),
                    raw_text=str(node.get("name", "")),
                    source="json_ld",
                    source_path=f"{path}.priceSpecification.price" if path else "priceSpecification.price",
                    valid_for_member_tier=is_member,
                    snippet=_json.dumps(node, default=str)[: _SNIPPET_MAX],
                )
                # Anchor: does this offer's url/sku match the product in the URL?
                _anchor_jsonld_evidence(ev, node, url_product_id)
                evidence.append(ev)

        # Check for direct price
        if "price" in node and not isinstance(node.get("price"), (dict, list)):
            raw_val = node["price"]
            if isinstance(raw_val, (int, float, str)):
                try:
                    numeric = str(raw_val).replace(",", "")
                    float(numeric)  # validate
                    # Don't duplicate if priceSpecification already covered this
                    is_member = "validForMemberTier" in node
                    ev = PriceEvidence(
                        value=numeric,
                        currency=node.get("priceCurrency", node.get("currency", "")),
                        raw_text=str(node.get("name", "")),
                        source="json_ld",
                        source_path=f"{path}.price" if path else "price",
                        valid_for_member_tier=is_member,
                        snippet=_json.dumps(node, default=str)[: _SNIPPET_MAX],
                    )
                    _anchor_jsonld_evidence(ev, node, url_product_id)
                    evidence.append(ev)
                except (ValueError, TypeError):
                    pass

        # Recurse into children
        for key, val in node.items():
            child_path = f"{path}.{key}" if path else key
            # Skip shipping rate subtrees
            if key.lower() in _SKIP_JSONLD_PATHS:
                continue
            _walk_jsonld(val, child_path, url_product_id, evidence)

    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_jsonld(item, f"{path}[{i}]" if path else f"[{i}]", url_product_id, evidence)


def _anchor_jsonld_evidence(
    ev: PriceEvidence, offer_node: dict, url_product_id: str | None
) -> None:
    """Anchor a JSON-LD price to the main product when possible."""
    if url_product_id is None:
        return

    # Check if the offer's url or sku contains the product id
    offer_url = str(offer_node.get("url", ""))
    offer_sku = str(offer_node.get("sku", ""))
    if url_product_id in offer_url or url_product_id in offer_sku:
        ev.anchor_relation = "inside_main"
        ev.matches_canonical_title = True


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def _resolve_site(url: str) -> str:
    """Best-effort site resolution from URL host."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    host_lower = host.lower()
    if "argos" in host_lower:
        return "argos"
    if "amazon" in host_lower:
        return "amazon"
    if "tesco" in host_lower:
        return "tesco"
    return host_lower.split(".")[-2] if "." in host_lower else host_lower


def _extract_url_product_id(url: str, site: str) -> str | None:
    """Extract the product id from the URL via site-specific regex."""
    pattern = _URL_PID_PATTERNS.get(site)
    if pattern is None:
        return None
    m = re.search(pattern, url)
    return m.group(1) if m else None


def _resolve_canonical_title(soup) -> str:
    """Page-level singleton title — content source, never <h1>.

    Priority: og:title → JSON-LD Product.name → <title> (strip site suffix).
    """
    # 1. og:title
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content", "").strip():
        return og_title["content"].strip()

    # 2. JSON-LD Product.name
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(script.string or "")
        except (_json.JSONDecodeError, TypeError):
            continue
        name = _find_product_name(data)
        if name:
            return name
        # Also check @graph
        if isinstance(data, dict) and "@graph" in data:
            for item in data["@graph"]:
                name = _find_product_name(item)
                if name:
                    return name

    # 3. <title> (strip site suffix after | or -)
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        title = title_tag.string.strip()
        # Strip site suffix: "Product Name | Site Name" → "Product Name"
        for sep in (" | ", " - ", " – "):
            if sep in title:
                title = title.split(sep)[0].strip()
                break
        return title

    return ""


def _find_product_name(data) -> str | None:
    """Extract Product.name from a JSON-LD node."""
    if not isinstance(data, dict):
        return None
    if data.get("@type") == "Product" or "Product" in str(data.get("@type", "")):
        name = data.get("name", "")
        if name and isinstance(name, str):
            return name.strip()
    return None


def _find_main_h1(soup, canonical_title: str):
    """Find the <h1> whose text bidirectionally-substring-matches the canonical title."""
    if not canonical_title:
        return soup.find("h1")  # fallback

    title_norm = _normalize_text(canonical_title)
    for h1 in soup.find_all("h1"):
        h1_norm = _normalize_text(h1.get_text())
        if not h1_norm:
            continue
        # Bidirectional substring match
        if h1_norm in title_norm or title_norm in h1_norm:
            return h1

    # Fallback: first <main> or first <h1>
    main = soup.find("main")
    if main is not None:
        return main
    return soup.find("h1")


def _anchor_evidence(
    evidence: list[PriceEvidence], soup, ctx: PriceContext
) -> None:
    """Assign anchor_relation to each evidence; delete cross_sell (double-hit).

    Anchoring uses four signals (all generic, no site-hard-coded strings):
      1. JSON-LD offers matching the URL product id → inside_main (already set
         by the JSON-LD walker).
      2. Cross-source value corroboration: a DOM/meta evidence whose numeric
         value equals a JSON-LD-anchored inside_main value → inside_main.
         "A visible price equal to the schema.org main-offer price is the
         main price."
      3. Primary price-container membership: evidence whose DOM node is a
         descendant of the main buy-box (located by _find_price_container) →
         inside_main.  This anchors both the regular and member price on
         membership pages (both live in the buy-box).
      4. Legacy positional check (_is_inside_main): inside h1 subtree or
         ancestor text contains the URL product id.

    We iterate a COPY of the list because we mutate (remove items).
    """
    # --- collect JSON-LD-anchored values for cross-source corroboration ---
    # Normalize to Decimal so "19.50" == "19.5" and "2" == "2.00".
    from decimal import Decimal, InvalidOperation

    def _norm(v: str) -> str:
        """Canonical numeric string for comparison (strips trailing zeros)."""
        try:
            d = Decimal(v).normalize()
            s = str(d)
            if "E" in s:
                # normalize() gave scientific notation (e.g. "2E+1" for 20)
                # — convert back to plain decimal
                s = format(Decimal(v), "f").rstrip("0").rstrip(".")
            return s
        except (InvalidOperation, ValueError, TypeError):
            return v

    jsonld_anchored_values: set[str] = set()
    for ev in evidence:
        if ev.anchor_relation == "inside_main":
            jsonld_anchored_values.add(_norm(ev.value))

    # --- locate the primary price container once (reused below) ---
    # Use ONLY JSON-LD-anchored values (not DOM — avoids circular anchoring
    # where a wrongly-selected container anchors delivery-fee DOM prices,
    # which then feed back into container scoring and lock in the wrong choice).
    main_container = _find_price_container(soup, jsonld_anchored_values)

    for ev in list(evidence):
        is_cross_sell = _is_cross_sell(ev, soup, ctx)
        if is_cross_sell:
            evidence.remove(ev)
            continue

        if ev.anchor_relation == "inside_main":
            # Already anchored by JSON-LD walker
            continue

        # --- signal 2: cross-source value corroboration ---
        if _norm(ev.value) in jsonld_anchored_values:
            ev.anchor_relation = "inside_main"
            continue

        # --- signal 3: inside the primary price container ---
        if main_container is not None:
            node = _find_evidence_node(ev, soup)
            if node is not None and _is_descendant_of(node, main_container):
                ev.anchor_relation = "inside_main"
                continue

        # --- signal 4: legacy positional check ---
        if _is_inside_main(ev, soup, ctx):
            ev.anchor_relation = "inside_main"
        else:
            ev.anchor_relation = "ambiguous"


def _is_cross_sell(ev: PriceEvidence, soup, ctx: PriceContext) -> bool:
    """Double-hit rule: container class/id matches cross-sell keywords AND
    nearest heading does NOT match canonical title."""
    # Find the evidence's node in the soup
    node = _find_evidence_node(ev, soup)
    if node is None:
        return False

    # Check ancestor classes/ids for cross-sell keywords
    cross_sell_hit = False
    for ancestor in node.parents:
        if ancestor.name is None:
            continue
        classes = " ".join(ancestor.get("class", [])) if hasattr(ancestor, "get") else ""
        container_id = (ancestor.get("id", "") or "") if hasattr(ancestor, "get") else ""
        combined = f"{classes} {container_id}".lower()
        for kw in _CROSS_SELL_KEYWORDS:
            if kw in combined:
                cross_sell_hit = True
                break
        if cross_sell_hit:
            break

    if not cross_sell_hit:
        return False

    # Second hit: nearest preceding heading must NOT match canonical title
    if not ctx.canonical_title:
        return True  # No title → can't verify → conservatively keep, but we're past first hit

    prev_heading = _find_previous_heading(node)
    if prev_heading is None:
        return True  # No nearby heading with cross-sell container → suspicious → delete

    heading_text = _normalize_text(prev_heading.get_text())
    title_norm = _normalize_text(ctx.canonical_title)
    if heading_text in title_norm or title_norm in heading_text:
        return False  # Heading matches canonical → this IS the main product despite container class
    return True  # Double-hit: cross-sell container + non-matching heading → DELETE


def _is_inside_main(ev: PriceEvidence, soup, ctx: PriceContext) -> bool:
    """Check if evidence node is inside the main product subtree."""
    if ctx.url_product_id is None and ctx.h1_main_position is None:
        return False

    node = _find_evidence_node(ev, soup)
    if node is None:
        return False

    # Check: is node inside the h1_main ancestor subtree?
    if ctx.h1_main_position is not None:
        h1 = _find_main_h1_from_position(soup, ctx.h1_main_position)
        if h1 is not None:
            for parent in node.parents:
                if parent is h1:
                    return True

    # Check: does the snippet or ancestor text contain url_product_id?
    if ctx.url_product_id:
        snippet_text = ev.snippet
        if ctx.url_product_id in snippet_text:
            return True
        for parent in node.parents:
            if parent.name is None:
                continue
            parent_text = parent.get_text()[:500]
            if ctx.url_product_id in parent_text:
                return True

    return False


def _find_main_h1_from_position(soup, position: int):
    """Re-find the h1 element at the given source line."""
    for h1 in soup.find_all("h1"):
        if h1.sourceline == position:
            return h1
    return None


def _find_evidence_node(ev: PriceEvidence, soup):
    """Try to find the DOM node that produced this evidence (best-effort)."""
    snippet = ev.snippet[:100]
    if not snippet:
        return None

    # Try to find by text content
    for tag in soup.find_all(string=lambda t: t and snippet[:40] in t):
        return tag
    return None


def _is_descendant_of(node, ancestor) -> bool:
    """Check whether *node* is a descendant of *ancestor* in the DOM tree."""
    for parent in node.parents:
        if parent is ancestor:
            return True
    return False


def _find_previous_heading(node):
    """Find the nearest preceding heading element (h1-h6)."""
    for prev in node.previous_elements:
        if hasattr(prev, "name") and prev.name in {f"h{i}" for i in range(1, 7)}:
            return prev
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_price_keyword(text_node) -> bool:
    """Check whether the text node or its ancestors carry a price-signal keyword."""
    text = text_node.strip().lower()
    for kw in _PRICE_KEYWORD_SEED:
        if kw.lower() in text:
            return True

    # Also check ancestor class/ids
    from bs4 import Tag

    for ancestor in text_node.parents:
        if not isinstance(ancestor, Tag):
            continue
        combined = " ".join(
            (ancestor.get("class") or []) + [ancestor.get("id") or ""]
            + [str(ancestor.get(f"data-{a}") or "") for a in ("test", "auto", "qa") if ancestor.get(f"data-{a}")]
        ).lower()
        for kw in _PRICE_KEYWORD_SEED:
            if kw.lower() in combined:
                return True
    return False


def _ancestor_has_struck(text_node) -> bool:
    """Check if an ancestor has a struck-through style or class."""
    from bs4 import Tag

    struck_classes = {"was", "strike", "strikethrough", "old", "line-through", "struckthrough",
                      "price-was", "price--was", "rrp", "original-price", "prev-price"}
    for ancestor in text_node.parents:
        if not isinstance(ancestor, Tag):
            continue
        # Check style attribute
        style = (ancestor.get("style") or "").lower()
        if "line-through" in style:
            return True
        # Check class names
        classes = " ".join(ancestor.get("class", []) if isinstance(ancestor.get("class"), list)
                           else [ancestor.get("class", "")] if ancestor.get("class") else []).lower()
        for sc in struck_classes:
            if sc in classes:
                return True
        # Check <s>, <del>, <strike> tags
        if ancestor.name in ("s", "del", "strike"):
            return True
    return False


def _build_css_hint(text_node) -> str:
    """Collect css class / data-* hints from ancestors."""
    from bs4 import Tag

    parts: list[str] = []
    for ancestor in text_node.parents:
        if not isinstance(ancestor, Tag):
            continue
        for attr in ("data-test", "data-auto", "data-qa", "class", "id"):
            val = ancestor.get(attr)
            if val:
                if isinstance(val, list):
                    parts.extend(str(v) for v in val)
                else:
                    parts.append(str(val))
        if len(parts) >= 10:
            break
    return " ".join(parts)[:200]


def _extract_label(raw: str) -> str:
    """Extract a label from text preceding the currency amount."""
    m = _CURRENCY_RE.search(raw)
    if m is None:
        return ""
    preceding = raw[: m.start()].strip()
    # Take last 1-3 words as potential label
    words = preceding.split()
    if not words:
        return ""
    label_words = words[-3:] if len(words) >= 3 else words
    label = " ".join(label_words).lower()
    # Filter garbage (mojibake single-char labels, pure unicode noise)
    if len(label) <= 2 and not label.isascii():
        return ""
    if all(ord(ch) > 127 or ch == " " for ch in label):
        return ""
    # Keep only if it looks like a price label
    price_label_terms = {"was", "rrp", "save", "now", "from", "only",
                         "regular", "clubcard", "prime", "member", "price", "offer"}
    for term in price_label_terms:
        if term in label:
            return label
    return label if len(label) <= 40 else ""


def _build_snippet(text_node) -> str:
    """Serialize the node + 1-2 parents as an HTML snippet, max _SNIPPET_MAX chars."""
    from bs4 import Tag

    parts: list[str] = [str(text_node).strip()[:500]]
    for ancestor in text_node.parents:
        if not isinstance(ancestor, Tag) or ancestor.name in ("[document]", "html", "body"):
            break
        # Serialize compactly
        tag_str = str(ancestor)[:800]
        parts.append(tag_str)
        if len(parts) >= 3:
            break
    combined = "\n".join(parts)
    if len(combined) > _SNIPPET_MAX:
        combined = combined[: _SNIPPET_MAX - 3] + "..."
    return combined


def _guess_currency(value_text: str) -> str:
    """Guess ISO-4217 currency code from symbol or suffix."""
    if "£" in value_text or "GBP" in value_text.upper():
        return "GBP"
    if "€" in value_text or "EUR" in value_text.upper():
        return "EUR"
    if "$" in value_text or "USD" in value_text.upper():
        return "USD"
    return ""


def _clean_numeric(value_text: str) -> str:
    """Extract a clean numeric string from a price text."""
    # Remove currency symbols and whitespace
    cleaned = re.sub(r"[£€$]", "", value_text).strip()
    cleaned = re.sub(r"\s*(GBP|EUR|USD)\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    # Remove thousands separators
    cleaned = cleaned.replace(",", "")
    # Validate
    try:
        float(cleaned)
        return cleaned
    except (ValueError, TypeError):
        return ""


def _is_zero_decoy(text_node, numeric: str) -> bool:
    """Check if this is a zero-amount basket/guide-price widget."""
    try:
        fv = float(numeric)
        if fv > 0.001:
            return False
    except ValueError:
        return False

    from bs4 import Tag

    # Check the parent element's full text for basket/guide-price labels
    parent = text_node.parent if hasattr(text_node, "parent") else None
    if isinstance(parent, Tag):
        parent_text = parent.get_text().lower()
        if "guide price" in parent_text or "basket" in parent_text:
            return True

    for ancestor in text_node.parents:
        if not isinstance(ancestor, Tag):
            continue
        classes = " ".join(ancestor.get("class", []) if isinstance(ancestor.get("class"), list) else []).lower()
        container_id = (ancestor.get("id") or "").lower()
        data_auto = (ancestor.get("data-auto") or "").lower()
        data_test = (ancestor.get("data-test") or "").lower()
        combined = f"{classes} {container_id} {data_auto} {data_test}"
        for token in _ZERO_CLASS_TOKENS:
            if token in combined:
                return True
        for token in _ZERO_DATA_AUTO_TOKENS:
            if token in data_auto or token in data_test:
                return True
    return False


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace."""
    return " ".join(text.lower().split())


# ---------------------------------------------------------------------------
# Promotion signal detection (structural, site-agnostic)
# ---------------------------------------------------------------------------


def detect_promotion(
    soup,
    trusted_values: set[str] | None = None,
    site: str | None = None,
) -> Optional[dict]:
    """Classify the main price container as discount, membership, or neither.

    Structural detection — no site-hard-coded strings.  The lexicons above
    supply *example* tokens grown by data; the mechanism is:
      - Find the primary price container (value-bar / buybox / pricing area).
        When *trusted_values* is given (JSON-LD main-offer prices), the scorer
        prefers the container that actually wraps those values — this avoids
        picking a delivery-fee box when the real buy-box is elsewhere on the
        page.
      - Check for membership gating: program-badge class, gating text phrases,
        or schema.org validForMemberTier corroborated by visible gating marker.
        An optional site profile can veto a page type known to be unavailable.
      - Check for reference-price signal: a struck/old/RRP price with a
        non-struck current price nearby.
      - Resolve: gating → membership; reference-only → discount; neither → None.

    Returns a small dict or None.  Callable standalone (no PriceContext needed).
    """
    # 1. Find the primary price container
    container = _find_price_container(soup, trusted_values)
    if container is None:
        return None

    container_text = container.get_text()
    container_classes = _element_class_tokens(container)

    # 2. Check for membership gating signals
    has_gating, gating_program = _check_membership_gating(
        container, container_text, container_classes
    )
    if has_gating and site and not page_type_available(site, "membership"):
        # Veto only the impossible membership interpretation. A real Was/RRP
        # discount in the same container must still be detected below.
        has_gating, gating_program = False, None

    # 3. Find all price amounts in the container
    prices_in_container = _find_prices_in_element(container)
    if not prices_in_container:
        return None

    # 4. Classify. A trusted main-offer value always maps to canonical `price`;
    # auxiliary amounts (instalments, gift cards, bundles) cannot displace it.
    numeric_prices = [(_price_decimal(p["value"]), p) for p in prices_in_container]
    numeric_prices = [(value, p) for value, p in numeric_prices if value is not None]
    numeric_prices.sort(key=lambda x: x[0])
    if not numeric_prices:
        return None

    trusted_decimals = {
        value
        for raw in (trusted_values or set())
        if (value := _price_decimal(raw)) is not None
    }
    detached_member_price: dict | None = None
    if trusted_decimals and (not site or page_type_available(site, "membership")):
        # Some retailers render the regular price and loyalty value-bar as
        # siblings. The trusted container then holds only `price`; consult the
        # detector's unanchored best container for the visibly gated lower
        # value and keep the trusted value as the regular-price anchor.
        gated_container = _find_price_container(soup)
        if gated_container is not None:
            gated_text = gated_container.get_text()
            gated_classes = _element_class_tokens(gated_container)
            detached_gating, detached_program = _check_membership_gating(
                gated_container, gated_text, gated_classes
            )
            if detached_gating:
                lower_gated = [
                    p
                    for p in _find_prices_in_element(gated_container)
                    if (value := _price_decimal(p["value"])) is not None
                    and value < max(trusted_decimals)
                ]
                if lower_gated:
                    detached_member_price = max(
                        lower_gated, key=lambda p: _price_decimal(p["value"])
                    )
                    has_gating = True
                    gating_program = detached_program
                    container_text = gated_text
    trusted_candidates = [
        (value, p) for value, p in numeric_prices if value in trusted_decimals
    ]
    non_reference_trusted = [
        (value, p)
        for value, p in trusted_candidates
        if not p.get("struck_through")
        and not _has_reference_label(p.get("label_text", ""))
    ]

    if non_reference_trusted:
        current_value, current_price = (
            max(non_reference_trusted, key=lambda item: item[0])
            if has_gating
            else min(non_reference_trusted, key=lambda item: item[0])
        )
        anchored = True
    elif trusted_candidates:
        current_value, current_price = trusted_candidates[0]
        anchored = True
    else:
        current_value, current_price = numeric_prices[0]
        anchored = False

    higher_candidates = [p for value, p in numeric_prices if value > current_value]
    ref_candidates = [
        p
        for p in higher_candidates
        if p.get("struck_through") or _has_reference_label(p.get("label_text", ""))
    ]

    # Resolve
    if has_gating:
        if anchored:
            lower_candidates = [
                p for value, p in numeric_prices if value < current_value
            ]
            gated_candidates = [
                p
                for p in lower_candidates
                if _has_membership_price_marker(p, gating_program)
            ]
            member_price = detached_member_price or (
                max(gated_candidates, key=lambda p: _price_decimal(p["value"]))
                if gated_candidates
                else None
            )
            return {
                "kind": "membership",
                "member_price": member_price["value"] if member_price else None,
                "regular_price": current_price["value"],
                "reference_price": None,
                "current_price": current_price["value"],
                "gated_by": gating_program,
                "evidence_text": container_text[:400].strip(),
            }

        # No trusted anchor: preserve the pre-M23 min/max fallback.
        fallback_current = numeric_prices[0][1]
        fallback_refs = [
            p for value, p in numeric_prices if value > numeric_prices[0][0]
        ]
        regular_price = fallback_refs[-1] if fallback_refs else None
        return {
            "kind": "membership",
            "member_price": fallback_current["value"],
            "regular_price": regular_price["value"] if regular_price else None,
            "reference_price": None,
            "current_price": fallback_current["value"],
            "gated_by": gating_program,
            "evidence_text": container_text[:400].strip(),
        }

    if ref_candidates:
        # Only an explicitly struck/Was/RRP higher price may become list_price.
        reference_price = max(
            ref_candidates, key=lambda p: _price_decimal(p["value"])
        )
        return {
            "kind": "discount",
            "reference_price": reference_price["value"],
            "current_price": current_price["value"],
            "member_price": None,
            "regular_price": None,
            "gated_by": None,
            "evidence_text": container_text[:400].strip(),
        }

    # No structural signal detected
    return {
        "kind": None,
        "reference_price": None,
        "current_price": current_price["value"],
        "member_price": None,
        "regular_price": None,
        "gated_by": None,
        "evidence_text": container_text[:400].strip(),
    }


def _has_membership_price_marker(price: dict, gating_program: str | None) -> bool:
    """Whether one price candidate itself carries visible gating evidence."""
    evidence = f"{price.get('label_text', '')} {price.get('css_hint', '')}".lower()
    if gating_program and gating_program in evidence:
        return True
    return any(hint in evidence for hint in _MEMBERSHIP_GATING_HINTS) or any(
        marker in evidence for marker in _GATING_TEXT_MARKERS
    )


def _price_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _find_price_container(soup, trusted_values: set[str] | None = None):
    """Find the primary price container in the DOM.

    Uses a scored heuristic (structural, no site-hard-coded strings):
      1. Find candidate containers matching price/promotion hints.
      2. Score each by:
         a. Contains a *price amount* matching a trusted value (numeric
            comparison, not substring — "2" won't match "2.25")
         b. Has buy-box-like data-auto attribute (pdp-buy-box etc.)
         c. Mid-sized text (20-2000 chars — the buy-box, not the whole page)
         d. Contains both a trusted current price and a labelled higher price
         e. Membership markers and DOM proximity to the h1
      3. Return the highest-scoring container.
    """
    from bs4 import Tag

    if trusted_values is None:
        trusted_values = set()

    # Normalize trusted values to Decimal for numeric comparison
    _trusted_dec: set[Decimal] = set()
    for tv in trusted_values:
        try:
            _trusted_dec.add(Decimal(tv))
        except (InvalidOperation, ValueError, TypeError):
            pass

    # Scope: prefer inside <main> or near the h1
    scope = soup.find("main") or soup.find("body") or soup

    # Locate the main h1 for proximity scoring
    h1_tag = soup.find("h1")

    # Walk candidates
    candidates: list[tuple[int, Tag]] = []
    for tag in scope.find_all(True):  # all tags
        if not isinstance(tag, Tag) or tag.name in ("script", "style", "noscript"):
            continue
        classes = _element_class_tokens(tag)
        if not classes:
            continue
        if any(hint in cls for cls in classes for hint in _PROMOTION_CONTAINER_HINTS):
            text_len = len(tag.get_text().strip())
            if text_len >= 8:  # minimum for a price label
                candidates.append((text_len, tag))

    if not candidates:
        return None

    # --- score each candidate ---
    scored: list[tuple[int, Tag]] = []  # (score, tag), higher is better
    for text_len, tag in candidates:
        score = 0

        # a. Contains a trusted price value (numeric comparison, not substring)
        if _trusted_dec:
            tag_prices = _find_prices_in_element(tag)
            tag_vals: set[Decimal] = set()
            for p in tag_prices:
                try:
                    tag_vals.add(Decimal(p["value"]))
                except (InvalidOperation, ValueError, TypeError):
                    pass
            if tag_vals & _trusted_dec:  # intersection → this container wraps a real price
                score += 100

        # b. Buy-box-like data-auto hints
        classes = _element_class_tokens(tag)
        buybox_hints = {"pdp-buy-box", "buybox", "buy-box", "product-price",
                        "price-block", "value-bar"}
        for cls in classes:
            for hint in buybox_hints:
                if hint in cls:
                    score += 50
                    break

        # c. Mid-sized bonus (20-2000 chars ideal for a buy-box)
        if 20 <= text_len <= 2000:
            score += 30

        # d. Contains at least one actual price amount (critical when no trusted
        #    values are available — prevents picking a logo/badge div that has
        #    promotion-hint classes but only icon text, no prices)
        tag_prices = _find_prices_in_element(tag)
        if tag_prices:
            score += 25

        # e. A trusted current price plus an explicitly struck/Was/RRP higher
        # price identifies the complete promotion wrapper, not a narrow child
        # containing only the offer price.
        if _trusted_dec and tag_prices:
            tag_values = [
                (value, p)
                for p in tag_prices
                if (value := _price_decimal(p["value"])) is not None
            ]
            present_values = {value for value, _ in tag_values}
            if any(
                trusted in present_values
                and any(
                    value > trusted
                    and (
                        p.get("struck_through")
                        or _has_reference_label(p.get("label_text", ""))
                    )
                    for value, p in tag_values
                )
                for trusted in _trusted_dec
            ):
                score += 40

        # f. Contains membership/loyalty gating keywords — a slight bonus so
        #    that a Clubcard/Prime value-bar wins over a regular buy-box when
        #    both contain prices but only one carries the membership signal.
        tag_text_lower = tag.get_text().lower()
        for hint in _MEMBERSHIP_GATING_HINTS:
            if hint in tag_text_lower:
                score += 10
                break

        # g. Proximity to h1 (closer = higher score)
        if h1_tag is not None:
            try:
                # Prefer containers that are siblings of or near the h1
                if _is_descendant_of(tag, h1_tag.parent) if h1_tag.parent else False:
                    score += 20
                # Share a common ancestor within ~5 levels
                for ancestor in tag.parents:
                    if ancestor is h1_tag.parent:
                        score += 15
                        break
            except Exception:
                pass

        scored.append((score, tag))

    # Sort by score descending, then prefer mid-sized among ties
    scored.sort(key=lambda x: (-x[0], -1 if 20 <= len(x[1].get_text().strip()) <= 2000 else 0))

    if scored and scored[0][0] > 0:
        return scored[0][1]

    # Fallback: return the first mid-sized container (original behaviour)
    mid = [(n, t) for n, t in candidates if 20 <= n <= 2000]
    if mid:
        return mid[0][1]
    return candidates[0][1]


def _check_membership_gating(
    container, container_text: str, container_classes: list[str],
) -> tuple[bool, Optional[str]]:
    """Check for membership/loyalty gating signals in a price container.

    Returns (has_gating, program_name_or_None).
    """
    container_text_lower = container_text.lower()

    # A. Check container class tokens for membership program hints.
    #    IMPORTANT: "non-<program>", "not-<program>", "no-<program>" prefixes
    #    are NEGATIVE signals — they explicitly mark a NON-member promotion.
    for cls in container_classes:
        for hint in _MEMBERSHIP_GATING_HINTS:
            if hint in cls:
                # Check for negation prefix on this specific class token
                if re.search(rf'\bnon[-_]?{re.escape(hint)}\b', cls, re.IGNORECASE):
                    return False, None  # explicitly NOT a member promotion
                if re.search(rf'\bnot[-_]?{re.escape(hint)}\b', cls, re.IGNORECASE):
                    return False, None
                if re.search(rf'\bno[-_]?{re.escape(hint)}\b', cls, re.IGNORECASE):
                    return False, None
                return True, hint

    # B. Check visible text for generic gating phrases
    for marker in _GATING_TEXT_MARKERS:
        if marker in container_text_lower:
            return True, marker

    # C. Check for a loyalty/member badge (SVG or icon with program name)
    from bs4 import Tag

    for child in container.find_all(True):
        if not isinstance(child, Tag):
            continue
        child_text = child.get_text().lower().strip()
        child_classes = _element_class_tokens(child)
        # Does this child look like a badge?
        badge_hints = {"badge", "logo", "icon", "label", "tag", "pill", "chip"}
        is_badge = any(h in cls for cls in child_classes for h in badge_hints)
        is_badge = is_badge or child.name in ("svg", "img")
        if not is_badge:
            # Also check if child's immediate parent carries a badge-like class
            parent = getattr(child, "parent", None)
            if isinstance(parent, Tag):
                parent_classes = _element_class_tokens(parent)
                is_badge = any(h in cls for cls in parent_classes for h in badge_hints)
        if is_badge:
            for hint in _MEMBERSHIP_GATING_HINTS:
                if hint in child_text or hint in " ".join(child_classes):
                    return True, hint

    return False, None


def _find_prices_in_element(element) -> list[dict]:
    """Find all currency amounts inside an element, with their struck status."""
    prices: list[dict] = []
    from bs4 import NavigableString, Tag

    for text_node in element.find_all(string=True):
        if not isinstance(text_node, NavigableString):
            continue
        parent = getattr(text_node, "parent", None)
        if isinstance(parent, Tag) and parent.name in ("script", "style"):
            continue

        raw = text_node.strip()
        if not raw:
            continue

        matches = _CURRENCY_RE.findall(raw)
        is_unit = bool(_UNIT_PRICE_RE.search(raw))
        if not matches or is_unit:
            continue

        struck = _ancestor_has_struck(text_node)
        label = _extract_label(raw)
        css_hint = _build_css_hint(text_node)

        for m in matches:
            value_text = m if isinstance(m, str) else (m[0] or m[1] if len(m) > 1 else str(m))
            numeric = _clean_numeric(value_text)
            if not numeric:
                continue
            try:
                fv = float(numeric)
                if fv < 0.001:
                    continue  # skip zero
            except ValueError:
                continue

            prices.append({
                "value": numeric,
                "currency": _guess_currency(value_text),
                "struck_through": struck,
                "label_text": label,
                "css_hint": css_hint,
            })

    # Deduplicate by value
    seen: set[str] = set()
    unique: list[dict] = []
    for p in prices:
        if p["value"] not in seen:
            seen.add(p["value"])
            unique.append(p)
    return unique


def _has_reference_label(label: str) -> bool:
    """Check whether a price label indicates a reference/comparison price."""
    if not label:
        return False
    label_lower = label.lower()
    for hint in _REFERENCE_PRICE_HINTS:
        if hint in label_lower:
            return True
    return False


def _element_class_tokens(element) -> list[str]:
    """Extract all class/id/data-* tokens from an element as lowercase strings."""
    from bs4 import Tag

    if not isinstance(element, Tag):
        return []
    tokens: list[str] = []
    for attr in ("class", "id", "data-auto", "data-test", "data-testid"):
        val = element.get(attr)
        if val:
            if isinstance(val, list):
                tokens.extend(v.lower() for v in val)
            else:
                tokens.append(str(val).lower())
    return tokens


def _collect_promotion_signal(
    soup,
    trusted_values: set[str] | None = None,
    site: str | None = None,
) -> Optional[dict]:
    """Bridge — calls detect_promotion; exists so the public API is clean."""
    return detect_promotion(soup, trusted_values, site)


# ---------------------------------------------------------------------------
# Budget allocation
# ---------------------------------------------------------------------------


def _build_excerpts(
    soup,
    json_ld_blocks: list[str],
    price_evidence: list[PriceEvidence],
    budget: int,
) -> tuple[str, str]:
    """Allocate budget: JSON-LD verbatim → price evidence snippets → head → main."""
    # JSON-LD (already separate in ctx.json_ld_blocks, not repeated in excerpts)
    jsonld_cost = sum(len(b) for b in json_ld_blocks)
    jsonld_cost = min(jsonld_cost, 8000)

    # Price evidence snippets
    evidence_text = ""
    for ev in price_evidence:
        line = (
            f"value={ev.value} {ev.currency} source={ev.source}"
            f" label={ev.label_text} css={ev.css_hint[:60]}"
            f" struck={ev.struck_through} member_tier={ev.valid_for_member_tier}"
            f" anchor={ev.anchor_relation}"
            f"\n  snippet: {ev.snippet[:300]}"
        )
        evidence_text += line + "\n"

    remaining = budget - jsonld_cost - len(evidence_text)
    if remaining < 2000:
        remaining = 2000

    # Head excerpt
    head_tag = soup.find("head")
    head_text = ""
    if head_tag:
        # Keep title + meta + canonical
        key_tags = head_tag.find_all(["title", "meta", "link"])
        for tag in key_tags:
            head_text += str(tag)[:300] + "\n"
        head_text = head_text[: _HEAD_BUDGET]

    remaining -= len(head_text)
    if remaining < 1000:
        remaining = 1000

    # Main excerpt — residual budget
    main_tag = soup.find("main") or soup.find("body")
    main_text = str(main_tag)[:remaining] if main_tag else ""

    return head_text, main_text


# ---------------------------------------------------------------------------
# Degraded fallback
# ---------------------------------------------------------------------------


def _degraded_context(html: str, url: str) -> PriceContext:
    """Minimal context when bs4 is unavailable."""
    # Still extract JSON-LD blocks
    json_ld_blocks, _ = _collect_jsonld_evidence(html, None)
    site = _resolve_site(url)
    url_product_id = _extract_url_product_id(url, site)
    return PriceContext(
        json_ld_blocks=json_ld_blocks,
        head_excerpt=html[:2000],
        main_excerpt=html[2000:24000],
        url_product_id=url_product_id,
    )

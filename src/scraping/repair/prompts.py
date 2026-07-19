"""Prompt templates for the repair agent (M8) and cold start (M11).

Each builder returns a list of chat messages in LangChain format:
    [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
"""

from __future__ import annotations

from typing import Any

SCHEMA_HINT = """\
Return a Python dict that will populate the ProductData model with these fields:

REQUIRED:
  title (str)
  in_stock (Python bool: True or False — NOT "true"/"false" strings, NOT 1/0 integers) —
      True iff a real customer purchase is currently possible
  image_urls (list of str) — may be empty list []; do not omit

OPTIONAL (return None or omit if unavailable):
  brand (str)
  gtin (str) — EAN/UPC barcode; do NOT put ASIN or SKU here
  variant (dict) — {"size": ..., "color": ..., "pack_qty": ...} keys as available
  price (Decimal-compatible string, e.g. "19.99") — REQUIRED by Gate 2 when in_stock is True (or list_price / membership_price — any one > 0 qualifies)
  currency (str) — ISO-4217 code like "GBP", "EUR", never a symbol like "£"
  list_price (Decimal-compatible string) — original/RRP price for discount detection
  membership_price (Decimal-compatible string) — member/loyalty offer price (e.g. Tesco Clubcard, Amazon Prime member price); the special price available only to loyalty-program members. Return None/omit if the page shows no member-specific price.
  unit_price (Decimal-compatible string) — e.g. "£/kg" numeric value
  unit (str) — e.g. "kg", "l"
  availability_raw (str) — original stock label from the page ("In stock", "Auf Lager", etc.)

CRITICAL:
  - in_stock MUST be a Python literal True or False (not a string, not None)
  - prices as strings ("19.99"), not floats, not with currency symbols
  - if in_stock is True, price MUST be present as a numeric string
  - do NOT return the tracing fields (url, website, scraped_at, source_type, parser_version) —
    the caller adds them
  - use bs4 (BeautifulSoup) and re; import only from: bs4, lxml, re, json

NEVER HALLUCINATE DEFAULTS (this catches real bugs, be strict):
  - If you cannot find a real product title, DO NOT default to the site
    name (e.g. "Argos", "Tesco"), `og:site_name`, or any global fallback.
    Return an empty dict `{}` from parse() to signal "not a product page".
  - If you cannot find a numeric price for an in-stock item, DO NOT return
    "0", "0.00", 0.0, or an empty string. Return None (or omit the key).
    Gate 2 rejects `in_stock=True + price <= 0` as a hallucinated default,
    so admitting the absence is strictly better than faking a zero.
  - Return `{}` ONLY when the URL path clearly indicates a non-product page —
    it contains `/browse/`, `/category/`, `/search`, or a category-code
    segment like `/c:<digits>`. For single-product URLs (e.g. `/product/<id>`,
    `/dp/<asin>`, `/shop/en-GB/products/<id>`), ALWAYS attempt extraction.
    When a specific field is genuinely missing on a product page, use None
    or omit the key — never return `{}` just because some fields are hard
    to find.
"""

_HTML_EXCERPT_LIMIT = 24000


def _excerpt(html: str, limit: int = _HTML_EXCERPT_LIMIT) -> str:
    """Build an excerpt that preserves structured data blocks.

    Strategy for large pages: include all JSON-LD script blocks first (they carry
    machine-readable product schema and are compact), then head + start of body.
    """
    if len(html) <= limit:
        return html

    # Extract JSON-LD blocks (SEO structured data, usually contains Product schema)
    import re
    jsonld_blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    jsonld_text = ""
    if jsonld_blocks:
        joined = "\n---\n".join(b.strip() for b in jsonld_blocks)
        jsonld_text = f"[JSON-LD blocks from page]\n{joined[:8000]}\n\n"

    remaining = limit - len(jsonld_text)
    if remaining < 4000:
        remaining = 4000

    # Prefer the head + first product-related chunk over generic footer
    head_end = html.find("</head>")
    if head_end > 0:
        head = html[:min(head_end + 8, remaining // 3)]
    else:
        head = html[:remaining // 3]

    body_start = remaining - len(head) - 200
    if body_start > 0:
        # Find first <main> or <div class*='product'> or fallback to after head
        body_kickoff = html.find("<main", head_end if head_end > 0 else 0)
        if body_kickoff < 0:
            body_kickoff = head_end + 1 if head_end > 0 else 0
        body_chunk = html[body_kickoff:body_kickoff + body_start]
    else:
        body_chunk = ""

    return (
        f"{jsonld_text}"
        f"[HEAD]\n{head}\n\n"
        f"[BODY EXCERPT]\n{body_chunk}\n\n"
        f"... (total page {len(html)} chars, showing structured data + head + partial body) ..."
    )


def no_product_prompt(html: str, site: str) -> list[dict[str, str]]:
    system = (
        "You are inspecting an HTML page and deciding whether it corresponds to a real "
        "purchasable product, or whether it is an error page, delisted product page, "
        "category page, homepage, captcha, or anti-bot wall. "
        "Return STRICT JSON with keys: decision (either 'product' or 'no_product'), "
        "phrase (string of a short characteristic snippet from the page if no_product, else null), "
        "reasoning (short string)."
    )
    user = (
        f"Site: {site}\n\nHTML EXCERPT (may be truncated):\n{_excerpt(html)}\n\n"
        "Respond with JSON only, no other text."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def source_absence_prompt(html: str, site: str, prior_errors: list[list[str]]) -> list[dict[str, str]]:
    system = (
        "You are inspecting an HTML page that a previous parser attempt failed to extract from. "
        "Decide: is this a REAL PRODUCT PAGE where the data exists but is hard to parse "
        "(decision='solvable'), OR does the page fundamentally lack the product data "
        "(anti-bot wall, incomplete render, price genuinely missing from page — decision='source_absent')? "
        "Return STRICT JSON: {decision: 'solvable'|'source_absent', reason: string}."
    )
    err_summary = "\n".join(f"Attempt {i}: {errs}" for i, errs in enumerate(prior_errors))
    user = (
        f"Site: {site}\n\nPRIOR ATTEMPTS' ERRORS:\n{err_summary}\n\n"
        f"HTML EXCERPT:\n{_excerpt(html)}\n\nRespond with JSON only."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


_ROLE_STRATEGY: dict[str, str] = {
    "first": (
        "STRATEGY (first attempt): Try the SIMPLEST viable approach. Modern "
        "e-commerce pages almost always embed a JSON-LD `Product` schema in a "
        "<script type=\"application/ld+json\"> block; parsing that with `json.loads` "
        "typically yields title, brand, gtin, image_urls, price, currency, and "
        "availability with minimal DOM traversal. Only fall back to BeautifulSoup "
        "selectors for fields not present in JSON-LD."
    ),
    "middle": (
        "STRATEGY (retry): The previous attempt failed. Read the CAPTURE SUMMARY "
        "and error traceback CAREFULLY and focus on fixing the SPECIFIC missing "
        "field(s) — do not rewrite the whole parser from scratch. Common causes: "
        "(a) selector returned None and you called `.text` on it "
        "(guard with `if node is not None`), (b) price string had a currency prefix "
        "or thousands separator (strip non-numeric chars before returning), "
        "(c) the JSON-LD block was structured as `@graph` with multiple entries "
        "(iterate and pick `@type == 'Product'`), (d) in_stock was set True but "
        "no price or list_price was returned (check both JSON-LD offers and DOM "
        "price blocks). If the current approach seems fundamentally wrong (e.g. "
        "JSON-LD has no Product schema but you kept parsing it), switch to DOM "
        "selectors. Some sites carry TWO price representations (e.g. Tesco "
        "encodes `offers.price` as the RRP and `offers.priceSpecification.price` "
        "as the current/discount price) — handle both, map current→price and RRP→list_price."
    ),
    "last": (
        "STRATEGY (LAST attempt): All prior attempts failed. You may have "
        "thinking/reasoning budget — USE IT. Slow down and think step by step: "
        "(1) inspect ALL prior capture summaries and tracebacks below; identify "
        "what consistent assumption made them fail. "
        "(2) inspect the HTML excerpt structure closely — are the fields nested "
        "in a way the earlier selectors kept missing? Is JSON-LD wrapped in "
        "`@graph`? Are there multiple `<script type=\"application/ld+json\">` "
        "blocks with different `@type` values (Product vs BreadcrumbList vs "
        "Corporation)? Iterate them and pick the Product one. "
        "(3) if the HTML is genuinely a product page but some fields are missing, "
        "DO NOT return `{}` — return what fields you CAN find, leaving optional "
        "fields as None. Gate 2 will accept out-of-stock products with images or "
        "list_price, and in-stock products with price OR list_price. "
        "(4) if this is genuinely NOT a product page (error, category, search), "
        "return `{}` explicitly."
    ),
}


def parser_gen_prompt(
    html: str,
    site: str,
    role: str,
    initial_errors: list[str],
    attempts: list[Any],
) -> list[dict[str, str]]:
    system = (
        "You are generating a Python parser function that extracts product data from HTML. "
        "STRICT RULES:\n"
        "  - Define exactly ONE top-level function: def parse(html: str, url: str) -> dict\n"
        "  - You may import ONLY: bs4, lxml, re, json (no os, subprocess, urllib, requests, etc.)\n"
        "  - Return a dict; missing optional values should be None (or key omitted)\n"
        "  - Prices as strings like '19.99'; do NOT import Decimal or datetime\n"
        "  - Handle missing/None DOM nodes defensively (check `if node is not None` before `.text`)\n"
        f"\n{SCHEMA_HINT}\n"
        "IMPORTANT: The HTML EXCERPT below may be TRUNCATED (large pages are shortened to "
        "fit the context). JSON-LD script blocks are extracted and shown first because they "
        "are the most stable machine-readable representation. PREFER JSON-LD-based extraction "
        "over DOM selectors when the data is available there — it's less likely to be affected "
        "by truncation, mojibake (some sites emit currency symbols that decode incorrectly), "
        "or SPA rendering order.\n\n"
        "Return STRICT JSON with a single key 'parser_code' containing the full function source as a string."
    )
    ctx_parts: list[str] = []
    strategy = _ROLE_STRATEGY.get(role)
    if strategy:
        ctx_parts.append(strategy)
    if initial_errors:
        ctx_parts.append(
            "PRE-REPAIR (existing parser list failed, not LLM candidates):\n"
            + "\n".join(f"  - {e}" for e in initial_errors)
        )
    if attempts:
        ctx_parts.append("PRIOR ATTEMPTS (read capture summaries and tracebacks carefully):")
        for rec in attempts:
            ctx_parts.append(_format_record(rec))

    user_parts: list[str] = [f"Site: {site}", f"Role: {role}"]
    if ctx_parts:
        user_parts.append("\n\n".join(ctx_parts))
    user_parts.append(f"\nHTML EXCERPT (may be truncated):\n{_excerpt(html)}")
    user_parts.append("\nRespond with JSON only: {\"parser_code\": \"...\"}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def _format_record(rec: Any) -> str:
    """Format one AttemptRecord for the repair prompt.

    Shows the candidate code, what it captured, what was missing, and why it
    failed.  For a long history we truncate the output dict to keep the prompt
    compact — the last attempt (formatted last) already carries the richest
    signal.
    """
    parts: list[str] = []
    parts.append(f"--- Attempt {rec.index} ({rec.model}) ---")
    if rec.failure_stage:
        parts.append(f"Failed at: {rec.failure_stage}")
    parts.append(f"Candidate code:\n{rec.code}")
    if rec.capture:
        parts.append(
            f"Captured: {rec.capture.get('captured', [])}\n"
            f"Missing required: {rec.capture.get('missing_required', [])}\n"
            f"Missing optional: {rec.capture.get('missing_optional', [])}"
        )
    if rec.errors:
        parts.append(f"Errors:\n" + "\n".join(f"  - {e}" for e in rec.errors))
    return "\n".join(parts)


def initial_parser_gen_prompt(html: str, site: str) -> list[dict[str, str]]:
    """Cold-start (M11): first parser generation, no error history yet."""
    return parser_gen_prompt(html, site, role="first", initial_errors=[], attempts=[])


def json_heal_precheck_prompt(
    json_data: dict[str, Any], missing_fields: list[str]
) -> list[dict[str, str]]:
    keys_summary = _summarize_json_keys(json_data)
    system = (
        "You are inspecting a JSON payload from a product API. Some fields the caller expected "
        "are missing from its output mapping. Decide whether the target data actually EXISTS "
        "in the JSON (just under a different key/path), or whether it is GENUINELY ABSENT "
        "(the API didn't return it). "
        "Return STRICT JSON: {decision: 'source_present'|'source_absent', reason: string}."
    )
    user = (
        f"MISSING FIELDS: {missing_fields}\n\n"
        f"JSON TOP-LEVEL KEYS (with nested key summary):\n{keys_summary}\n\n"
        "Answer only whether the missing data exists somewhere in the JSON. JSON only."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def json_heal_remap_prompt(
    json_data: dict[str, Any], missing_fields: list[str]
) -> list[dict[str, str]]:
    keys_summary = _summarize_json_keys(json_data)
    system = (
        "You are proposing a field remapping for a product API JSON payload. "
        "For each missing target field, provide a DOTTED PATH pointing to where that data "
        "already exists in the JSON. "
        "CRITICAL RULES:\n"
        "  - You MUST NOT fabricate values. Only reference dotted paths of keys that exist.\n"
        "  - If a target field's data is genuinely absent, OMIT it from your mapping.\n"
        "  - Dotted path syntax: 'buybox_prices.final_price', 'variations.0.price', etc.\n"
        f"\n{SCHEMA_HINT}\n"
        "Return STRICT JSON: {mapping: {target_field: dotted_path, ...}}"
    )
    user = (
        f"MISSING FIELDS TO REMAP: {missing_fields}\n\n"
        f"AVAILABLE JSON KEYS:\n{keys_summary}\n\n"
        "Respond with JSON only."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _summarize_json_keys(json_data: dict[str, Any], max_lines: int = 100) -> str:
    lines = []

    def _walk(obj: Any, prefix: str = "", depth: int = 0):
        if depth > 3 or len(lines) >= max_lines:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict, list)):
                    lines.append(f"{path}: {type(v).__name__}")
                    _walk(v, path, depth + 1)
                else:
                    sample = repr(v)[:60]
                    lines.append(f"{path} = {sample}")
        elif isinstance(obj, list) and obj:
            _walk(obj[0], prefix + ".0" if prefix else "0", depth + 1)

    _walk(json_data)
    return "\n".join(lines[:max_lines])

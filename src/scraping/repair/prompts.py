"""Prompt templates for the repair agent (M8) and cold start (M11).

Each builder returns a list of chat messages in LangChain format:
    [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
"""

from __future__ import annotations

from types import SimpleNamespace
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
  price (Decimal-compatible string, e.g. "19.99") — ordinary non-member current price; REQUIRED and positive when in_stock is True
  currency (str) — ISO-4217 code like "GBP", "EUR", never a symbol like "£"
  list_price (Decimal-compatible string) — higher Was/RRP/original reference price for a normal non-member discount; only set it when it is strictly greater than price
  membership_price (Decimal-compatible string) — lower price gated behind a named loyalty/membership program (e.g. Tesco Clubcard, Amazon Prime, Nectar, member/loyalty card). Only set this when the PAGE VISIBLY SHOWS membership gating — a program badge, \"Clubcard Price\" / \"Prime member price\" label, or \"only available with <program>\" text. It must be strictly lower than price. A plain \"Was £X Now £Y\" markdown is NOT membership even if JSON-LD tags the offer with a member tier — that is a normal discount (→ list_price + price).
  availability_raw (str) — a SHORT human-readable stock label ("In stock", "Out of stock", "Auf Lager", etc.).  NEVER return the raw JSON-LD script block or a long JSON string here — navigate to the `offers.availability` schema.org token (e.g. \"http://schema.org/InStock\") and map it to a short label, or derive from visible DOM text.

  PRICE FIELD CONTRACT:
  - standard product: price only; omit list_price and membership_price
  - normal discounted product: price + list_price, with list_price > price
  - membership product: price + membership_price, with membership_price < price; list_price is optional only when a separate higher Was/RRP is visibly shown
  - when all three prices occur: list_price > price > membership_price
  - coupon/promo-code discounts are normal discounts (→ price + list_price), not membership discounts

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
        "no ordinary `price` was returned (check JSON-LD offers, DOM price blocks, "
        "and member/loyalty price labels). If the current approach "
        "seems fundamentally wrong (e.g. JSON-LD has no Product schema but you kept "
        "parsing it), switch to DOM selectors."
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
        "fields as None. Gate 2 accepts out-of-stock products with product signals, "
        "but every in-stock product requires a positive ordinary `price`. "
        "(4) if this is genuinely NOT a product page (error, category, search), "
        "return `{}` explicitly."
    ),
}


def parser_gen_prompt(
    price_context,  # PriceContext from prepass.py
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
        "PRICE EXTRACTION RULES (structural, apply to ALL sites — no site-specific logic):\n"
        "- The VISIBLE price presentation on the page is authoritative.  JSON-LD "
        "corroborates but never overrides the visible DOM when they conflict.\n"
        "- `validForMemberTier` / `MemberProgramTier` in JSON-LD is a CORROBORATING HINT "
        "only — retailers sometimes stamp this on plain markdown discounts.  Use it ONLY "
        "when the visible page ALSO shows a membership gating signal (program badge, "
        "\"<program> price\" label, \"only available with <program>\" text).\n"
        "- **Plain discount** (no membership gating visible): a higher price shown as "
        "struck-through or labeled \"Was\" / \"RRP\" / \"original\" / \"原价\" → "
        "`list_price` (the higher one) and `price` (the current one).  Do NOT set "
        "`membership_price` for a plain markdown, even if JSON-LD tags the offer with "
        "a member tier.\n"
        "- **Membership-gated price**: a price visibly tied to a named loyalty/membership "
        "program (a program badge/logo, \"Clubcard Price\" / \"Prime member price\" / "
        "\"会员价\", or \"only available with <program>\") → the gated price goes to "
        "`membership_price` and the regular price to `price`.\n"
        "- **PRICE FIELD CONTRACT (mandatory)**: standard = `price` only; normal "
        "discount = `price` + higher `list_price`; membership = `price` + lower "
        "`membership_price` (+ optional higher `list_price`). Therefore, when both "
        "values are present, enforce `list_price > price > membership_price`. Never "
        "copy `price` into either other field.\n"
        "- Check the [PROMOTION SIGNAL] below — it classifies the main price container "
        "structurally.  When it says \"discount\" or \"membership\", use that as the "
        "primary signal.\n"
        "- When a page states TWO prices in free text as \"regular vs member/loyalty\" "
        "(in meta description, DOM text, or JSON-LD), capture BOTH — regular → `price`, "
        "member/gated → `membership_price`.  Do not drop one.\n"
        "- When all three price types co-occur (current + RRP + member), fill all three "
        "fields — they are not mutually exclusive.\n"
        "- Returning None for `list_price` / `membership_price` when a struck-through / "
        "labeled / member-gated price candidate is present in the evidence below is a "
        "RECALL FAILURE — the data is in your context, you must extract it.\n"
        "- Basket widgets showing \"Guide price £0.00\" (class guide-price / "
        "basket-guide-price / price-value) are NOT product prices — ignore them.\n"
        "- Unit prices (e.g. £/kg, /litre, /100g) are NOT product prices — ignore them "
        "entirely; never put them in `price`, `list_price`, or `membership_price`.\n"
        "- Coupon / promo-code discounts are normal discounts: put the payable "
        "non-member price in `price` and a separately displayed higher Was/RRP in "
        "`list_price`; never use `membership_price` unless membership gating is visible.\n\n"
        "ROBUST PRICE EXTRACTION (apply to ALL sites — handles dirty/mojibake/malformed HTML):\n"
        "- NEVER strip only the currency symbol (e.g. `text.replace('£','').strip()`). "
        "Stray characters from encoding errors (`Â`), non-breaking spaces (`\\xa0`), "
        "thousands separators, or unit suffixes (`/litre`) will corrupt the parse and "
        "raise Decimal conversion errors.\n"
        "- INSTEAD, locate the price DOM node first (via JSON-LD offer, buy-box selector, "
        "or anchored price container), then extract the numeric run from ITS TEXT ONLY "
        "with a tolerant regex: `m = re.search(r'\\d[\\d,]*\\.?\\d*', node_text)` → "
        "`m.group().replace(',', '')` → return as a string (e.g. `'19.99'`).\n"
        "- The regex is for CLEANING an already-located price node's text — NEVER use a "
        "bare `re.findall(r'[\\d.]+', whole_page)` which will grab pack sizes "
        "(\"12x330ml\"), ratings (\"4.5\"), years (\"48h\"), or delivery minimums.\n"
        "- After extracting the numeric run, validate: it must parse as a positive number "
        "and be > 0.0. Zero-amount basket/guide-price widgets (`£0.00` with class "
        "\"guide-price\" / \"basket-guide-price\" / \"price-value\") are NOT product "
        "prices — exclude them.\n\n"
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

    # Render PriceContext as structured evidence bundle
    user_parts.append(_render_price_context(price_context))
    user_parts.append("\nRespond with JSON only: {\"parser_code\": \"...\"}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def _render_price_context(ctx) -> str:
    """Render a PriceContext into a structured text bundle for the LLM prompt."""
    parts: list[str] = []

    # Canonical title + product id
    parts.append(f"CANONICAL TITLE: {ctx.canonical_title or '(not found)'}")
    if ctx.url_product_id:
        parts.append(f"URL PRODUCT ID: {ctx.url_product_id}")

    # Promotion signal (M15 — structural classification of the main price container)
    if ctx.promotion_signal and ctx.promotion_signal.get("kind"):
        ps = ctx.promotion_signal
        sig_lines = ["[PROMOTION SIGNAL]"]
        sig_lines.append(f"  kind: {ps['kind']}")
        if ps.get("current_price"):
            sig_lines.append(f"  current_price: {ps['current_price']}")
        if ps.get("reference_price"):
            sig_lines.append(f"  reference_price: {ps['reference_price']}"
                             f" (→ list_price if discount, or regular price if membership)")
        if ps.get("member_price"):
            sig_lines.append(f"  member_price: {ps['member_price']} (→ membership_price)")
        if ps.get("regular_price"):
            sig_lines.append(f"  regular_price: {ps['regular_price']} (→ price)")
        if ps.get("gated_by"):
            sig_lines.append(f"  gated_by: {ps['gated_by']}")
        if ps.get("evidence_text"):
            sig_lines.append(f"  evidence_text: {ps['evidence_text'][:300]}")
        parts.append("\n".join(sig_lines))

    # JSON-LD blocks (verbatim, compact)
    if ctx.json_ld_blocks:
        joined = "\n---\n".join(b[:4000] for b in ctx.json_ld_blocks)
        parts.append(f"[JSON-LD BLOCKS]\n{joined}")

    # Price evidence list
    if ctx.price_evidence:
        ev_lines: list[str] = ["[PRICE EVIDENCE (cross-sell removed)]"]
        for i, ev in enumerate(ctx.price_evidence):
            ev_lines.append(
                f"  [{i}] value={ev.value} currency={ev.currency} "
                f"source={ev.source} source_path={ev.source_path} "
                f"anchor={ev.anchor_relation} "
                f"label={ev.label_text or '-'} "
                f"struck_through={ev.struck_through} "
                f"valid_for_member_tier={ev.valid_for_member_tier} "
                f"css_hint={ev.css_hint[:80]}"
            )
            if ev.snippet:
                ev_lines.append(f"       snippet: {ev.snippet[:400]}")
        parts.append("\n".join(ev_lines))

    if ctx.unit_price_evidence:
        u_lines: list[str] = ["[UNIT PRICE EVIDENCE (not product prices)]"]
        for i, ev in enumerate(ctx.unit_price_evidence):
            u_lines.append(f"  [{i}] value={ev.value} {ev.currency} snippet: {ev.snippet[:200]}")
        parts.append("\n".join(u_lines))

    # Head excerpt
    if ctx.head_excerpt:
        parts.append(f"[HEAD EXCERPT]\n{ctx.head_excerpt[:2000]}")

    # Main excerpt
    if ctx.main_excerpt:
        parts.append(f"[MAIN EXCERPT (DOM fallback)]\n{ctx.main_excerpt[:8000]}")

    return "\n\n".join(parts)


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


def initial_parser_gen_prompt(price_context, site: str) -> list[dict[str, str]]:
    """Cold-start (M11): first parser generation, no error history yet."""
    return parser_gen_prompt(price_context, site, role="first", initial_errors=[], attempts=[])


def coldstart_repair_prompt(
    price_context,
    site: str,
    current_code: str,
    feedbacks: list[Any],
    failures: list[str],
    *,
    role: str,
    attempt_index: int,
    model: str,
    resolved_ledger: dict[str, list[str]],
    regressions: dict[str, list[str]],
) -> list[dict[str, str]]:
    """Build a cold-start repair prompt from code and in-memory review evidence.

    This deliberately reuses ``parser_gen_prompt`` and its retry strategies.  A
    lightweight record with the same attributes as ``AttemptRecord`` avoids a
    prompts -> agent import cycle while still using the shared formatter.
    """
    errors: list[str] = []

    if regressions:
        lines = ["【回退警告 — 上一轮修复弄坏了已通过的字段】"]
        for url, fields in sorted(regressions.items()):
            lines.append(f"- {url}: {', '.join(fields)}")
        errors.append("\n".join(lines))

    if feedbacks:
        lines = ["【提取错误，需修复】(本轮)"]
        for feedback in feedbacks:
            lines.append(f"URL: {feedback.url} ({feedback.page_type})")
            if feedback.corrections:
                for correction in feedback.corrections:
                    expected = correction.correct_value or "(人工未提供正确值)"
                    lines.append(f"- {correction.field}: 正确值应为 {expected!r}")
            else:
                lines.append("- 人工确认结果有误，但未指定字段")
            if feedback.hint:
                lines.append(f"人工提示：{feedback.hint}")
        errors.append("\n".join(lines))

    if failures:
        errors.append(
            "【Sandbox / Gate 失败，需修复】(本轮)\n"
            + "\n".join(f"- {failure}" for failure in failures)
        )

    if resolved_ledger:
        lines = ["【历史已修复，保持现状勿回退】"]
        for url, fields in sorted(resolved_ledger.items()):
            lines.append(f"- {url}: {', '.join(fields)}")
        errors.append("\n".join(lines))

    record = SimpleNamespace(
        index=attempt_index,
        model=model,
        code=current_code,
        output=None,
        capture=None,
        failure_stage="coldstart_review",
        errors=errors,
    )
    return parser_gen_prompt(
        price_context,
        site,
        role=role,
        initial_errors=[],
        attempts=[record],
    )


def json_heal_precheck_prompt(
    json_data: dict[str, Any], missing_fields: list[str]
) -> list[dict[str, str]]:
    keys_summary = _summarize_json_keys(json_data)
    system = (
        "You are inspecting a JSON payload from a product API. Some fields the caller expected "
        "are missing from its output mapping. Decide whether the target data actually EXISTS "
        "in the JSON (just under a different key/path), or whether it is GENUINELY ABSENT "
        "(the API didn't return it). "
        "A per-unit rate (£/kg, /litre) is not a product price — if the only price-like "
        "data in the JSON is a unit price, answer `source_absent`. "
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
        "  - Unit-price keys (`unit_price`, `price_per_unit`, `unit_cost`) and per-unit "
        "values like '668,26€ / kg' are NOT product prices. NEVER map them to `price`, "
        "`list_price`, or `membership_price` — omit them from the mapping.\n"
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

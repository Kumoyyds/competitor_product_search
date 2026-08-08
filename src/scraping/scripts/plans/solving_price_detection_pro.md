# Price-Extraction Repair-Agent Fix — Refined P0 Plan

## Context

The scraping module's HTML route generates a `def parse(html, url) -> dict` parser via a Qwen LLM **repair ladder** (`src/scraping/repair/`) when the existing parser list fails. The promoted parser is persisted in SQLite and reused on every future scrape of that site — so a bad repair becomes a permanent wrong-fast-path. On discount and membership pages the generated parser systematically **misses `list_price` and `membership_price`** and **confuses the three price types**.

A design doc (`src/scraping/scripts/plans/price_extraction_design.md`) diagnoses this and proposes 5 components. The user asked for an evaluation, validated against the 8 fixtures in `src/scraping/data/html_sample/`.

### Evaluation verdict

The doc is **directionally correct but Argos-centric and partly wrong for Tesco**.

- The doc's root-cause claim — *"list_price / membership_price are almost never in JSON-LD"* — is **true for Argos** (`argos_frame_discount.html`: `list_price` 58.00 exists only in DOM `data-test="price-was"`; JSON-LD has only current `price` 52.20) but **false for Tesco**:
  - `tesco_net_discount.html` (JSON-LD line 38): `offers.price` = 129.99 (regular) **and** `offers.priceSpecification.price` = 99.99 with `validForMemberTier → https://www.tesco.com/clubcard#clubcard`. The membership price **is in JSON-LD**.
  - `tesco_pc_membership.html`: `offers.price` = 2 (the Clubcard price; no `priceSpecification`); the regular price £2.25 exists **only in `<meta name="description">` free text** ("Regular price £2.25, Clubcard price is £2").
  - The two Tesco Clubcard fixtures use **mutually incompatible** JSON-LD shapes — a parser cannot assume one mapping for `offers.price`.
- The doc's two identified causes are confirmed in code: `_excerpt` (`prompts.py:64-110`) keeps JSON-LD + 24k of `<main>` and can truncate the DOM price subtree away; the `parser_gen_prompt` SYSTEM message (`prompts.py:206-211`) says *"PREFER JSON-LD-based extraction over DOM selectors"*, telling the model to stop once JSON-LD has `price`.

### Additional blockers the doc does NOT mention (found during exploration)

- **`agent.py:summarize_capture` (101-108)** omits `membership_price` from both its required-check and optional list → the repair feedback loop never credits/flags `membership_price`.
- **`json_healer.py:_extract_missing_fields` (140-143)** omits `membership_price` → API route never heals it.
- **`config.py:42`** default `qwen3.7-plus` (no hyphen) vs `qwen-3.7-plus` everywhere else → possible wrong model id if env not overridden.
- **`golden.py:classify_page_type`** has no `membership` bucket → Clubcard pages mis-classify as `standard`/`discounted`, weakening the golden regression signal.
- **Gate 2** (`gate2.py:_core_price_rule`) is deliberately lenient — *any one* of price/list_price/membership_price > 0 passes — so missing/misclassified prices slip through silently. Only an explicit recall check catches them (→ P1).
- The `middle` role strategy (`prompts.py:154-169`) **already** contains a Tesco two-price hint, but (a) `middle` never fires on the default 2-attempt ladder (roles go `first`→`last`), and (b) it mislabels the Clubcard price as "current→price", missing `membership_price` entirely.
- No verify script asserts `list_price`/`membership_price` values today.

### Decisions (from user)

- **Scope = P0 only** (pre-pass + anchoring + prompt rewrite + prerequisite fixes).
- **Add a `membership` golden bucket** (with the one-time SQLite migration).
- **Price-field convention: OPEN** — see "Open decision" at the end. The implementation below is convention-agnostic except where flagged.

---

## Refined P0 = doc's (component 1 + 1.5 + 5) + prerequisite fixes A–D + minimal membership classification in the prompt/pre-pass

The full extract-then-classify two-step split (doc component 2) is deferred to P2. P0 embeds only the minimal classification rules the LLM follows via the rewritten prompt + pre-pass signals (`validForMemberTier`, meta-description labels).

### 1. New module `src/scraping/repair/prepass.py` — price-aware context + anchoring (components 1 + 1.5)

Public API:
```python
@dataclass
class PriceEvidence:
    value: str; currency: str; raw_text: str; label_text: str; css_hint: str
    struck_through: bool
    source: str            # "dom" | "meta" | "json_ld" | "dom_unit"
    source_path: str       # dotted JSON-LD path, or meta name, or ""
    anchor_relation: str   # "inside_main" | "ambiguous"  (cross_sell deleted before emission)
    valid_for_member_tier: bool
    matches_canonical_title: bool
    snippet: str           # node + 1-2 parents, serialized, <=1500 chars

@dataclass
class PriceContext:
    json_ld_blocks: list[str]
    price_evidence: list[PriceEvidence]          # cross_sell removed; zeros/units excluded
    unit_price_evidence: list[PriceEvidence]      # £/kg etc., kept separate so they don't pollute price recall
    head_excerpt: str
    main_excerpt: str
    canonical_title: str
    url_product_id: str | None
    h1_main_position: int | None

def build_price_aware_context(html: str, url: str, *, budget: int = 24000) -> PriceContext: ...
```

**Three evidence sources (all required to cover the fixtures):**
- **DOM scan** (BeautifulSoup): for each text node matching `[£€$]\s?\d[\d,]*\.?\d*` or `\d[\d,]*\.?\d*\s?(GBP|EUR|USD)`, OR whose class/id/data-* contains a `PRICE_KEYWORD_SEED` term (`was, rrp, save, now, regular price, clubcard, prime, member, loyalty, 会员, 原价, …`): keep node + 1-2 parents as `snippet`; record `css_hint` (class/id/data-test/data-auto), `struck_through` (ancestor class `was|strike|old|line-through`), `label_text` (leading word(s) before the amount).
- **Meta scan** (required for `tesco_pc_membership`): run the currency regex on `<meta>` `content` for name/property ∈ {description, og:description, twitter:description}; emit one `PriceEvidence` per amount with `source="meta"`; pre-tag labels via regexes `Regular price £X` / `Clubcard price is £Y` / `(Prime|member) price £Z`.
- **JSON-LD `priceSpecification` walk** (required for `tesco_net_discount`): recursively walk each JSON-LD block for `price`/`priceSpecification`; emit one `PriceEvidence` per price with dotted `source_path` (e.g. `offers.price`, `offers.priceSpecification.price`); set `valid_for_member_tier=True` if a `validForMemberTier` key sits in that `priceSpecification`. **Do not classify here** — only flag the signal; the prompt decides the mapping.

**Anchoring (component 1.5):**
- **URL product ID**: regex per site — `/product/(\d+)` (Argos), `/dp/([A-Z0-9]{10})` (Amazon), `/shop/[^/]+/products/(\d+)` (Tesco).
- **Canonical title** (page-level singleton, content source — never `<h1>`): `og:title` → JSON-LD `Product.name` (walk `@graph`) → `<title>` (strip site suffix).
- **Main-subtree position**: the `<h1>` whose normalized text bidirectionally-substring-matches the canonical title → `h1_main_position` (fallback: first `<main>`).
- **Cross-sell delete (double-hit rule)**: for each evidence node, if an ancestor class/id matches a `CROSS_SELL_KEYWORDS` term (`recommend, related, similar, carousel, also-bought, alternatives, essential-extras, product-card, sponsored, rail, …`) **AND** the nearest preceding heading does NOT match the canonical title → `anchor_relation="cross_sell"` → **delete, do not emit**. Else if inside the h1_main subtree OR `url_product_id` appears in `snippet` OR nearby heading matches canonical → `inside_main`; else `ambiguous` (kept, LLM decides). JSON-LD-source evidence is always `inside_main` (page-level).
- **Emission filter (false-positive control; also serves P1's recall count)**: drop zero amounts from basket widgets (`basket-guide-price`/`price-value`/`guide-price`, value `0`/`0.00`); drop JSON-LD `shippingRate` paths; route unit prices (`£/kg`, `/litre`, `/100g`, …) to `unit_price_evidence`, not `price_evidence`.

**Budget:** all JSON-LD verbatim → all `price_evidence` + snippets → `head_excerpt` (title/meta/canonical, ~2k) → `main_excerpt` (residual `<main>`, filler).

**Reuse:** BeautifulSoup (already in `sandbox_import_whitelist`); `golden._normalize` for numeric comparison in tests.

### 2. Prompt rewrite (component 5) — `prompts.py:206-211`

Replace the *"PREFER JSON-LD-based extraction over DOM selectors"* paragraph in `parser_gen_prompt`'s SYSTEM message with conditional rules. The new text must:
- Keep JSON-LD for **identity** (title/brand/gtin/image_urls/availability) + canonical current price, BUT warn that on Tesco Clubcard PDPs `offers.price` may be the Clubcard price, not the regular price.
- State `priceSpecification.price` **WITH** `validForMemberTier` → `membership_price` (Tesco net_discount); **WITHOUT** → secondary current-price candidate.
- State the **meta-description fallback** "Regular price £X, Clubcard price is £Y" → `price`=X, `membership_price`=Y (Tesco pc_membership).
- State the **convention** (see Open decision): under Option A, `price`=non-member price, `membership_price`=Clubcard, `list_price`=separate RRP/'Was' for non-member discounts only; under Option B, `price`=current/displayed, `list_price`='Was'/RRP, `membership_price`=clubcard when a member tier is present. When all three co-occur, fill all three.
- State that returning `None` for list_price/membership_price when a struck-through / labeled / meta / `validForMemberTier` price is visible in the PRICE EVIDENCE list is a **recall failure**.
- State that basket `Guide price £0.00` and unit prices are NOT product prices (`unit_price` is a separate optional field).

`parser_gen_prompt` signature changes to accept a `PriceContext` instead of raw `html`; the user message renders the structured `[JSON-LD BLOCKS]` / `[PRICE EVIDENCE (cross-sell removed)]` / `[HEAD EXCERPT]` / `[MAIN EXCERPT]` bundle. `_excerpt` is **kept** for `no_product_prompt`/`source_absence_prompt` (they fire before parser-gen and need page shape, not price recall). `initial_parser_gen_prompt` (coldstart) also switches to `PriceContext`.

### 3. Wire the pre-pass into the repair ladder — `agent.py`, `coldstart.py`

- Add `price_context: PriceContext | None = None` to `RepairContext`; compute once in `_gen_parser` via `build_price_aware_context(ctx.html, ctx.url)` and cache.
- `coldstart.py:_gen_initial_parser` calls `build_price_aware_context(html, url)` before `initial_parser_gen_prompt`.

### 4. Prerequisite fixes (A–D)

- **A — `agent.py:summarize_capture` (101-108)**: add `membership_price` to the required-check (`if not (price or list_price or membership_price)`) and to the `optional` list. Preserve the existing `image_urls` field name (do **not** rename to `image_ids`).
- **B — `json_healer.py:_extract_missing_fields` (140-143)**: add `"membership_price"` to `key_fields`. (Gate 2 error strings already mention `membership_price`, so the substring match works.)
- **C — `config.py:42`**: change default to `["qwen-3.7-plus", "qwen-3.7-plus"]` to match the rest of the codebase. Verify the DashScope model id resolves with a one-line smoke test before merge.
- **D — `membership` golden bucket** (user-approved, with migration):
  - `golden.py:classify_page_type` (51-69): add `membership` bucket; precedence `out_of_stock > membership > discounted > multipack > standard`; detected by `membership_price is not None and membership_price > 0`. Add `"membership"` to the iteration tuple at `golden.py:116`.
  - `models/enums.py`: add `"membership"` to the `PageType` Literal.
  - `storage/database.py:22`: the `golden_samples.page_type` CHECK constraint must be recreated (SQLite can't ALTER a CHECK in place). Add a one-time migration script `storage/migrations/add_membership_bucket.py` (create `_new` with expanded CHECK, copy rows, drop, rename). Run once on deploy.
  - `tests/verify_m9.py`: add a membership-bucket case.

### 5. Verification — new `src/scraping/tests/verify_m14.py` (offline-first)

Three tiers, following the `verify_mN.py` convention (`[PASS]/[FAIL]`, `SUMMARY`, tee to `verify_m14_output.log`, update `tests/README.md`):

**Tier 1 — pre-pass + anchoring (offline, no Qwen, no DB)** — call `build_price_aware_context(html, url)` per fixture and assert:

| Fixture | Key assertions |
|---|---|
| `argos_frame_discount.html` | evidence has dom `value=58.00 css_hint=price-was struck=True anchor=inside_main`; cross-sell `alternatives-product-card-price` values are **deleted**; `url_product_id=3284476` |
| `tesco_net_discount.html` | evidence has json_ld `source_path=offers.priceSpecification.price value=99.99 valid_for_member_tier=True` AND `offers.price value=129.99`; no basket `0.00` |
| `tesco_pc_membership.html` | evidence has meta `value=2.25 label=regular` AND `value=2 label=clubcard` AND json_ld `offers.price value=2` |
| `tesco_pc_membership_bd.html` | identical to the above (clean-£ BD copy) |
| `tesco_cloth_normal.html` | json_ld `value=19.5`; no basket `0.00` |
| `tesco_snack_unavailable.html` | json_ld `value=1.25`; unit `12.50/kg` routed to `unit_price_evidence` |
| `argos_game_normal.html` | json_ld `value=69.99`; empty `<li></li>` produces no spurious evidence |

**Tier 2 — prompt rendering (offline)** — assert `parser_gen_prompt(ctx, …)` output contains the JSON-LD blocks, the PRICE EVIDENCE list with `anchor_relation`, and the new rules (`validForMemberTier`, `Regular price £X, Clubcard price is £Y`, `RECALL FAILURE`); does **not** contain the old `PREFER JSON-LD-based extraction over DOM selectors` string.

**Tier 3 — end-to-end parser-gen (gated on `QWEN_KEY`)** — single-attempt `_try_repair` per fixture; assert `ProductData` three-price values (compare via `golden._normalize` to tolerate `"2"` vs `Decimal("2.00")`). **Values below assume Option A — see Open decision.**

| Fixture | price | list_price | membership_price | in_stock | page_type |
|---|---|---|---|---|---|
| argos_frame_discount | 52.20 | 58.00 | None | True | discounted |
| argos_game_normal | 69.99 | None | None | True | standard |
| tesco_net_discount | 129.99 | None | 99.99 | True | membership |
| tesco_pc_membership | 2.25 | None | 2.00 | True | membership |
| tesco_pc_membership_bd | 2.25 | None | 2.00 | True | membership |
| tesco_cloth_normal | 19.50 | None | None | True | standard |
| tesco_snack_unavailable | 1.25 | None | None | False | out_of_stock |

Also extend `verify_m12.py`'s `PerURLReport` with a `membership_price` field (currently absent) for live-scrape visibility.

---

## Out of scope (later phases)

- **P1** — deterministic recall check + self-heal retry (doc component 3). Must include the false-positive exclusions proven by the fixtures (unit prices, basket `£0.00`, `Save X%`, JSON-LD `shippingRate`) — the doc's "dedup handles it" is insufficient.
- **P2** — extract-then-classify two-step split (doc component 2): move the P0 prompt rules into a deterministic classifier with LLM fallback for ambiguous cases.
- **P3** — multi-variant generation (doc component 4): per-site variant group drawn from the golden set (now including `membership`); unblocked once bug D lands.

---

## Open decision (must confirm before/as part of implementation)

**Price-field convention for Clubcard/member pages.** The pre-pass, anchoring, and prerequisite fixes A–D are identical under either choice — only the prompt rule wording and the Tier-3 expected `price`/`list_price`/`membership_price` columns change.

- **Option A (recommended; matches current SCHEMA_HINT verbatim — "price = the price for normal customer", "membership_price = Tesco Clubcard"):** `price` = non-member price, `membership_price` = Clubcard price, `list_price` = separate RRP/'Was' for non-member discounts only. For `tesco_net_discount`: price=129.99, membership_price=99.99, list_price=None. (Table above uses Option A.)
- **Option B (matches the existing but dead `middle`-role hint "map current→price, RRP→list_price"):** `price` = current/displayed, `list_price` = 'Was'/RRP, `membership_price` = clubcard when a member tier is present. For `tesco_net_discount`: price=99.99, list_price=129.99, membership_price=99.99.
- **A third reading the user raised:** treat `tesco_net_discount` as a *normal* (non-Clubcard) discount despite its `validForMemberTier`/`cc-promotion` signals — then price=99.99, list_price=129.99, membership_price=None (Argos-style). This would ignore the page's own Clubcard structured data, so it is not recommended unless there is domain knowledge that Tesco's `validForMemberTier` is unreliable here.

Recommendation: **Option A.** It is the only reading consistent across both Tesco Clubcard fixtures (their visible headline prices differ — net_discount shows £99.99 clubcard, pc_membership shows £2.25 regular — so "price = displayed" cannot be a consistent rule), and it matches the schema the user recently added `membership_price` to capture.

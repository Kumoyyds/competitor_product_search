# Scraping Module — Verification Artifacts

Each milestone's verification is persisted here so you can audit and re-run at any time.

## Milestone Overview

The scraping module was built incrementally across 23 milestones (M1–M23). Each milestone adds a self-contained capability, verified by a corresponding `verify_mN.py` script.

### M1 — ProductData Schema + Two Gates
Defines the canonical `ProductData` Pydantic model (url, website, title, price, currency, in_stock, image_urls, …) and the two-gate validation pipeline:
- **Gate 1**: Pydantic type/structure validation (price optional at this layer).
- **Gate 2**: `feasible_check` cross-field semantics (e.g. `in_stock=True` + `price=None` → fault, but `in_stock=False` + `price=None` → legal).

Also defines `InvalidTargetResult` — a separate sentinel for non-product pages (HTTP 404, error pages, etc.).

### M2 — BaseScraper + Router + Registry
Establishes the scraper class hierarchy and dispatch infrastructure:
- **`BaseScraper`** — ABC that all scrapers inherit from.
- **`@register_scraper`** decorator — registers a scraper class for a site with an explicit `order` priority.
- **Router** — two-hop dispatch: `url` → extract host → resolve site (via `hosts.yaml`) → ordered scraper list → try each scraper in order.
- **`hosts.yaml`** — maps hostnames to site names (e.g. `www.tesco.com` → `tesco`, `www.amazon.de` → `amazon`).

### M3 — SQLite 6 Tables
Persistent storage layer with 6 tables and corresponding store classes:
| Table | Store Class | Purpose |
|-------|-------------|---------|
| `parsers` | `ParserStore` | Generated parser code, version, status (active/retired) |
| `golden_samples` | `GoldenStore` | HTML + expected output pairs for parser validation |
| `scrape_runs` | `RunStore` | Execution log (URL, outcome, timing, winning parser), dedup window |
| `results` | `ResultStore` | Append-only `ProductData` history (time-series preserved per D24) |
| `escalations` | `EscalationStore` | Signature-deduped failure escalations |
| `invalid_target_phrases` | `PhraseStore` | Known error-page phrases per site for detection |

### M4 — DirectAPIScraper (Amazon + Tesco DCA)
API-based scraping route for sites with structured-data endpoints:
- **Amazon UK** via BrightData Datasets API — JSON response → `_map_fields()` extracts title, brand, GTIN (UPC), price, list_price, variant, images.
- **Tesco DCA** (backup) via BrightData DCA API — JSON payload → `_map_fields()` extracts product_name, current_price, original_price, stock status.
- **`_is_not_found()`** — per-scraper detection of missing/empty product data on the API side, routing to `InvalidTargetResult`.

### M5 — HTMLScraper + Invalid-Page Detection
HTML-based scraping route for sites requiring browser rendering (Tesco, Argos):
- **BrightData Web Unlocker** — fetches raw HTML from JavaScript-heavy product pages.
- **Invalid-page detection** with 5 signal layers (checked in order):
  1. JSON-LD `@type: Product` schema presence (positive signal).
  2. HTTP status codes (404, 410, 403, 451).
  3. Multi-absence: page long enough but missing 2+ product signals (title, price, cart button).
  4. Page length anomaly (< 5000 characters).
  5. Known invalid-target keyword/phrase match (per-site phrase list).

### M6 — Ordered Parser List + Run Recording
Parser selection and execution tracking for the HTML route:
- **Ordered parser list** — active parsers sorted by hit rate (real-time aggregation from `scrape_runs`, D17). Zero-hit tiebreak: newest parser first (id DESC).
- **First-passing-parser-wins** — tries parsers in order; first to pass both gates wins.
- **`winning_parser_id`** recorded to `scrape_runs` on success, `parser_version` stamped on `ProductData`.
- **Empty-list handling** — no active parsers → falls through to repair ladder (M8).

### M7 — Sandbox Runner
Safe execution environment for LLM-generated parser code:
- **Subprocess isolation** — parser runs in a separate Python process.
- **AST scan** (pre-execution) — rejects:
  - Forbidden imports: `os`, `subprocess`, `urllib`, `socket`, `requests`, etc.
  - Forbidden names: `open`, `eval`, `exec`, `__import__`.
  - Dunder attribute access: `__class__`, `__globals__`, `__mro__`, etc.
- **Timeout** — kills infinite loops (POSIX: `setrlimit`; Windows: subprocess timeout fallback).
- **Whitelisted imports** — `bs4`, `lxml`, `re`, `json` allowed.
- **Exception isolation** — runtime errors caught and reported as `SandboxException` with type name.

### M8 — Repair Agent + JSON Healer
LLM-driven self-repair when parsers fail or API data is malformed. Uses **real Qwen API**:
- **Repair ladder** (up to 4 attempts, shared budget):
  - **Turn A — no_product judgment**: LLM decides if HTML is a real product page. If not → backfill phrase to `invalid_target_phrases`, return `InvalidTargetResult`. Does NOT consume budget.
  - **Turn B — source_absence** (attempt 2 only): distinguishes "hard-to-parse product page" from "no data on page" (source_absent → terminal, skip attempt 3).
  - **Turn C — parser generation**: LLM produces `def parse(html, url) -> dict` → sandbox execute → gates → `promote_candidate()` → active parser inserted.
- **JSON healer** (DirectAPIScraper route): restricted JSON field remapping with **D25 red line** — may only remap to paths that already exist in the JSON payload. Never fabricates data. In-memory cache per scraper class.

### M9 — Golden Set + Promote/Prune
Parser quality control and lifecycle management:
- **`classify_page_type`** — 5 buckets: `standard`, `out_of_stock`, `discounted`, `multipack`, `membership` (in priority order: out_of_stock > membership > discounted > multipack > standard).
- **`maybe_seed_golden`** — first product of each `page_type` per site is seeded as a golden sample; subsequent same-type products are skipped.
- **`promote_candidate`** — LLM-generated parser must reproduce ALL goldens for its site → promoted to active; any mismatch → rejected.
- **`_prune_hard_cap`** — enforces `per_site_parser_limit` (default 4). When exceeded, retires the oldest lowest-hit parser.
- **`prune_stale`** — retires parsers with zero hits in the last `prune_sliding_window` (default 50) runs.

### M10 — Scraper-Level Fallback + Escalation
Failure handling across the scraper list and operational alerting:
- **Fallback loop** — when a scraper fails terminally (`ScrapeFailed`), the Router tries the next scraper in the ordered list. Escalates when all are exhausted.
- **Escalation reasons** with automatic `EscalationStore.upsert()`:
  - `parser_broken` — all HTML parsers exhausted for a site.
  - `api_malformed` — API scraper gate failures after heal attempts exhausted.
  - `infra_failure` — BrightData quota/network errors (`BrightDataInfraError`). **Bypasses fallback** (D21) — does not retry next scraper, raises immediately.
  - `mass_invalid_target` — triggered when invalid_target ratio exceeds `mass_invalid_target_ratio` (30%) AND absolute count exceeds `mass_invalid_target_absolute` (20) within 24h.
- **Signature dedup** — repeated identical failures increment `affected_count` on one row.
- **INFRA ALERT** — logged via `logger.error` when `BrightDataInfraError` occurs (Phase 1 hook for email/IM).

### M11 — Cold Start CLI
Bootstrap a new site with zero manual parser writing. Uses **real Qwen API**:
- **Interactive flow**: `python -m src.scraping.coldstart --site <site> --input <file.xlsx>` →
  1. Fetch all URLs via BrightData.
  2. Pick the largest 200-OK HTML as representative.
  3. LLM generates the first parser from that HTML.
  4. For each URL: run parser → display result → user confirms (y/n/q).
  5. Accepted URLs → seeded as golden samples; first parser inserted as `created_by=initial`.
- **This is the only manual step in the pipeline's lifetime** — after cold start, repair + promote/prune handle ongoing maintenance.

### M17 — Excel cold-start contract + golden-set caps

- Validates `page_type` / `url` Excel input, normalization, all-row errors, and mandatory coverage before extraction.
- Seeds the human-declared bucket, warns on classifier mismatch, and returns exit 2 for post-review coverage shortfalls.
- Applies one global per-page-type cap to cold start and runtime auto-seeding, with duplicate-URL protection.
- Adds `golden_samples.created_by` plus a dry-run-first prune command that evicts stale → oldest auto → oldest cold-start samples.

### M18 — Provider-aware LLM clients

- Centralizes model IDs, endpoints, key names, JSON-mode support, and thinking toggles in `providers.py`.
- Routes repair, JSON healing, and cold start through one client factory.
- Supports Qwen and the official DeepSeek V4 API, explicit `provider/model` names, dynamic dotenv keys, and unknown-model fallback.
- Sends each provider's registered output cap (`ProviderSpec.max_output_tokens`) as a body-level `max_tokens`. Without it DeepSeek applies its own 8192 default and truncates parser-generation replies mid-JSON; the OpenAI SDK then raises `LengthFinishReasonError` and discards the partial content. The cap cannot ride on `ChatOpenAI(max_tokens=...)` — langchain renames that to `max_completion_tokens`, which DeepSeek accepts and ignores (verified live: 12-token cap honored as `max_tokens`, ignored as `max_completion_tokens`).

### M21 — Human-terminated cold-start repair

- Decouples review/repair rounds from the model ladder; the final model and temperature repeat with thinking enabled until review succeeds or the human stops.
- Keeps repair prompts bounded to the immediately preceding round plus a compact resolved/regression ledger.
- Adds explicit continue, abandon, and partial-save outcomes, a configurable safety cap, and a two-consecutive-unusable-reply guard.

### M22 — Remove unit-price fields and guard API healing

- Removes `unit_price` / `unit` from `ProductData`, Amazon API mapping, parser/golden field lists, live-run reporting, and the v1.2 schema.
- Keeps the HTML pre-pass unit-price evidence filter, while changing parser guidance to ignore per-unit rates as product prices.
- Adds deterministic and prompt-level protection so JSON healing and cached mappings cannot feed unit-price keys or values into `price`, `list_price`, or `membership_price`.

### M23 — Argos runtime repair and site profiles

- Adds `sites.yaml` and fail-open site-aware page-type/cold-start policy.
- Prevents Argos Nectar point accrual from masquerading as membership pricing.
- Anchors promotion prices to the canonical offer and filters reference/member candidates structurally across all 16 reviewed Argos/Tesco golden snapshots.
- Reuses validated Argos parsers on the fast path, supports `tuc...` URL IDs, and gives DeepSeek thinking nodes a 65536-token output budget.
- `verify_m23.py` is fully offline and opens the project database read-only.

### M12 — End-to-End Live Scraping

Runs the full scraping pipeline against a per-site URL batch (`src/scraping/data/tesco_test.xlsx.xlsx` or `argos_test.xlsx.xlsx`, both 3-column `label / url / host`) using real BrightData and real Qwen. No mocking — this exercises the complete live pipeline end-to-end.

### M15 — Data-Quality Gate: promotion detection, availability normalization, fast-path distrust

Fixes three defects discovered in a live M12 re-run (buzzballz case: JSON-LD blob in `availability_raw`; Alivio/Peroni: discount vs membership price mis-mapped by a saved parser from a single-price page). The underlying root cause is that a promoted parser is reused on unseen page types with no output-quality check. Fixes:

- **Central `availability_raw` normalizer** (`models/product_data.py` — `@model_validator(mode="after")`) — recovers a schema.org availability token from anywhere inside a blob (`InStock → "In stock"`, `OutOfStock → "Out of stock"`, `PreOrder → "Pre-order"`, etc.) or derives from `in_stock`. A parser can never surface a blob again — this is a single choke point for both HTML and API routes. Site-agnostic (schema.org vocabulary is W3C-standard).
- **`detect_promotion(soup) → Optional[dict]`** (`repair/prepass.py`) — new reusable promotion-signal detector that classifies the main price container structurally (not hard-coded to any site's keywords). Discount = a struck/reference higher price with **no** membership-gating marker nearby. Membership = a price gated behind a named loyalty/membership program (badge, "Clubcard/Prime price", "only available with <program>", schema.org `validForMemberTier` corroborated by visible gating). Negation prefixes (`non-`, `not-`, `no-`) on membership-hint class tokens explicitly override. Detection reuses the existing BeautifulSoup parse; a growable lexicon of example tokens seeds each category.
- **Fast-path distrust guard** (`scrapers/html_scraper.py` — `_fast_path_sane()`) — after a reused parser passes both gates, the guard runs lightweight structural checks: (1) is `availability_raw` a JSON blob? (2) does the page carry a visible promotion signal (discount or membership) that the parser failed to capture (neither `list_price` nor `membership_price` returned)? If suspicious → the parser is skipped and the repair ladder self-heals to a better `vN+1` parser. Uses the same `detect_promotion` detector (no duplicate parsing).
- **Gate 2 structural price rules** (`validation/gate2.py` — `_structural_price_rule()`) — route-agnostic: `list_price <= price` and `membership_price >= price` are faults. Catches duplicate and inverted Alivio/Peroni-style mappings regardless of whether the parser came from the fast path or repair.
- **Prompt rewrite** (`repair/prompts.py`) — demotes `validForMemberTier` from an imperative "→ `membership_price` — use it" to "a **corroborating hint only**; the visible price presentation is authoritative and overrides it." Renders a new `[PROMOTION SIGNAL]` section in the PriceContext bundle showing the structural classification. SCHEMA_HINT updated: `membership_price` now mentions "VISIBLY SHOWS membership gating", `availability_raw` warns "NEVER return the raw JSON-LD."

**Verification**: `verify_m15.py` — 44 offline checks (Tier 1: promotion signal detection on Alivio/Peroni/standard, including negation-proof; Tier 2: availability normalization including blob→label, token recovery, model_validator wiring; Tier 3: gate2 structural rules including list==price, member==price, valid discount/membership, feasible_check wiring; Tier 4: fast-path guard on blob availability / missed-promotion / correct-extraction / standard-page; Tier 5: prompt rendering — PROMOTION SIGNAL section, old "use it" text absent, new principles present).


### M16 — UTF-8 Mojibake Resilience + Anchoring/Promotion Hardening

Triggered by M12 live findings: Tesco items `[01] Dove` and `[06] Peroni` fell through the HTML route to the DCA API backup after 400+ seconds each (Argos HTML succeeded on the same pipeline). Root cause: `BrightDataUnlocker.fetch()` returned `resp.text`, letting httpx auto-guess the charset. For Tesco, httpx guesses Latin-1, so UTF-8 `£` becomes `Â£`. LLM-generated parsers commonly do `text.replace('£','')` → `Decimal(...)`; on `'Â£20.00'` the spurious `Â` survives → parser crashes. Secondary root cause: the price-aware pre-pass (M14) never anchored the main visible DOM price, so every DOM price was `anchor=ambiguous`, and `detect_promotion._find_price_container` picked the wrong container (delivery-fee instead of buy-box).

Five coordinated fixes:

1. **UTF-8 decode at extraction** (`extraction/bright_data.py`): `enc = resp.charset_encoding or "utf-8"; html = resp.content.decode(enc, errors="replace")`. Honors explicit charset declarations (future sites), defaults to UTF-8 (every modern retailer). Single choke point.
2. **Fixture cleanup** (`data/html_sample/tesco_*.html`): targeted byte-level replacement of `Â£` → `£`. Zero mojibake byte sequences remain.
3. **Anchoring hardening** (`repair/prepass.py` `_anchor_evidence`): added cross-source value corroboration (DOM value matches JSON-LD-anchored inside_main value → inside_main) and primary price-container membership (descendant of `_find_price_container` → inside_main). Uses only JSON-LD-anchored values for container scoring to avoid circular anchoring.
4. **Promotion container scorer** (`repair/prepass.py` `_find_price_container`): 6-factor scored heuristic — trusted-value match (+100), buy-box hints (+50), mid-sized (+30), has-prices (+25), membership keywords (+10), h1 proximity (+20/+15). Uses Decimal-based numeric comparison (not substring). Added `buy-box`, `pdp-buy-box`, `pdp-buybox` to `_PROMOTION_CONTAINER_HINTS`. `detect_promotion(soup, trusted_values=None)` threads trusted values through.
5. **Robust-extraction prompt** (`repair/prompts.py`): added "ROBUST PRICE EXTRACTION" section — locate price via structure, then clean with regex (`re.search(r'\d[\d,]*\.?\d*', node_text)`), never `text.replace('£','')`. Explicit constraint: regex is for cleaning an already-located node, never for scanning the whole page.

**Verification**: Re-ran M14 (41/41 pass) and M15 (44/44 pass) against fixed fixtures. Pre-pass anchoring on Tesco samples: `alivio`/`net` discount — main DOM prices now `inside_main`, promo correct; `cloth_normal` — main DOM price `19.50` now `inside_main`, promo `cur=19.50` (delivery `£2.50` no longer chosen); `peroni` — DOM `13.00` `inside_main`, promo `kind=membership`, `mem=13.00`; `pc_membership` — DOM `2.00` `inside_main`, promo `kind=membership`.


### M14 — Price-Aware Pre-Pass + Membership Support

Fixes the systematic loss of `list_price` / `membership_price` in LLM-generated HTML parsers. The root cause was two-fold: (1) the raw-truncation budget (24k chars, JSON-LD-first) could drop the DOM subtree containing struck-through/member prices, and (2) the prompt instructed the model to "PREFER JSON-LD over DOM" — so it stopped once JSON-LD had a `price` value. The fix:

- **`repair/prepass.py`** — New price-aware context builder replacing character-count truncation. Three evidence sources (DOM currency-regex scan, meta description scan, schema.org `priceSpecification`/`validForMemberTier` walk) are collected, anchored to the main product via URL product ID + canonical title + h1 matching, and cross-sell/recommendation prices are hard-deleted (double-hit rule: recommendation container + heading ≠ canonical title). Basket £0.00 guide-price widgets, JSON-LD shipping rates, and unit prices are filtered. Budget is evidence-first: no DOM price subtree can be truncated.
- **Prompt rewrite** — Replaced the unconditional "PREFER JSON-LD over DOM" with site-agnostic, evidence-driven rules: `validForMemberTier` → `membership_price`; struck-through / "Was"/"RRP"-labeled → `list_price`; two prices in free text → capture both. Site names (Tesco Clubcard, Amazon Prime) appear only as `e.g.` examples. A RECALL FAILURE warning tells the model it must extract evidence already in its context.
- **`parser_gen_prompt` signature change** — Accepts `PriceContext` instead of raw `html`; user message renders structured `[JSON-LD BLOCKS]` / `[PRICE EVIDENCE]` / `[HEAD EXCERPT]` / `[MAIN EXCERPT]` bundle. `coldstart` wired to build `PriceContext` for initial parsers.
- **Membership golden bucket** — `classify_page_type` now has 5 buckets: `out_of_stock > membership > discounted > multipack > standard`. `PageType` Literal and `golden_samples` CHECK constraint updated.
- **Prerequisite fixes**: `summarize_capture` (agent.py) and `_extract_missing_fields` (json_healer.py) now include `membership_price`. Contradictory `middle`-role Tesco hint deleted from prompts.py.
- **API-route membership mapping** — `amazon_uk.py`, `tesco_dca.py` best-effort map `member_price`/`prime_price`/`clubcard_price` from BrightData payloads.

**Verification**: `verify_m14.py` — 41 offline checks (Tier 1: pre-pass + anchoring across all 8 fixtures; Tier 2: prompt rendering — old "PREFER JSON-LD" text absent, new rules present). Tier 3 (end-to-end parser-gen, gated on QWEN_KEY) is best-effort.

Key aspects:
- **Site-agnostic** — Currency regex (not keywords) is the primary recall signal, works for any marketplace. `validForMemberTier` is a standard schema.org property. Keyword seed tables grow by data over time.
- **Safe-by-default anchoring** — Cross-sell delete requires double-hit (recommendation container + non-matching heading). Unknown containers stay `ambiguous` (kept for LLM), never mis-deleted.
- **JSON-LD anchoring** — Offers whose URL/SKU match the URL product ID are marked `inside_main`; others stay `ambiguous`.
- **Anti-mojibake** — Meta regexes tolerate `Â£`/mojibake currency symbols. DOM scan excludes `<script>`/`<style>` to avoid ingesting embedded-JSON phantom prices.
- **No per-site hints in prompt** — Rules are evidence-driven principles, breaking the pattern that produced the dead `middle`-role Tesco text.
- **`verify_m12.PerURLReport`** extended with `membership_price` field, populated on success scrapes.

### M13 — Amazon/Tesco DCA Polling Fix

Fixes the duplicate-trigger bug: Amazon and Tesco DCA scrapers trigger only one BD snapshot per URL, even on poll timeout. The fix:
- Splits each async BD client's `fetch()` into `_trigger()` (retryable — a failed POST creates no snapshot) and `_poll()` (run **outside** `with_extraction_retry`, owning the full configurable budget).
- Moves polling constants (max seconds, interval) into `ScrapingConfig` as `bd_async_poll_max_seconds` (default 300s) and `bd_async_poll_interval_seconds` (default 4s).
- Polls immediately on first GET (no sleep before it) for faster retrieval when the snapshot is ready.
- Proves with offline mocked httpx.AsyncClient that a poll timeout does NOT cause a re-trigger — the core bug.

**Latest results** (July 2026):
| Batch | URLs | SUCCESS | Invalid Target | Infra Error | Escalated | Check |
|-------|------|---------|---------------|-------------|-----------|-------|
| `tesco_test.xlsx.xlsx` | 6 | 5 (83%) | 1 | 0 | 0 | ✓ 6/6 pass |
| `argos_test.xlsx.xlsx` | 8 | 6 (75%) | 2 | 0 | 0 | ✓ 6/6 pass |

The last-ditch attempt (attempt 1, `qwen-3.7-plus` with thinking mode) rescued URL 2 (McGregor lawnmower, 207s) — in prior runs without it, this URL had consistently escalated.

Key aspects:
- **Concurrent execution** — `asyncio.Semaphore(4)` runs up to 4 URLs in parallel; results stream in as they complete (via `asyncio.as_completed`) and the summary is sorted back to input order.
- **2-attempt repair ladder** — model sequence `[qwen-3.7-plus, qwen-3.7-plus]`; last attempt enables thinking mode (`extra_body.enable_thinking`).
- **Gate 2 strengthened**: rejects in-stock items with price ≤ 0 (LLM hallucinated zero), and OOS items with no product signals (image_urls empty + no price + no list_price — likely an error page, not a real product).
- **Upstream error retry**: BD responses < 1000 chars OR containing `"502 Bad Gateway"` / `"500 Internal Server Error"` etc. (1000-5000 byte gap) trigger extraction retry with 2s pause.
- **Temp DB** — uses a temporary SQLite database (`%TEMP%\verify_m12.db`) to avoid polluting production `scraping.db`. Cleaned up on process exit.
- **`.env` key aliasing** — `ScrapingConfig.bright_data_key` accepts `BRIGHT_UNLOCKER_KEY` as well as `BRIGHT_DATA_KEY` / `SCRAPING_BRIGHT_DATA_KEY` (see `config.py`), so no script-local shim is needed.
- **Per-URL detailed report** — each URL gets a formatted block showing: input label, resolved site, scraper chain, outcome, all extracted ProductData fields (title / price / list_price / brand / gtin / variant / availability_raw / image_urls / parser_version / source_type), latency, and mechanisms triggered.
- **Mechanism inference** — analyzes the `scrape_runs` DB path (`fast`, `agent_repaired`, `invalid_target`, `escalated`) to report exactly what happened.
- **TeeWriter with per-write flush** — output goes to stdout + log file simultaneously, flushed on every write for live progress.
- **Summary report** — breakdowns by label, site, DB path; mechanism tally; escalation listing; timing stats (avg, median, p95, min, max).

---

## Files

| File | Purpose | LLM |
|------|---------|-----|
| `verify_m1_m3.py` | M1 (ProductData + gates), M2 (Router + Registry), M3 (SQLite 6 tables) | offline |
| `verify_m1_m3_output.log` | Latest run — 33 checks, 0 failed | — |
| `verify_m4_m5.py` | M4 (DirectAPIScraper + field mapping), M5 (HTMLScraper + invalid page detection) | offline |
| `verify_m4_m5_output.log` | Latest run — 44 checks, 0 failed | — |
| `verify_m6.py` | M6 — parser list ordering by hit rate, tiebreak, winning_parser_id write, empty-list handling | offline |
| `verify_m6_output.log` | Latest run — 14 checks, 0 failed | — |
| `verify_m7.py` | M7 — sandbox AST scan (imports/names/dunder), timeout, exception isolation, whitelisted imports, Windows | offline |
| `verify_m7_output.log` | Latest run — 21 checks, 0 failed | — |
| `verify_m8.py` | M8 — repair agent no_product judgment, JSON healer D25 red line, end-to-end parser gen on real HTML | **real Qwen** |
| `verify_m8_output.log` | Latest run — 14 checks, 0 failed | — |
| `verify_m9.py` | M9 — page_type classification, promote_candidate (accept/reject), hard-cap prune, natural prune | offline |
| `verify_m9_output.log` | Latest run — 20 checks, 0 failed | — |
| `verify_m10.py` | M10 — escalation writing (parser_broken / api_malformed / infra_failure), signature dedup, mass_invalid_target thresholds, INFRA ALERT log | offline |
| `verify_m10_output.log` | Latest run — 14 checks, 0 failed | — |
| `verify_m11.py` | M11 — cold start CLI end-to-end: fetch → configured LLM gen → user review → seed parser + goldens | **real configured cold-start LLM** |
| `verify_m11_output.log` | Latest run — 15 checks, 0 failed | — |
| `verify_m12.py` | M12 — End-to-end live scraping (per-site batch from `tesco_test.xlsx.xlsx` / `argos_test.xlsx.xlsx`, concurrent, real BrightData + Qwen) | **real BrightData + Qwen** |
| `verify_m12_output.log` | Latest tesco run — 6 checks, 0 failed | — |
| `verify_m12_argo_output.log` | Latest argos run — 6/8 SUCCESS, 0 escalated | — |
| `verify_m13.py` | M13 — Amazon/Tesco DCA polling fix: no duplicate triggers, immediate-first-poll, configurable budget (offline, mocked) | offline |
| `verify_m13_output.log` | Latest run — 9 checks, 0 failed | — |
| `verify_m14.py` | M14 — Price-aware pre-pass + anchoring (8 fixtures), prompt rewrite (evidence-driven, de-Tesco-ified), membership golden bucket (5 types), API-route membership mapping | offline (Tier 1+2); real Qwen (Tier 3, best-effort) |
| `verify_m14_output.log` | Latest run — 41 checks, 0 failed (Tier 1+2) | — |
| `verify_m15_output.log` | Historical M15 run — 44 checks, 0 failed. The original script is absent; M23 re-covers promotion and fast-path behavior against current goldens. | — |
| `verify_m17.py` | M17 — Excel input contract, config policy, declared buckets, caps/URL dedup, provenance migration, dry-run/apply pruning | offline |
| `verify_m17_output.log` | Latest run — 39 checks, 0 failed | — |
| `verify_m17_live_output.log` | Bounded live smoke: round 1 exposed invalid BD token + hard-coded Qwen model; after fixes, round 2 used exactly 4 BD + 1 Qwen calls with zero retries — BD 4/4 HTTP 200, Qwen parser generated, 3/4 rows passed gates | **real BrightData + Qwen** |
| `verify_m18.py` | M18 — provider resolution, dynamic dotenv key lookup, unified client args, output-cap delivery, thinking toggles, and call-site model forwarding | offline |
| `verify_m18_output.log` | Latest run — 30 checks, 0 failed | — |
| `verify_m19.py` | M19 — cold-start all-pass gate, structured feedback repair loop, review reuse/panel, stale-golden control, and HTML snapshot reuse | offline |
| `verify_m19_output.log` | Latest run — 41 checks, 0 failed | — |
| `verify_m20.py` | M20 — canonical standard/discounted/membership price contract, Gate 2 ordering, prompt wording, and cold-start clear-value feedback | offline |
| `verify_m20_output.log` | Latest run — 15 checks, 0 failed | — |
| `verify_m21.py` | M21 — repeating cold-start final rung, sliding feedback/ledger, regression warnings, continue/quit/partial-save, and termination guards | offline |
| `verify_m21_output.log` | Latest run — 31 checks, 0 failed | — |
| `verify_m22.py` | M22 — ProductData/Amazon unit-price removal, JSON-heal contamination guard, cache guard, and prompt rules | offline |
| `verify_m22_output.log` | Latest run — 21 checks, 0 failed | — |
| `verify_m23.py` | M23 — 16-golden promotion alignment, active-parser fast path, site profiles, Argos IDs, cold-start input, and DeepSeek thinking cap | offline |
| `verify_m23_output.log` | Latest run — 90 checks, 0 failed | — |
| `verify_m23_live_output.log` | Argos + Tesco live smoke — both reused active parsers through the HTML fast path | **real BrightData** |
| `verify_clear_db.py` | `ScrapeDB.clear_site()` — FK-safe delete ordering, atomicity, idempotency, cross-site isolation, schema preservation, foreign_keys=OFF compat | offline |
| `verify_clear_db_output.log` | Latest run — 42 checks, 0 failed | — |

**Total: 520+ checks passed across all milestones. M23 adds 90 offline checks for Argos/Tesco promotion and fast-path correctness.**

## How to re-run

From repo root, with `.venv` activated:

```bash
python -m src.scraping.tests.verify_m1_m3 | tee src/scraping/tests/verify_m1_m3_output.log
python -m src.scraping.tests.verify_m4_m5 | tee src/scraping/tests/verify_m4_m5_output.log
python -m src.scraping.tests.verify_m6   | tee src/scraping/tests/verify_m6_output.log
python -m src.scraping.tests.verify_m7   | tee src/scraping/tests/verify_m7_output.log
python -m src.scraping.tests.verify_m8   | tee src/scraping/tests/verify_m8_output.log
python -m src.scraping.tests.verify_m9   | tee src/scraping/tests/verify_m9_output.log
python -m src.scraping.tests.verify_m10  | tee src/scraping/tests/verify_m10_output.log
python -m src.scraping.tests.verify_m11  | tee src/scraping.tests/verify_m11_output.log
python -m src.scraping.tests.verify_m12  | tee src/scraping/tests/verify_m12_output.log
python -m src.scraping.tests.verify_m13  | tee src/scraping/tests/verify_m13_output.log
python -m src.scraping.tests.verify_m14  | tee src/scraping/tests/verify_m14_output.log
python -m src.scraping.tests.verify_m17  | tee src/scraping/tests/verify_m17_output.log
python -m src.scraping.tests.verify_m18  | tee src/scraping/tests/verify_m18_output.log
python -m src.scraping.tests.verify_m19  | tee src/scraping/tests/verify_m19_output.log
python -m src.scraping.tests.verify_m20  | tee src/scraping/tests/verify_m20_output.log
python -m src.scraping.tests.verify_m21  | tee src/scraping/tests/verify_m21_output.log
python -m src.scraping.tests.verify_m22  | tee src/scraping/tests/verify_m22_output.log
python -m src.scraping.tests.verify_m23  | tee src/scraping/tests/verify_m23_output.log
python src/scraping/tests/verify_clear_db.py | tee src/scraping/tests/verify_clear_db_output.log
```

On Windows, prefix with `PYTHONIOENCODING=utf-8` (or use PowerShell's `$env:PYTHONIOENCODING="utf-8"`) so `->` and similar ASCII arrows don't crash cp1252.

## Milestone artifact summary

| Milestone | Focus | LLM | BD |
|-----------|-------|-----|----|
| M1–M3 | ProductData, gates, router, SQLite 6 tables | offline | offline |
| M4 | DirectAPIScraper (Amazon + Tesco DCA) field mapping | offline | offline |
| M5 | HTMLScraper + invalid-page detection (5 signals) | offline | offline |
| M6 | Ordered parser list + run recording | offline | offline |
| M7 | Sandbox AST scan, timeout, isolation | offline | offline |
| M8 | Repair agent + JSON healer (real Qwen) | **real** | offline |
| M9 | Golden set + promote/prune | offline | offline |
| M10 | Escalation writing, signature dedup, mass_invalid_target | offline | offline |
| M11 | Cold start CLI (configured cold-start provider) | **real** | offline |
| M12 | End-to-end live scraping | **real** | **real** |
| M13 | Amazon/Tesco DCA polling fix (no duplicate triggers) | offline | offline |
| M14 | Price-aware pre-pass + anchoring + prompt rewrite + membership golden bucket + API membership mapping | offline (Tier 1+2); real Qwen (Tier 3) | offline |
| M15 | Promotion detection + availability normalization + gate2 structural rules + fast-path distrust guard + prompt rewrite (visual-value-bar-first) | offline | offline |
| M16 | UTF-8 mojibake resilience (extraction choke point) + anchoring hardening (cross-source corroboration, buy-box container) + promotion container scored selection + robust-extraction prompt | offline (M14+M15 re-run on fixed fixtures) | offline |
| M17 | Validated Excel cold-start contract + capped/provenance-aware goldens | offline | offline |
| M18 | Provider-aware Qwen/DeepSeek client registry | offline | offline |
| M19 | Cold-start repair loop, all-pass gate, review panel/reuse, stale-golden control, HTML cache | offline | offline |
| M20 | Canonical price-field contract, strict ordering Gate 2, prompt and cold-start feedback normalization | offline | offline |
| M21 | Human-terminated cold-start repair, repeating final rung, bounded feedback ledger, partial save | offline | offline |
| M22 | ProductData unit-price removal, API JSON-heal and cache contamination guards | offline | offline |
| M23 | Argos runtime promotion repair, site profiles, anchored prices, thinking output cap | offline | offline |

**Total: 520+ checks passed across all milestones.**

## LLM-dependent tests

**verify_m8** hits real Qwen; **verify_m11** uses the provider selected by `cold_start_model_ladder` (DeepSeek by default). Requirements:
- The selected provider key (`QWEN_KEY` or `DEEPSEEK_KEY`) is set in `.env`.
- Small API cost per full run.
- **LLM output varies**: parser code generated on the same HTML can differ between runs. The verify scripts test *machinery* (ladder progresses, phrases backfill, D25 red line, coldstart seeds correctly), not exact parser code output. A rerun that produces a slightly worse parser may show more `[SKIP]` outputs but should not `[FAIL]`.

## Reading the log

- Section headers (`===...===`) mark which milestone/component is being checked
- `[PASS]` = expected result observed; the detail column shows the actual value
- `[FAIL]` = mismatch; detail shows expected vs actual
- `[SKIP]` = optional dependency missing (e.g., QWEN_KEY absent for M8/M11)

## What is NOT covered here

- Real BrightData network calls — exercised via `playground.ipynb` and `verify_m12.py`
- End-to-end scraping against live sites — covered by M12 (`verify_m12.py`)

## Convention

Any future milestone verification MUST add:
1. A `verify_mN.py` script here (offline preferred; real API only when strictly needed)
2. The captured `verify_mN_output.log` alongside it
3. An entry in this README's table

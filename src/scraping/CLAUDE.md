# Scraping Module

**Status**: M1–M28 complete. Full Phase 0 lifecycle. M28 adds exact run/result/escalation correlation and repaired-parser attribution. See `tests/unit/scraping/`.

## Responsibility

Extracts structured product data from marketplace pages. Takes `(url, website)` as input, returns `ProductData` (Pydantic model), `InvalidTargetResult` (not-a-product sentinel), or raises `ScrapeFailed` (terminal, all scrapers exhausted).

## Design Spec

Full spec: `scraping_module_spec_v1_2.md` (v1.2, 510 lines). Key decisions are numbered D1–D29 with rationale.

## Architecture

### Data Flow

`Router.scrape(url)` → host→site→ordered scraper list (two-hop) → try each scraper:
- **HTMLScraper route** (Tesco, Argos): BrightData Web Unlocker → HTML → invalid-target pre-detection → ordered parser list (sandbox-executed) → two gates → success → ProductData. On failure: **Agent repair ladder** (attempt count = `len(repair_model_ladder)`; config-driven via `config.py`) → candidate parser → sandbox + golden test → promote if passes.
- **DirectAPIScraper route** (Amazon, Tesco DCA backup): BrightData Datasets/DCA API → JSON → field mapping → two gates. On gate failure: **restricted JSON self-healing** (D25 red line — remaps existing keys only, never fabricates).

On terminal failure of a scraper: Router tries the next in the list; when exhausted → `EscalationStore.upsert(signature, reason, snapshot)` with reason ∈ `{parser_broken, api_malformed, infra_failure, mass_invalid_target}`.

### Class Hierarchy

```
BaseScraper (ABC)
  ├── HTMLScraper (Template Method with M6/M8/M9 hooks)
  │     ├── TescoScraper     (Web Unlocker, order=1)
  │     └── ArgosScraper     (Web Unlocker, order=1)
  └── DirectAPIScraper
        ├── AmazonUKScraper  (Datasets API, order=1)
        └── TescoDCAScraper  (DCA API, order=2, Tesco backup)
        └── ArgosDCAScraper  (DCA API, order=2, Argos backup)
```

### Two Gates (Public Checkpoint)

- Gate 1: Pydantic type/structure validation (`price` optional at this layer)
- Gate 2: `feasible_check` cross-field semantics:
  - `in_stock=True + price=None/<=0` → fault; `price` is always the ordinary non-member price
  - `list_price` may be present only when `list_price > price` (higher Was/RRP)
  - `membership_price` may be present only when `membership_price < price` (lower gated member price)
  - `in_stock=False + no_images + no_price + no_list_price + no_membership_price` → fault (likely an error/stub page, not a real out-of-stock product)

### Repair Ladder (§5.5)

Config-driven: attempt count = `len(cfg.repair_model_ladder)`. Each attempt registers a full `AttemptRecord` (code, capture summary, errors) fed back to the next attempt — no index misalignment, works for any node count. Default: `["deepseek-v4-flash", "deepseek-v4-flash"]` (2 attempts; previously 4, reduced in `fb68f14`).

With the default 2-node ladder, Turn B runs before attempt 1 only when attempt 0 produced a runnable parser, failed at the gates, and its capture summary shows missing required fields. Sandbox failures, golden failures, and gate failures involving only optional fields do not provide source-absence evidence, so they continue directly to parser generation. The logic is:

1. **Turn A — no_product judgment** (attempt 0 only): LLM decides if HTML is a real product page. If not, backfill phrase to `invalid_target_phrases` and return `InvalidTargetResult`. Does NOT consume budget.
2. **Turn B — source_absence** (attempt 1 only, evidence-gated): Distinguishes "hard-to-parse product page" (solvable) vs "no data on page" (source_absent → terminal). It may stop the last attempt before its expensive parser-generation call; LLM errors or a `solvable` verdict fail open to Turn C.
3. **Turn C — parser generation** (every attempt): LLM produces `def parse(html, url) -> dict` → sandbox → gates → `promote_candidate()` (golden test) → active parser row inserted.
4. **Last attempt**: thinking mode enabled (`reasoning_effort="high"`, `extra_body: {thinking: {type: enabled}}`). Strategy: `_ROLE_STRATEGY["last"]` (step-by-step, inspect all prior records).

**Convergence-quality signals fed into the ladder**:
- **Full sandbox tracebacks** propagated into next attempt (F1 fix).
- **AttemptRecord list**: candidate code + `summarize_capture()` output (which fields captured/missing) + errors, all aligned by index.
- **Temperature ramp** from config (`repair_temperature_ladder`), default `[0.1, 0.4]` (2 values matching 2-attempt ladder). Judgment prompts (Turn A/B) always stay at 0.1.
- **Thinking mode** enabled on the last attempt (len-1), only for Turn C.
- **Role-specific strategy hints** (3 semantic roles): `first` = "JSON-LD for identity, evidence-driven for prices", `middle` = "fix specific missing field from capture", `last` = "thinking, inspect all prior records".
- **Price-aware pre-pass (M14/M15)**: `build_price_aware_context(html, url)` replaces raw `_excerpt` for parser-gen prompts. Three evidence sources (DOM currency scan, meta description scan, schema.org JSON-LD `priceSpecification`/`validForMemberTier` walk) are collected, anchored to the main product (URL product ID + canonical title + h1 match), cross-sell prices hard-deleted (double-hit rule), and emitted as a structured `PriceContext` (including `promotion_signal` from M15). Prompt rules are evidence-driven (struck-through/labeled → `list_price`; visible membership gating → `membership_price`), site-agnostic, with a RECALL FAILURE warning. `validForMemberTier` is a corroborating hint only (demoted from imperative in M15). No DOM price subtree can be truncated away.
- **Promotion signal detector (M15)**: `detect_promotion(soup)` classifies the main price container structurally (not site-hard-coded): struck/reference higher price with no membership gating → discount; gated membership/loyalty program badge or text → membership. Exposed on `PriceContext.promotion_signal` and rendered as `[PROMOTION SIGNAL]` in the prompt. Used by the fast-path distrust guard.
- **Fast-path distrust guard (M15)**: After a reused parser passes both gates, `_fast_path_sane()` runs lightweight structural checks: (1) is `availability_raw` a JSON blob? (2) does the page carry a visible promotion signal the parser missed? If suspicious → the parser is skipped and the repair ladder self-heals to a better parser. Uses the same `detect_promotion` detector (no duplicate parsing).
- **`availability_raw` normalizer (M15)**: `ProductData.@model_validator(mode="after")` recovers a schema.org availability token from anywhere inside a blob or derives from `in_stock`. Single choke point for both HTML and API routes — a parser can never surface a blob.
- **Gate 2 structural price rules (M20)**: `_structural_price_rule()` enforces `list_price > price > membership_price` where the respective fields are present (route-agnostic). Prevents duplicate, inverted, and non-positive membership-price mappings from slipping past gates.
- **GoldenRejection**: `promote_candidate` returns which golden/field/expected/actual on mismatch, fed back into the next attempt's errors.

### BrightData extraction hardening (added after M12 findings)

`extraction/bright_data.py:_check_infra_error(status_code, body, headers, expect_html)`:
- **Header-authoritative**: reads `x-brd-error-code` (values include `min_size`, `reject_block`, `networkidle_event_timeout`, `bucket_rate_limit`) — BD's own signal that the fetch failed.
- **Body-length fallback** (only when `expect_html=True`): body < 1000 chars on an HTTP 200 → treated as infra flake (empty/stub response).
- **Soft-tolerance**: when Web Unlocker returned an error header BUT the body still has substantial HTML (≥ 1000 chars), the response flows through to `detect_invalid_page` (and Turn A as ultimate safety net) instead of failing hard. Logged at WARN. Applies only to `expect_html=True`; Datasets/DCA trigger endpoints (small JSON responses) always fail fast on any error header.

### BrightData extraction hardening (added after M12 findings)

`extraction/bright_data.py:_check_infra_error(status_code, body, headers, expect_html)`:
- **Infra status codes**: `{407, 429, 500, 502, 503, 504}` — all server-side transient errors; all trigger the retry + pause mechanism.
- **Header-authoritative**: reads `x-brd-error-code` (values include `min_size`, `reject_block`, `networkidle_event_timeout`, `bucket_rate_limit`) — BD's own signal that the fetch failed.
- **Body-length fallback** (only when `expect_html=True`): body < 1000 chars on an HTTP 200 → treated as infra flake.
- **Upstream error content markers**: when `expect_html=True`, status is 200, and body is under 5000 chars, check for well-known upstream error status-line text (`502 Bad Gateway`, `503 Service Unavailable`, `500 Internal Server Error`, `504 Gateway Timeout`, nginx `Gateway Time-out`). If found → raise — these are transient upstream failures that BD retried as transparent proxies; our extraction retry will pause and re-request.
- **Soft-tolerance**: when Web Unlocker returned an error header BUT the body still has substantial HTML (≥ 1000 chars), the response flows through to `detect_invalid_page` (and Turn A as ultimate safety net) instead of failing hard. Logged at WARN. Applies only to `expect_html=True`; Datasets/DCA trigger endpoints (small JSON responses) always fail fast on any error header.

### Scraper independence (added after M12 findings)

`html_scraper.py` and `api_scraper.py` catch `BrightDataInfraError` and convert to `ScrapeFailed(signature=(site, "extraction_infra", ""))` / `(site, "api_infra", "")`. Rationale: each scraper's BD channel is independent (Web Unlocker ≠ DCA ≠ Datasets), so one channel's infra failure shouldn't block the router from trying the next scraper. The router's `_derive_reason` promotes the escalation reason back to `infra_failure` only when **all** attempted channels failed with `*_infra` signatures — that's a genuine BD-ecosystem-down event.

### UTF-8 mojibake resilience + anchoring/promotion hardening (M16)

Triggered by M12 live findings: Tesco items `[01] Dove` and `[06] Peroni` fell through the HTML route to the DCA API backup after 400+ seconds each (Argos HTML succeeded on the same pipeline). Root cause: `BrightDataUnlocker.fetch()` returned `resp.text`, letting httpx auto-guess the charset. For Tesco, httpx guesses Latin-1, so UTF-8 `£` (`C2 A3`) becomes `Â£` (`C3 82 C2 A3` re-encoded). LLM-generated parsers commonly do `text.replace('£','')` → `Decimal(...)`; on `'Â£20.00'` the spurious `Â` survives → `Decimal('Â20.00')` raises `InvalidOperation` → parser fails gates → repair ladder burns both attempts → router falls to the DCA API. Argos samples were clean (no mojibake), matching the log divergence exactly.

Secondary root cause: the price-aware pre-pass (M14) never anchored the main visible DOM price — `_is_inside_main` only accepted a node inside the `<h1>` subtree or whose ancestor text contains the URL product id. Tesco's price lives in a `data-auto="pdp-buy-box"` sibling of the `<h1>`, so every DOM price was `anchor=ambiguous`. `detect_promotion._find_price_container` returned the *first* mid-sized promotion-hint container in DOM order, picking the wrong one (delivery-fee container on cloth_normal, missed regular price on Peroni).

Five coordinated changes:

1. **Force UTF-8 decode at extraction** (`extraction/bright_data.py` `BrightDataUnlocker.fetch()`): replace `return resp.status_code, resp.text` with `enc = resp.charset_encoding or "utf-8"; html = resp.content.decode(enc, errors="replace"); return resp.status_code, html`. Honors an explicitly declared `Content-Type` charset (future sites that genuinely serve Latin-1/Shift-JIS), defaults to UTF-8 otherwise (every modern retailer). Single choke point — no per-parser or per-site mojibake patching.
2. **Re-saved mojibaked Tesco fixtures** (`data/html_sample/tesco_*.html`): targeted byte-level replacement of `C3 82 C2 A3` → `C2 A3` (Â£ → £). Zero `Â£` byte sequences remain; Argos fixtures were already clean.
3. **Anchor the main DOM price** (`repair/prepass.py` `_anchor_evidence`): added two site-agnostic signals — (a) **cross-source value corroboration**: any DOM/meta evidence whose Decimal-normalized value equals a JSON-LD-anchored inside_main value is promoted to inside_main ("a visible price equal to the schema.org main-offer price is the main price"); (b) **primary price-container membership**: evidence whose DOM node is a descendant of `_find_price_container(soup)` is promoted to inside_main. Uses only JSON-LD-anchored values for container scoring (not DOM) to avoid circular anchoring where a wrongly-selected container anchors delivery-fee DOM prices which then feed back and lock in the wrong choice. Added `_is_descendant_of()` helper.
4. **Harden promotion container selection** (`repair/prepass.py` `_find_price_container`): replaced "first mid-sized container in DOM order" with a 6-factor scored heuristic (no site-hard-coded strings): trusted-value match (+100), buy-box hints `pdp-buy-box/buybox/buy-box/product-price/price-block/value-bar` (+50), mid-sized 20-2000 chars (+30), contains at least one price amount (+25, prevents picking logo divs with no prices), membership gating keywords (+10), h1 proximity (+20/+15). Uses Decimal-based numeric comparison (not substring — `"2"` no longer matches inside `"2.25"`). Added `buy-box`, `pdp-buy-box`, `pdp-buybox` to `_PROMOTION_CONTAINER_HINTS`. `detect_promotion(soup, trusted_values=None)` threads trusted values through for container scoring.
5. **Robust-extraction prompt guidance** (`repair/prompts.py` `parser_gen_prompt`): added a "ROBUST PRICE EXTRACTION" section directing the LLM to (a) locate the price via structure (JSON-LD offer / anchored buy-box), then (b) clean that node's text with a tolerant numeric regex (`re.search(r'\d[\d,]*\.?\d*', node_text)` → `m.group().replace(',', '')`), instead of fragile `text.replace('£','').strip()`. Explicit constraint: the regex is for **cleaning an already-located price node**, never for scanning the whole page (blind `[\d.]+` grabs pack sizes, ratings, delivery minimums). Defense-in-depth — works even when Web Unlocker returns messy markup.

Verified by re-running M14 (41/41) and M15 (44/44) against fixed fixtures. Pre-pass anchoring on Tesco samples: `alivio`/`net` discount — main DOM prices now `inside_main`, promo correct; `cloth_normal` — main DOM price `19.50` now `inside_main`, promo `cur=19.50` (delivery `£2.50` no longer chosen); `peroni` — DOM `13.00` `inside_main`, promo `kind=membership`, `mem=13.00`; `pc_membership` — DOM `2.00` `inside_main`, promo `kind=membership`.

### Datasets/DCA polling fix — trigger/poll split (M13)

`BrightDataDatasets` (Amazon) and `BrightDataDCA` (Tesco backup) use a **trigger-then-poll** async API. The old code ran the entire trigger+poll cycle inside `with_extraction_retry`, so a poll timeout re-POSTed to `/trigger` — creating a fresh snapshot and abandoning the original (which kept running on BD's side and eventually succeeded, visible on the console but never retrieved). The same URL could trigger up to 3 snapshots (1 + `extraction_retry_count`) before giving up.

M13 splits each client into `_trigger()` (retryable — a failed POST creates no snapshot) and `_poll()` (runs **outside** `with_extraction_retry`, owning the full wall-clock budget set by `bd_async_poll_max_seconds`). Only `_trigger` is wrapped in the retry. Result:

- One Amazon URL triggers **at most one** BD snapshot.
- Poll budget is configurable (`bd_async_poll_max_seconds` = 300s, `bd_async_poll_interval_seconds` = 4s) — covers the Amazon Datasets tail.
- First poll is immediate (no sleep before the first GET); transient single-GET failures are logged and tolerated (loop continues).
- `BrightDataInfraError` on poll timeout/failure propagates exactly once — no retry, no re-trigger, honoring the "no retry" intent from D21.
- The HTML route (Unlocker) is completely unaffected (`with_extraction_retry` was not modified).

### Datasets/DCA polling response parsing (M27)

Bright Data's `/dca/dataset` endpoint serves completed collections as JSON Lines. A one-record response is one JSON object plus a newline, so `httpx.Response.json()` returns a `dict`, not the list the old DCA terminal check required. M27 routes both Datasets and DCA polling through one shape-tolerant loop: it accepts JSON arrays, JSON objects, single-line JSONL, and multi-line JSONL; classifies pending/failed status envelopes; fails fast on 401/403; and includes the last response shape in timeout diagnostics. Trigger retry behavior is unchanged, so polling can never create a second job.

Known limitation: the current Tesco DCA collector does not emit a Clubcard/member-price field. The fallback therefore cannot populate `membership_price` until the collector definition is updated in the Bright Data console; the code-side aliases remain best-effort only.

## File Structure

```
src/scraping/
├── __init__.py             # Public API: scrape(), ProductData, ScrapeFailed
├── config.py               # ScrapingConfig (spec §7)
├── providers.py            # LLM model/provider registry + unified client factory (M18)
├── exceptions.py           # ScrapeFailed, BrightDataInfraError, SandboxSpawnError
├── detection.py            # Invalid page detection (5 signals)
├── router.py               # Two-hop dispatch + fallback loop + escalation writer (M10)
├── registry.py             # @register_scraper decorator
├── hosts.yaml              # host → site mapping (edit here to add sites)
├── sites.yaml              # site → page-type availability / cold-start profile
├── site_profile.py         # fail-open site-profile loader and policy accessors
├── coldstart.py            # CLI cold start (M11/M17/M19: validated Excel + review/repair loop + golden HTML reuse)
├── models/                 # ProductData (M15: availability_raw normalizer), enums, InvalidTargetResult
├── validation/             # gate1 (Pydantic), gate2 (feasible_check + M15 structural price rules)
├── scrapers/
│   ├── base.py             # BaseScraper ABC
│   ├── html_scraper.py     # HTMLScraper Template Method (M6 parser list + M8 hook + M9 seed + M10 signature + M15 fast-path distrust guard)
│   ├── api_scraper.py      # DirectAPIScraper + JSON healer integration + heal cache
│   └── sites/              # Tesco / Argos / AmazonUK / TescoDCA
├── extraction/
│   ├── bright_data.py      # Unlocker / Datasets / DCA async clients
│   └── retry.py            # Extraction retry (D7)
├── repair/
│   ├── sandbox.py          # M7/M26 — AST isolation + bounded, cancellation-safe subprocess lifecycle
│   ├── agent.py            # M8 — repair ladder (RepairContext, ladder driver, no_product/source_absence branches)
│   ├── prompts.py          # M8/M14/M15 — prompt builders + SCHEMA_HINT + PriceContext renderer
│   ├── prepass.py          # M14/M15 — price-aware context builder (PriceEvidence, PriceContext, anchoring, cross-sell delete, promotion signal detector)
│   ├── json_healer.py      # M8 — restricted JSON remap (D25 3-layer enforcement)
│   └── golden.py           # M9/M14 — classify_page_type (5 buckets incl. membership), promote_candidate, prune
├── storage/
│   ├── database.py         # 6 SQLite tables (golden_samples CHECK incl. membership since M14)
│   └── ...                 # store classes (golden, parser, run, result, escalation, phrase)
└── tests/                  # Legacy verify_mN.py scripts pending topic-based pytest migration
```

## Milestone Status

| M | Component | Verified |
|---|-----------|----------|
| M1 | ProductData schema + two gates | ✔ verify_m1_m3.py |
| M2 | BaseScraper + Router + Registry | ✔ verify_m1_m3.py |
| M3 | SQLite 6 tables + config | ✔ verify_m1_m3.py |
| M4 | DirectAPIScraper (Amazon + TescoDCA) | ✔ verify_m4_m5.py |
| M5 | HTMLScraper extraction + invalid-page detection | ✔ verify_m4_m5.py |
| M6 | Ordered parser list + scrape_runs writes | ✔ verify_m6.py |
| M7 | Sandbox runner | ✔ verify_m7.py |
| M8 | Agent repair ladder + JSON healer (real Qwen) | ✔ verify_m8.py |
| M9 | Golden set + promote/prune | ✔ verify_m9.py |
| M10 | Scraper-level fallback + escalation writing | ✔ verify_m10.py |
| M11 | Cold start CLI (real configured cold-start provider) | ✔ verify_m11.py |
| M12 | End-to-end live scraping (real BrightData + Qwen, 4-way concurrent, config-driven ladder) | Historical logs; tool moved to `scripts/live_batch_report.py` |
| M13 | Datasets/DCA polling fix — trigger/poll split (no duplicate BD triggers) | ✔ verify_m13.py |
| M14 | Price-aware pre-pass + anchoring + prompt rewrite (evidence-driven, site-agnostic) + membership golden bucket (5 types) + API membership mapping | ✔ verify_m14.py |
| M15 | Data-quality gates: promotion detection (structural, visual-value-bar-first), availability normalization, gate2 structural rules, fast-path distrust guard, prompt rewrite | ✔ verify_m15.py |
| M16 | UTF-8 mojibake resilience (extraction choke point) + anchoring hardening (cross-source value corroboration, buy-box container membership) + promotion container scored selection + robust-extraction prompt guidance | ✔ verify_m14.py + verify_m15.py (re-run on fixed fixtures) |
| M17 | Validated Excel cold-start input + config-driven page-type requirements + global golden caps/URL dedup + provenance-aware dry-run pruning | ✔ verify_m17.py |
| M18 | Provider-aware LLM registry + unified Qwen/DeepSeek client factory + dynamic dotenv key lookup | ✔ verify_m18.py |
| M19 | Cold-start structured correction loop + all-pass persistence gate + full review panel + stale-golden control + HTML snapshot reuse | ✔ verify_m19.py |
| M20 | Canonical price-field contract + Gate ordering rules + cold-start clear-value feedback | ✔ verify_m20.py |
| M21 | Human-terminated cold-start repair + repeating final rung + sliding feedback/ledger + partial save | ✔ verify_m21.py |
| M22 | Remove ProductData unit-price fields + guard API JSON healing/cache against unit-price contamination | ✔ verify_m22.py |
| M23 | Argos promotion/runtime repair + site profiles + anchored prices + thinking output cap | ✔ verify_m23.py |
| M24 | API price-contract normalization + failed-run observability + schema migration | ✔ verify_m24.py |
| M25 | Evidence-gated source-absence pre-screen on repair attempt 1 | ✔ verify_m25.py |
| M26 | Cancellation-safe sandbox lifecycle + bounded process/network/client resources | ✔ verify_m26.py |
| M27 | Shape-tolerant JSON/JSONL polling + status/auth/timeout diagnostics | ✔ verify_m27.py |
| M28 | Run/result/escalation correlation + repaired-parser attribution | ✔ test_run_correlation.py |

## Public API

```python
from src.scraping import scrape, ProductData, InvalidTargetResult, ScrapeFailed

result = await scrape("https://www.argos.co.uk/product/3284476")
# result is either ProductData or InvalidTargetResult; ScrapeFailed raised on terminal failure
```

## Cold Start (new site)

```bash
uv run python -m src.scraping.coldstart --site tesco --input src/scraping/data/cold_start/tesco.xlsx
```

The workbook requires `page_type` + `url`. Mandatory coverage is validated before network spend. Non-stale golden HTML for the same URL is reused unless `--force-fetch` is passed. Interactive review shows every non-tracing `ProductData` field plus declared-bucket warnings. A rejection collects structured field corrections and a free-text hint; after the full round the parser is repaired and all URLs are rerun. The model ladder is a warm-up schedule: its last rung and temperature repeat, with thinking enabled, until all cases pass, the human quits, or the safety cap is reached. Each repair prompt contains only the preceding round's failures plus a compact resolved/regression ledger. Unchanged prior acceptances are reused. Complete success writes the parser + accepted goldens (exit 0, or exit 2 for incomplete coverage); `s` saves the current parser + this round's accepted goldens as a partial result (exit 2); `q` writes nothing (exit 1).

## Key Config (all in `ScrapingConfig`, spec §7)

- `BRIGHT_DATA_KEY` / provider keys such as `QWEN_KEY` and `DEEPSEEK_KEY` — loaded from `.env`
- `SCRAPING_DB_PATH` — SQLite path (default: `scraping.db`)
- `repair_model_ladder` / `repair_temperature_ladder` — runtime HTML-repair model + temperature per attempt (default: `["deepseek-v4-flash", "deepseek-v4-flash"]` / `[0.1, 0.4]`; lengths must match); JSON healing uses the first repair model
- `cold_start_model_ladder` / `cold_start_temperature_ladder` — independent cold-start warm-up schedule (default: `["deepseek-v4-flash", "deepseek-v4-flash"]` / `[0.1, 0.4]`; lengths must match); the last rung repeats with thinking enabled
- `cold_start_max_repair_rounds = 10` — runaway guard for the otherwise human-terminated cold-start repair loop
- `bd_async_poll_max_seconds` / `bd_async_poll_interval_seconds` — Datasets/DCA poll budget (default: 300s / 4s; from M13 Amazon fix)
- `json_heal_budget = 1`
- `sandbox_timeout = 10s`, `sandbox_max_concurrency = 8`, `sandbox_spawn_retries = 2`, `sandbox_spawn_retry_interval = 1.0s`, `sandbox_import_whitelist = [bs4, lxml, re, json]`
- `prune_sliding_window = 50`, `per_site_parser_limit = 4`
- `cold_start_page_require_mandatory` — default mandatory: standard, discounted, out_of_stock, membership; multipack optional
- `golden_max_samples_per_page_type = 3` — global cap for cold start and runtime auto-seeding
- `mass_invalid_target_ratio = 0.3`, `mass_invalid_target_absolute = 20`

## Phase 0 Known Compromises

- **Windows sandbox**: `resource.setrlimit` is POSIX-only. On Windows only the subprocess timeout provides isolation. Phase 2 will use Docker.
- **JSON heal cache**: In-memory class-level dict (`DirectAPIScraper._json_heal_cache`), lost on process restart. Next scrape re-heals (~1 LLM call).
- **INFRA ALERT**: Logged via `logger.error`, no email/IM. Phase 1 hook.
- **LLM output variance**: Verify scripts test *machinery*, not exact parser code. Different runs may produce different (but correct) parsers.

## External Dependencies

- **BrightData Web Unlocker** — raw HTML (Tesco, Argos)
- **BrightData Datasets API** — structured JSON (Amazon)
- **BrightData DCA** — structured JSON (Tesco backup)
- **Provider-aware LLMs** — runtime repair defaults to Qwen via DashScope; cold start defaults to DeepSeek via its official OpenAI-compatible API. Add models/vendors only in `providers.py`, then set the selected provider key in `.env`.

### Output cap (`ProviderSpec.max_output_tokens`)

Parser generation emits the whole `parse()` source as a JSON-escaped string, so replies run long. Providers apply a small default when no cap is requested — DeepSeek uses 8192, which truncates the reply mid-JSON; because `providers.py` sets `response_format={"type": "json_object"}`, langchain takes the OpenAI SDK's `.parse()` path, which raises `LengthFinishReasonError` and **discards the partial content**. Cold start then had nothing to salvage and aborted the whole run.

Each `ProviderSpec` therefore registers `max_output_tokens` (DeepSeek: 32768, well under its 384K ceiling); `None` keeps the provider default. Providers may also register `thinking_max_output_tokens` when reasoning and visible content share the same budget (DeepSeek: 65536). `make_chat_client(..., max_tokens=N)` overrides both per call.

The cap is injected into `extra_body` as a body-level `max_tokens`, **not** passed as `ChatOpenAI(max_tokens=...)`: langchain rewrites that field to `max_completion_tokens`, which DeepSeek accepts and silently ignores (verified live — a 12-token cap truncated the reply as `max_tokens` and had no effect as `max_completion_tokens`). Anything raising a cap must keep using the body-level name.

## Verification Discipline (mandatory)

Add new developer tests by topic under `tests/unit/scraping/` and run them with
pytest. Keep the default suite offline and deterministic by mocking BrightData,
HTTP, and LLM clients. Any test that calls a real paid API MUST use
`@pytest.mark.live`; it is excluded from the default `uv run pytest` run.
Tests that launch real sandbox subprocesses or perform multi-second I/O SHOULD
also use `@pytest.mark.slow`.

Do not add new `verify_mN.py` scripts or committed run logs. Existing milestone
scripts are migration inputs only: move their checks to topic-based pytest files,
then delete a script once every check has equivalent coverage. Historical logs
under `tests/logs/archive/` are immutable audit evidence.

The paid end-to-end batch report is operational tooling, not a test. Run it only
when explicitly needed with `uv run python -m src.scraping.scripts.live_batch_report`.

## M19 — Cold-start correction and golden reuse

- Cold start has an independent provider-aware model/temperature warm-up schedule. Node 0 generates the parser; later rounds receive the current code and immediately preceding review evidence. M21 makes the final rung repeat with provider-specific thinking.
- A round that returns no usable code (truncated reply, unparsable JSON) falls through: with no prior code the next round generates from scratch, while a usable current parser is retained. M21 makes two consecutive unusable replies terminal.
- Review is round-based. Every repair reruns every resolved URL; a previously accepted result is auto-accepted only when all golden-compared fields remain equal.
- Persistence is atomic at workflow level: any parser/gate crash or human rejection blocks both parser and golden writes. Fetch failures are reported separately and do not blame/block the parser.
- The review panel derives fields from `ProductData.model_fields`, excludes only tracing/debug fields, summarizes image lists and long strings, and flags empty page-type-critical fields.
- Cold start reads same-URL HTML from non-stale `golden_samples` before BrightData. `--force-fetch` bypasses this cache. Resolution never writes unreviewed snapshots.
- Golden rot detection is now real: when a candidate fails a golden, every active parser is sandboxed against that sample. If none reproduces the expectation (including the orphan-golden case), the golden is marked stale and does not reject the candidate.

**Verification**: `verify_m19.py` — 38 offline checks covering the persistence gate, correction convergence/feedback, review reuse, model forwarding/thinking, ladder fall-through on an unusable node reply, panel rendering, stale-golden behavior, and snapshot cache/force-fetch semantics.

## M20 — Canonical price-field contract

- `price` is the ordinary, non-member current customer price. Every in-stock result must have a positive value.
- `list_price` is only a separately displayed higher Was/RRP reference: normal discounted products use `price + list_price` and require `list_price > price`.
- `membership_price` is only a visibly gated lower loyalty/member price: membership products use `price + membership_price` and require `membership_price < price`. A separate higher Was/RRP remains optional, producing `list_price > price > membership_price`.
- Gate 2 enforces those relations for every route, and parser/cold-start repair prompts repeat the same contract. In the cold-start review UI, `-`, `none`, `null`, and `n/a` now explicitly mean “clear or omit this field”, so a rejected field cannot be accidentally taught as a literal hyphen.

**Verification**: `verify_m20.py` — 15 offline checks covering valid standard/discounted/membership/triple-price combinations, invalid equality/inversion/zero cases, in-stock price requirement, prompt wording, and clear-value feedback normalization.

## M21 — Human-terminated cold-start repair

- Cold-start rounds are independent of ladder length. The configured ladder is a warm-up schedule; its last model and temperature repeat for later rounds, with thinking enabled from that rung onward.
- After a failing review, `c` or legacy alias `y` continues, `q` abandons without writes, and `s` persists the current parser plus that round's accepted goldens as `partial=True` (CLI exit 2). The configured maximum only guards against runaway interaction and offers save/quit when reached.
- Repair prompts carry only the immediately preceding round's corrections and sandbox/gate failures. A bounded URL→field ledger records resolved issues, while regressions are removed from the resolved ledger and raised both in the next prompt and as `[REGRESSION]` console output.
- One unusable LLM reply falls through while retaining any usable current parser; two consecutive unusable replies abort.

**Verification**: `verify_m21.py` — 31 offline checks covering five-round convergence on a two-rung ladder, repeated final-rung client settings, current-round-only feedback, resolved/regression ledger behavior (including sandbox regressions), continue/quit/partial-save exits, the safety cap, and unusable-reply fall-through/termination.

## M22 — Unit-price field removal and API healing guard

- `ProductData` no longer exposes `unit_price` or `unit`; Amazon mapping and all downstream field lists/reporting follow the same contract.
- The HTML pre-pass continues to retain unit-price evidence as a negative signal, but parser prompts no longer offer those fields as targets.
- JSON healing rejects unit-price source keys and per-unit value shapes for `price`, `list_price`, and `membership_price`, both for fresh LLM mappings and cached mappings.

**Verification**: `verify_m22.py` — 21 offline checks for model/mapping removal, source-key and value-shape detection, poisoned and legitimate healing, cache replay, and prompt wording.

## M23 — Argos runtime repair and site profiles

- `sites.yaml` declares per-site page-type availability and cold-start requirements. Undeclared sites and types fail open to the global config; unavailable types can never become mandatory. Add or review a profile when onboarding a site whose commercial page types differ from the global defaults.
- Promotion detection accepts a site veto, no longer treats loyalty-point accrual such as Argos Nectar points as gated pricing, and anchors canonical `price` to trusted parser/JSON-LD values. Only visibly struck or Was/RRP-labelled higher values become `list_price`; visibly gated lower values become `membership_price`.
- The fast-path guard passes validated product prices and the site into the shared detector, so Argos standard pages reuse their active parser instead of entering repair and DCA fallback. Argos URL-product IDs now accept alphanumeric `tuc...` values.
- DeepSeek thinking nodes receive a separate 65536-token output cap because reasoning and visible parser content share the output budget; non-thinking nodes remain at 32768.
- Cold-start input and coverage checks use the site profile, and accepted results classified into a declared-unavailable bucket produce a conspicuous reverse-validation warning.

**Verification**: `verify_m23.py` — 90 offline checks across all 16 reviewed Argos/Tesco golden snapshots, active-parser fast-path reuse, unanchored fallback safety, site profiles, alphanumeric Argos IDs, cold-start rejection, and DeepSeek token routing.

## M24 — API price normalization and execution observability

- Every hand-written API mapping passes through `scrapers/price_fields.py` before validation. The normalizer drops qualifiers that violate the M20 ordering contract while retaining positive qualifier-only signals when ordinary `price` is absent. HTML output is deliberately not normalized so gate failures continue to drive parser repair.
- `_record_run` and `_store_result` live on `BaseScraper`. Each API/HTML terminal raise site writes an `outcome='escalated'` run with a canonical signature and truncated error.
- `scrape_runs` is the per-execution log. `escalations` remains the `UNIQUE(signature)` aggregate/alarm defined by D24. API gate failures attach a raw-payload preview to the aggregate for diagnosis.
- Existing databases gain `scrape_runs.signature` and `scrape_runs.error` automatically through the serialized incremental migration in `ScrapeDB.init_db()`.

**Verification**: `verify_m24.py` — offline checks covering price normalization, the Amazon equal-price regression, all six terminal raise sites, per-execution logging, historical schema migration, payload preview persistence, exception detail, and the mass-invalid denominator change.

## M25 — Evidence-gated repair source-absence pre-screen

- Turn B now runs at repair attempt 1 even when that attempt is the last configured node, allowing a `source_absent` verdict to skip the final parser-generation call.
- The judgment is asked only when the immediately preceding attempt ran successfully, failed at validation, and reported missing required fields. Sandbox crashes, golden rejections, and optional-only omissions remain on the parser-repair path.
- `source_absent` remains a scraper-scoped `ScrapeFailed`: it is written to `scrape_runs.signature`, and the router still tries the next scraper. The aggregate escalation reason remains `parser_broken`; no schema migration is required.

**Verification**: `verify_m25.py` — fully offline coverage for the evidence matrix, one/two/three-node ladder positions, source-absence short-circuit and fail-open behavior, run signature persistence, and router fallback.

## M26 — Process and resource lifecycle hardening

- Sandbox calls hold an event-loop-local concurrency gate, retry transient `EAGAIN`/`ENOMEM` spawn failures with diagnostics, and track owned child PIDs.
- Normal completion, timeout, and cancellation share one cleanup path that closes stdin, kills a live child, and awaits `waitpid` before returning. Spawn failures become `sandbox_spawn` scraper failures so router fallback and failure observability remain intact.
- Exceptional database paths close in `finally`; equivalent LLM clients reuse connection pools and expose explicit/exit cleanup; cold-start fetch fan-out respects `per_site_concurrency`.

**Verification**: `verify_m26.py` — 26 fully offline checks for cancellation/timeout reaping, retry exhaustion, concurrency caps, scraper/router fallback, exceptional DB cleanup, LLM client reuse/closure, cold-start limiting, and invalid config guards.

## M27 — Shape-tolerant Datasets/DCA polling

- Completed DCA collections may be `application/jsonl`; single-record bodies parse as a JSON object rather than a JSON array.
- Datasets and DCA now share JSON/JSONL normalization plus pending/failed/ready status classification without changing trigger retry boundaries.
- Permanent 401/403 responses fail immediately, while timeouts report poll count, last HTTP status, and a bounded body preview.
- The Tesco DCA collector currently omits Clubcard/member-price data, so that fallback remains unable to populate `membership_price` until the external collector is changed.

**Verification**: `verify_m27.py` — 23 offline checks covering the two observed single-record JSONL shapes, Argos gate-valid mapping, multi-line JSONL, 200/202 status envelopes, failure/auth handling, malformed/empty bodies, timeout diagnostics, one-trigger preservation, and Datasets regressions.

## M28 — Run correlation keys and repaired-parser attribution

- `results.run_id → scrape_runs.id` identifies the exact execution that produced every new qualified result. `scrape_runs.escalation_id → escalations.id` links every failed execution in a router fallback chain to its signature-deduplicated ticket; historical rows retain `NULL` because they cannot be backfilled safely.
- `scrape_runs` is strictly one row per execution. The old success dedup window and its configuration key were removed so each result can have a distinct producing run.
- HTML repair returns `CandidateSucceeded` to the scraper, preserving the promoted parser id. Repaired successes now populate `winning_parser_id`, so hit-rate ordering and pruning credit the parser's first successful scrape.
- Both HTML parser repair and API JSON healing record the actual configured LLM in `scrape_runs.repair_model`; non-repair runs leave it `NULL`. The former `model_used` column is renamed during migration and the unused `cost` column is removed.
- `ScrapeDB.init_db()` migrates both nullable foreign-key columns and the run-log schema before creating indexes. `clear_site()` explicitly detaches retained references even when SQLite foreign-key enforcement is disabled.

**Verification**: `tests/unit/scraping/test_run_correlation.py` and `test_clear_site.py` cover fast-path, repaired, and API result links; repeated executions; router and invalid-target escalation links; legacy migration/idempotence; and FK-on/FK-off clear behavior.

## Observations from M12 Qwen Live Run (2026-07-19)

Analysis of `verify_m12_qwen_output.log` (16 URLs, Tesco + Argos, Qwen 3.7 Plus):

### #2 — Most repairs need `agent_attempt_1`

4 of 6 agent-repaired URLs won on `agent_attempt_1` (the second attempt), only 2 on `agent_attempt_0`. This is expected: with the default 2-node ladder (`["qwen-3.7-plus", "qwen-3.7-plus"]`), attempt 0 runs at temp 0.1 with the "first" strategy, while attempt 1 runs at temp 0.4 with thinking mode enabled and the "last" strategy (step-by-step, all prior records visible). The thinking/temperature boost carrying most wins is not a bug. The attempt index is observable via `ProductData.parser_version` (`agent_attempt_N`), while the actual LLM is recorded separately in `scrape_runs.repair_model`. No action needed.

### #3 — Argos never reused a stored parser

Confirmed: every Argos HTML success went through `agent_repaired`; only Tesco shows `fast path (parser)`. Two real leaks, **not** a plumbing bug (`ParserStore.create` inserts `status='active'` and `get_active_ordered_by_hits` retrieves them — promoted parsers are reusable on a shared DB):

1. **Cold-start herd (timing).** `_run_parsers` reads stored parsers at the very start of a scrape. Argos repairs are slow (249–660s), so a promoted parser appears well after the other concurrently-launched Argos scrapes have already passed their parser-read step. Tesco reused parsers because its first wave repaired faster (~130s). verify_m12 uses a fresh temp DB per run — cross-run reuse is never exercised.

2. **Poor generality.** Log item [09] (LEGO, started 21:40) began *after* item [11] promoted its parser (~21:37) yet still re-repaired — proof a stored Argos parser was available and got rejected on a different product. An LLM parser tuned to one product's DOM can fail Gate 2 or the M15 fast-path distrust guard on the next product and silently re-repair, never surfacing that a stored parser was tried-and-rejected.

**Deferred fixes:** A per-site single-flight cold-start gate (first scrape repairs, concurrent siblings await + reuse) would fix leak 1. A JSON-LD-first generality prompt + tried-but-rejected `db_path` signal would address leak 2. Revisit if Argos repair cost becomes a concern.

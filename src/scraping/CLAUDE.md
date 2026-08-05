# Scraping Module

**Status**: M1–M18 complete. Full Phase 0 lifecycle. M1–M15: 266+ offline checks pass. M12: end-to-end live-scraping (Tesco+Argos). M13: Amazon/Tesco DCA polling fix (trigger/poll split — no duplicate Bright Data triggers). M14: price-aware pre-pass + membership golden bucket + prompt rewrite (evidence-driven, site-agnostic). M15: data-quality gates — promotion detection (structural, visual-value-bar-first), availability normalization (schema.org token recovery), fast-path distrust guard (single-parser reuse → self-heal), gate2 structural price rules, prompt demoted `validForMemberTier` from imperative to corroborating hint. M16: UTF-8 mojibake resilience at extraction + anchoring/promotion hardening (cross-source corroboration, scored buy-box container, robust-extraction prompt). M17: validated Excel cold start and capped golden lifecycle. M18: provider-aware LLM registry (Qwen + DeepSeek). See `src/scraping/tests/`.

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
  - `in_stock=True + price=None` → fault (unless `list_price` OR `membership_price` is present and >0)
  - `in_stock=True + price<=0` → fault (hallucinated zero from LLM default)
  - `in_stock=False + no_images + no_price + no_list_price + no_membership_price` → fault (likely an error/stub page, not a real out-of-stock product)

### Repair Ladder (§5.5)

Config-driven: attempt count = `len(cfg.repair_model_ladder)`. Each attempt registers a full `AttemptRecord` (code, capture summary, errors) fed back to the next attempt — no index misalignment, works for any node count. Default: `["qwen-3.7-plus", "qwen-3.7-plus"]` (2 attempts; previously 4, reduced in `fb68f14`).

When the ladder has 2 nodes, Turn B (source_absence) is skipped (attempt 1 is the last, and source_absence only runs on non-last attempts). The logic is:

1. **Turn A — no_product judgment** (attempt 0 only): LLM decides if HTML is a real product page. If not, backfill phrase to `invalid_target_phrases` and return `InvalidTargetResult`. Does NOT consume budget.
2. **Turn B — source_absence** (non-last attempt only; skipped on 2-node ladder): Distinguishes "hard-to-parse product page" (solvable) vs "no data on page" (source_absent → terminal).
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
- **Gate 2 structural price rules (M15)**: `_structural_price_rule()` rejects `list_price == price` and `membership_price == price` (route-agnostic). Prevents a parser from duplicating the same value into two price fields and slipping past gates.
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

## File Structure

```
src/scraping/
├── __init__.py             # Public API: scrape(), ProductData, ScrapeFailed
├── config.py               # ScrapingConfig (spec §7)
├── providers.py            # LLM model/provider registry + unified client factory (M18)
├── exceptions.py           # ScrapeFailed, BrightDataInfraError
├── detection.py            # Invalid page detection (5 signals)
├── router.py               # Two-hop dispatch + fallback loop + escalation writer (M10)
├── registry.py             # @register_scraper decorator
├── hosts.yaml              # host → site mapping (edit here to add sites)
├── coldstart.py            # CLI cold start (M11/M17: validated Excel + declared page types)
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
│   ├── sandbox.py          # M7 — subprocess + AST scan + timeout + setrlimit (POSIX)
│   ├── agent.py            # M8 — repair ladder (RepairContext, ladder driver, no_product/source_absence branches)
│   ├── prompts.py          # M8/M14/M15 — prompt builders + SCHEMA_HINT + PriceContext renderer
│   ├── prepass.py          # M14/M15 — price-aware context builder (PriceEvidence, PriceContext, anchoring, cross-sell delete, promotion signal detector)
│   ├── json_healer.py      # M8 — restricted JSON remap (D25 3-layer enforcement)
│   └── golden.py           # M9/M14 — classify_page_type (5 buckets incl. membership), promote_candidate, prune
├── storage/
│   ├── database.py         # 6 SQLite tables (golden_samples CHECK incl. membership since M14)
│   └── ...                 # store classes (golden, parser, run, result, escalation, phrase)
└── tests/                  # verify_mN.py + verify_mN_output.log per milestone (M1-M18)
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
| M11 | Cold start CLI (real Qwen) | ✔ verify_m11.py |
| M12 | End-to-end live scraping (real BrightData + Qwen, 4-way concurrent, config-driven ladder) | ✔ verify_m12.py |
| M13 | Datasets/DCA polling fix — trigger/poll split (no duplicate BD triggers) | ✔ verify_m13.py |
| M14 | Price-aware pre-pass + anchoring + prompt rewrite (evidence-driven, site-agnostic) + membership golden bucket (5 types) + API membership mapping | ✔ verify_m14.py |
| M15 | Data-quality gates: promotion detection (structural, visual-value-bar-first), availability normalization, gate2 structural rules, fast-path distrust guard, prompt rewrite | ✔ verify_m15.py |
| M16 | UTF-8 mojibake resilience (extraction choke point) + anchoring hardening (cross-source value corroboration, buy-box container membership) + promotion container scored selection + robust-extraction prompt guidance | ✔ verify_m14.py + verify_m15.py (re-run on fixed fixtures) |
| M17 | Validated Excel cold-start input + config-driven page-type requirements + global golden caps/URL dedup + provenance-aware dry-run pruning | ✔ verify_m17.py |
| M18 | Provider-aware LLM registry + unified Qwen/DeepSeek client factory + dynamic dotenv key lookup | ✔ verify_m18.py |

## Public API

```python
from src.scraping import scrape, ProductData, InvalidTargetResult, ScrapeFailed

result = await scrape("https://www.argos.co.uk/product/3284476")
# result is either ProductData or InvalidTargetResult; ScrapeFailed raised on terminal failure
```

## Cold Start (new site)

```bash
python -m src.scraping.coldstart --site tesco --input src/scraping/data/cold_start/tesco.xlsx
```

The workbook requires `page_type` + `url`. Mandatory coverage is validated before network spend. Interactive review shows declared type and any classifier mismatch; accepted rows seed the declared bucket with `created_by=coldstart`. Exit 2 means accepted data was seeded but mandatory coverage remains incomplete.

## Key Config (all in `ScrapingConfig`, spec §7)

- `BRIGHT_DATA_KEY` / provider keys such as `QWEN_KEY` and `DEEPSEEK_KEY` — loaded from `.env`
- `SCRAPING_DB_PATH` — SQLite path (default: `scraping.db`)
- `repair_model_ladder` / `repair_temperature_ladder` — model + temperature per attempt (default: `["qwen3.7-plus", "qwen3.7-plus"]` / `[0.1, 0.4]`; lengths must match); models resolve through `providers.py`; cold start and JSON healing use the first configured model
- `bd_async_poll_max_seconds` / `bd_async_poll_interval_seconds` — Datasets/DCA poll budget (default: 300s / 4s; from M13 Amazon fix)
- `json_heal_budget = 1`
- `sandbox_timeout = 10s`, `sandbox_import_whitelist = [bs4, lxml, re, json]`
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
- **Provider-aware repair LLM** — Qwen via DashScope by default; DeepSeek via its official OpenAI-compatible API. Add models/vendors only in `providers.py`, then set the provider key in `.env`.

## Verification Discipline (mandatory)

Every milestone verification MUST leave persistent artifacts under `src/scraping/tests/`. Inline-only verification (bash `python -c "..."` output that disappears into chat history) is not acceptable — the user must be able to audit and re-run.

For each new milestone:
1. **Add a `verify_mN.py` script** — named checks with `[PASS]`/`[FAIL]` output, ends with `SUMMARY: N passed, M failed`, exits non-zero on failure.
2. **Capture the output log** — run with `| tee src/scraping/tests/verify_mN_output.log`.
3. **Update [tests/README.md](tests/README.md)** — add the new files to the table.
4. **Prefer offline** — mock BrightData / LLM where possible. Real API only when strictly needed (e.g., LLM-generated parser correctness).

See [tests/README.md](tests/README.md) for the full inventory (currently 266+ checks across 11 log files).

## Observations from M12 Qwen Live Run (2026-07-19)

Analysis of `verify_m12_qwen_output.log` (16 URLs, Tesco + Argos, Qwen 3.7 Plus):

### #2 — Most repairs need `agent_attempt_1`

4 of 6 agent-repaired URLs won on `agent_attempt_1` (the second attempt), only 2 on `agent_attempt_0`. This is expected: with the default 2-node ladder (`["qwen-3.7-plus", "qwen-3.7-plus"]`), attempt 0 runs at temp 0.1 with the "first" strategy, while attempt 1 runs at temp 0.4 with thinking mode enabled and the "last" strategy (step-by-step, all prior records visible). The thinking/temperature boost carrying most wins is not a bug. The attempt index is already observable via `parser_version` / `model_used` (`agent_attempt_N`). No action needed.

### #3 — Argos never reused a stored parser

Confirmed: every Argos HTML success went through `agent_repaired`; only Tesco shows `fast path (parser)`. Two real leaks, **not** a plumbing bug (`ParserStore.create` inserts `status='active'` and `get_active_ordered_by_hits` retrieves them — promoted parsers are reusable on a shared DB):

1. **Cold-start herd (timing).** `_run_parsers` reads stored parsers at the very start of a scrape. Argos repairs are slow (249–660s), so a promoted parser appears well after the other concurrently-launched Argos scrapes have already passed their parser-read step. Tesco reused parsers because its first wave repaired faster (~130s). verify_m12 uses a fresh temp DB per run — cross-run reuse is never exercised.

2. **Poor generality.** Log item [09] (LEGO, started 21:40) began *after* item [11] promoted its parser (~21:37) yet still re-repaired — proof a stored Argos parser was available and got rejected on a different product. An LLM parser tuned to one product's DOM can fail Gate 2 or the M15 fast-path distrust guard on the next product and silently re-repair, never surfacing that a stored parser was tried-and-rejected.

**Deferred fixes:** A per-site single-flight cold-start gate (first scrape repairs, concurrent siblings await + reuse) would fix leak 1. A JSON-LD-first generality prompt + tried-but-rejected `db_path` signal would address leak 2. Revisit if Argos repair cost becomes a concern.

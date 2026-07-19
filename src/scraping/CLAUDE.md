# Scraping Module

**Status**: M1–M13 complete. Full Phase 0 lifecycle. M1–M11: 172 offline checks pass. M12: end-to-end live-scraping (Tesco+Argos). M13: Amazon/Tesco DCA polling fix (trigger/poll split — no duplicate Bright Data triggers). See `src/scraping/tests/`.

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

Config-driven: attempt count = `len(cfg.repair_model_ladder)`. Each attempt registers a full `AttemptRecord` (code, capture summary, errors) fed back to the next attempt — no index misalignment, works for any node count. Default: `["deepseek-v4-flash", "deepseek-v4-pro"]` (2 attempts; previously 4, reduced in `fb68f14`).

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
- **Role-specific strategy hints** (3 semantic roles): `first` = "JSON-LD first", `middle` = "fix specific missing field from capture", `last` = "thinking, inspect all prior records".
- **HTML truncation disclosure**: parser_gen prompt warns the excerpt may be truncated, pushes JSON-LD-first extraction.
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
├── exceptions.py           # ScrapeFailed, BrightDataInfraError
├── detection.py            # Invalid page detection (5 signals)
├── router.py               # Two-hop dispatch + fallback loop + escalation writer (M10)
├── registry.py             # @register_scraper decorator
├── hosts.yaml              # host → site mapping (edit here to add sites)
├── coldstart.py            # CLI cold start (M11)
├── models/                 # ProductData, enums, InvalidTargetResult
├── validation/             # gate1 (Pydantic), gate2 (feasible_check)
├── scrapers/
│   ├── base.py             # BaseScraper ABC
│   ├── html_scraper.py     # HTMLScraper Template Method (M6 parser list + M8 hook + M9 seed + M10 signature)
│   ├── api_scraper.py      # DirectAPIScraper + JSON healer integration + heal cache
│   └── sites/              # Tesco / Argos / AmazonUK / TescoDCA
├── extraction/
│   ├── bright_data.py      # Unlocker / Datasets / DCA async clients
│   └── retry.py            # Extraction retry (D7)
├── repair/
│   ├── sandbox.py          # M7 — subprocess + AST scan + timeout + setrlimit (POSIX)
│   ├── agent.py            # M8 — repair ladder (RepairContext, ladder driver, no_product/source_absence branches)
│   ├── prompts.py          # M8 — prompt builders + SCHEMA_HINT + JSON-LD-aware excerpt
│   ├── json_healer.py      # M8 — restricted JSON remap (D25 3-layer enforcement)
│   └── golden.py           # M9 — classify_page_type, promote_candidate, prune_stale, hard-cap prune
├── storage/                # 6 SQLite tables (parsers, golden_samples, scrape_runs, results, escalations, invalid_target_phrases)
└── tests/                  # verify_mN.py + verify_mN_output.log per milestone
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
| M8 | Agent repair ladder + JSON healer (real DeepSeek) | ✔ verify_m8.py |
| M9 | Golden set + promote/prune | ✔ verify_m9.py |
| M10 | Scraper-level fallback + escalation writing | ✔ verify_m10.py |
| M11 | Cold start CLI (real DeepSeek) | ✔ verify_m11.py |
| M12 | End-to-end live scraping (real BrightData + DeepSeek, 4-way concurrent, config-driven ladder) | ✔ verify_m12.py |
| M13 | Datasets/DCA polling fix — trigger/poll split (no duplicate BD triggers) | ✔ verify_m13.py |

## Public API

```python
from src.scraping import scrape, ProductData, InvalidTargetResult, ScrapeFailed

result = await scrape("https://www.argos.co.uk/product/3284476")
# result is either ProductData or InvalidTargetResult; ScrapeFailed raised on terminal failure
```

## Cold Start (new site)

```bash
python -m src.scraping.coldstart --site tesco --urls-file cold_urls.txt
```

Interactive: fetches all URLs → LLM generates first parser → user confirms each result (y/n/q) → seeds `parsers` + `golden_samples`. First-and-only manual step in the pipeline's lifetime.

## Key Config (all in `ScrapingConfig`, spec §7)

- `BRIGHT_DATA_KEY` / `DEEPSEEK_KEY` — API keys (loaded from `.env`)
- `SCRAPING_DB_PATH` — SQLite path (default: `scraping.db`)
- `repair_model_ladder` / `repair_temperature_ladder` — model + temperature per attempt (default: `["deepseek-v4-flash", "deepseek-v4-pro"]` / `[0.1, 0.4]`; lengths must match)
- `bd_async_poll_max_seconds` / `bd_async_poll_interval_seconds` — Datasets/DCA poll budget (default: 300s / 4s; from M13 Amazon fix)
- `json_heal_budget = 1`
- `sandbox_timeout = 10s`, `sandbox_import_whitelist = [bs4, lxml, re, json]`
- `prune_sliding_window = 50`, `per_site_parser_limit = 4`
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
- **DeepSeek** (OpenAI-compatible endpoint) — repair LLM (flash=deepseek-v4-flash, pro=deepseek-v4-pro)

## Verification Discipline (mandatory)

Every milestone verification MUST leave persistent artifacts under `src/scraping/tests/`. Inline-only verification (bash `python -c "..."` output that disappears into chat history) is not acceptable — the user must be able to audit and re-run.

For each new milestone:
1. **Add a `verify_mN.py` script** — named checks with `[PASS]`/`[FAIL]` output, ends with `SUMMARY: N passed, M failed`, exits non-zero on failure.
2. **Capture the output log** — run with `| tee src/scraping/tests/verify_mN_output.log`.
3. **Update [tests/README.md](tests/README.md)** — add the new files to the table.
4. **Prefer offline** — mock BrightData / LLM where possible. Real API only when strictly needed (e.g., LLM-generated parser correctness).

See [tests/README.md](tests/README.md) for the full inventory (currently 172 checks across 8 log files).

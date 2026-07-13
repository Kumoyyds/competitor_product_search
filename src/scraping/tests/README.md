# Scraping Module — Verification Artifacts

Each milestone's verification is persisted here so you can audit and re-run at any time.

## Milestone Overview

The scraping module was built incrementally across 12 milestones (M1–M12). Each milestone adds a self-contained capability, verified by a corresponding `verify_mN.py` script.

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
- **Amazon UK** via BrightData Datasets API — JSON response → `_map_fields()` extracts title, brand, GTIN (UPC), price, list_price, unit_price, variant, images.
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
LLM-driven self-repair when parsers fail or API data is malformed. Uses **real DeepSeek API**:
- **Repair ladder** (up to 3 attempts, shared budget):
  - **Turn A — no_product judgment**: LLM decides if HTML is a real product page. If not → backfill phrase to `invalid_target_phrases`, return `InvalidTargetResult`. Does NOT consume budget.
  - **Turn B — source_absence** (attempt 2 only): distinguishes "hard-to-parse product page" from "no data on page" (source_absent → terminal, skip attempt 3).
  - **Turn C — parser generation**: LLM produces `def parse(html, url) -> dict` → sandbox execute → gates → `promote_candidate()` → active parser inserted.
- **JSON healer** (DirectAPIScraper route): restricted JSON field remapping with **D25 red line** — may only remap to paths that already exist in the JSON payload. Never fabricates data. In-memory cache per scraper class.

### M9 — Golden Set + Promote/Prune
Parser quality control and lifecycle management:
- **`classify_page_type`** — 4 buckets: `standard`, `out_of_stock`, `discounted`, `multipack` (in priority order: out_of_stock > discounted > multipack > standard).
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
Bootstrap a new site with zero manual parser writing. Uses **real DeepSeek API**:
- **Interactive flow**: `python -m src.scraping.coldstart --site <site> --urls-file <file>` →
  1. Fetch all URLs via BrightData.
  2. Pick the largest 200-OK HTML as representative.
  3. LLM generates the first parser from that HTML.
  4. For each URL: run parser → display result → user confirms (y/n/q).
  5. Accepted URLs → seeded as golden samples; first parser inserted as `created_by=initial`.
- **This is the only manual step in the pipeline's lifetime** — after cold start, repair + promote/prune handle ongoing maintenance.

### M12 — End-to-End Live Scraping

Runs the full scraping pipeline against a per-site URL batch (`src/scraping/data/tesco_test.xlsx.xlsx` or `argos_test.xlsx.xlsx`, both 3-column `label / url / host`) using real BrightData and real DeepSeek. No mocking — this exercises the complete live pipeline end-to-end.

**Latest results** (July 2026):
| Batch | URLs | SUCCESS | Invalid Target | Infra Error | Escalated | Check |
|-------|------|---------|---------------|-------------|-----------|-------|
| `tesco_test.xlsx.xlsx` | 6 | 5 (83%) | 1 | 0 | 0 | ✓ 6/6 pass |
| `argos_test.xlsx.xlsx` | 8 | 6 (75%) | 2 | 0 | 0 | ✓ 6/6 pass |

The last-ditch attempt (attempt 3, `deepseek-v4-pro` with thinking mode) rescued URL 2 (McGregor lawnmower, 207s) — in prior runs without it, this URL had consistently escalated.

Key aspects:
- **Concurrent execution** — `asyncio.Semaphore(4)` runs up to 4 URLs in parallel; results stream in as they complete (via `asyncio.as_completed`) and the summary is sorted back to input order.
- **4-attempt repair ladder** — model sequence `[deepseek-v4-flash ×2, deepseek-v4-pro ×2]`; last attempt enables pro thinking mode (`reasoning_effort=high` + `extra_body.thinking`).
- **Gate 2 strengthened**: rejects in-stock items with price ≤ 0 (LLM hallucinated zero), and OOS items with no product signals (image_urls empty + no price + no list_price — likely an error page, not a real product).
- **Upstream error retry**: BD responses < 1000 chars OR containing `"502 Bad Gateway"` / `"500 Internal Server Error"` etc. (1000-5000 byte gap) trigger extraction retry with 2s pause.
- **Temp DB** — uses a temporary SQLite database (`%TEMP%\verify_m12.db`) to avoid polluting production `scraping.db`. Cleaned up on process exit.
- **`.env` key aliasing** — accepts `BRIGHT_UNLOCKER_KEY` in `.env` and injects as `BRIGHT_DATA_KEY` before config load.
- **Per-URL detailed report** — each URL gets a formatted block showing: input label, resolved site, scraper chain, outcome, all extracted ProductData fields (title / price / list_price / brand / gtin / variant / unit_price / unit / availability_raw / image_urls / parser_version / source_type), latency, and mechanisms triggered.
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
| `verify_m8.py` | M8 — repair agent no_product judgment, JSON healer D25 red line, end-to-end parser gen on real HTML | **real DeepSeek** |
| `verify_m8_output.log` | Latest run — 14 checks, 0 failed | — |
| `verify_m9.py` | M9 — page_type classification, promote_candidate (accept/reject), hard-cap prune, natural prune | offline |
| `verify_m9_output.log` | Latest run — 17 checks, 0 failed | — |
| `verify_m10.py` | M10 — escalation writing (parser_broken / api_malformed / infra_failure), signature dedup, mass_invalid_target thresholds, INFRA ALERT log | offline |
| `verify_m10_output.log` | Latest run — 14 checks, 0 failed | — |
| `verify_m11.py` | M11 — cold start CLI end-to-end: fetch → LLM gen → user confirm (y/n/q) → seed parser + goldens | **real DeepSeek** |
| `verify_m11_output.log` | Latest run — 15 checks, 0 failed | — |
| `verify_m12.py` | M12 — End-to-end live scraping (per-site batch from `tesco_test.xlsx.xlsx` / `argos_test.xlsx.xlsx`, concurrent, real BrightData + DeepSeek) | **real BrightData + DeepSeek** |
| `verify_m12_output.log` | Latest tesco run — 6 checks, 0 failed | — |
| `verify_m12_argo_output.log` | Latest argos run — 6/8 SUCCESS, 0 escalated | — |

**Total: 172 checks passed across all milestones (M1–M11). M12 adds live end-to-end validation (label-based checks vary per input batch).**

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
python -m src.scraping.tests.verify_m11  | tee src/scraping/tests/verify_m11_output.log
python -m src.scraping.tests.verify_m12  | tee src/scraping/tests/verify_m12_output.log
```

On Windows, prefix with `PYTHONIOENCODING=utf-8` (or use PowerShell's `$env:PYTHONIOENCODING="utf-8"`) so `->` and similar ASCII arrows don't crash cp1252.

## LLM-dependent tests

**verify_m8** and **verify_m11** hit the real DeepSeek API (per user decision — see plan file). Requirements:
- `DEEPSEEK_KEY` set in `.env` (loaded automatically via `python-dotenv`).
- Small cost per full run: ~$0.01–0.05 across a handful of `deepseek-chat` requests.
- **LLM output varies**: parser code generated on the same HTML can differ between runs. The verify scripts test *machinery* (ladder progresses, phrases backfill, D25 red line, coldstart seeds correctly), not exact parser code output. A rerun that produces a slightly worse parser may show more `[SKIP]` outputs but should not `[FAIL]`.

## Reading the log

- Section headers (`===...===`) mark which milestone/component is being checked
- `[PASS]` = expected result observed; the detail column shows the actual value
- `[FAIL]` = mismatch; detail shows expected vs actual
- `[SKIP]` = optional dependency missing (e.g., DEEPSEEK_KEY absent for M8/M11)

## What is NOT covered here

- Real BrightData network calls — exercised via `playground.ipynb` and `verify_m12.py`
- End-to-end scraping against live sites — covered by M12 (`verify_m12.py`)

## Convention

Any future milestone verification MUST add:
1. A `verify_mN.py` script here (offline preferred; real API only when strictly needed)
2. The captured `verify_mN_output.log` alongside it
3. An entry in this README's table

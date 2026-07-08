# Scraping Module

**Status**: M1–M5 implemented (core extraction pipeline). M6–M11 pending (parser list, sandbox, repair, golden set, fallback, cold start).

## Responsibility

Extracts structured product data from marketplace pages. Takes `(url, website)` as input, returns `ProductData` (Pydantic model), `InvalidTargetResult` (not-a-product sentinel), or raises `ScrapeFailed`.

## Design Spec

Full spec: `scraping_module_spec_v1_2.md` (v1.2, 510 lines). Key decisions are numbered D1–D29 with rationale.

## Architecture

### Data Flow

`Router.scrape(url)` → host→site→ordered scraper list (two-hop dispatch) → try each scraper:
- **HTMLScraper route** (Tesco, Argos): BrightData Web Unlocker → HTML → invalid target detection → parser list (M6) → two gates → ProductData
- **DirectAPIScraper route** (Amazon, Tesco DCA backup): BrightData Datasets/DCA API → JSON → field mapping → two gates → ProductData

### Class Hierarchy

```
BaseScraper (ABC)
  ├── HTMLScraper (Template Method)
  │     ├── TescoScraper     (Web Unlocker, order=1)
  │     └── ArgosScraper     (Web Unlocker, order=1)
  └── DirectAPIScraper
        ├── AmazonUKScraper  (Datasets API, order=1)
        └── TescoDCAScraper  (DCA API, order=2, Tesco backup)
```

### Two Gates (Public Checkpoint)

- Gate 1: Pydantic type/structure validation (`price` optional at this layer)
- Gate 2: `feasible_check` cross-field semantics (`in_stock=True + price=None` → fault)

## File Structure

```
src/scraping/
├── __init__.py          # Public API: scrape(), ProductData, ScrapeFailed
├── config.py            # ScrapingConfig (pydantic-settings, spec §7)
├── exceptions.py        # ScrapeFailed, BrightDataInfraError
├── detection.py         # Invalid page detection (JSON-LD, status, multi-absence, keywords)
├── router.py            # Two-hop dispatch + scraper-level fallback
├── registry.py          # @register_scraper decorator (D3)
├── coldstart.py         # (placeholder, M11)
├── models/
│   ├── product_data.py  # ProductData Pydantic model (Decimal price, D1)
│   ├── enums.py         # Outcome, EscalationReason, SourceType, etc.
│   └── results.py       # InvalidTargetResult, ScrapeOutcome union
├── validation/
│   ├── gate1.py         # Pydantic type validation
│   └── gate2.py         # feasible_check cross-field rules
├── scrapers/
│   ├── base.py          # BaseScraper ABC
│   ├── html_scraper.py  # HTMLScraper Template Method
│   ├── api_scraper.py   # DirectAPIScraper
│   └── sites/
│       ├── tesco.py     # TescoScraper (HTML, order=1)
│       ├── tesco_dca.py # TescoDCAScraper (DCA, order=2)
│       ├── argos.py     # ArgosScraper (HTML, order=1)
│       └── amazon_uk.py # AmazonUKScraper (Datasets API)
├── extraction/
│   ├── bright_data.py   # BrightDataUnlocker / Datasets / DCA clients
│   └── retry.py         # Extraction retry logic (D7)
├── repair/              # (placeholders, M7-M8)
│   ├── agent.py
│   ├── sandbox.py
│   ├── json_healer.py
│   └── prompts.py
└── storage/
    ├── database.py      # SQLite 6-table DDL
    ├── parser_store.py  # parsers table CRUD
    ├── golden_store.py  # golden_samples table
    ├── run_store.py     # scrape_runs table + dedup + hit rate
    ├── result_store.py  # results table (append-only, D24)
    ├── escalation_store.py  # escalations table (signature dedup)
    └── phrase_store.py  # invalid_target_phrases table
```

## What's Implemented (M1–M5)

- **M1**: ProductData schema + two-gate validation
- **M2**: BaseScraper ABC + Router two-hop + `@register_scraper` decorator
- **M3**: SQLite 6 tables + ScrapingConfig
- **M4**: DirectAPIScraper + AmazonUKScraper + TescoDCAScraper field mappings
- **M5**: HTMLScraper extraction layer + invalid page detection tool (5 signal layers)

## What's Pending (M6–M11)

- **M6**: Ordered parser list match logic + scrape_runs/results writing in parsers
- **M7**: Sandbox runner (subprocess + AST whitelist)
- **M8**: Agent repair ladder (DeepSeek flash/pro) + phrase backfill
- **M9**: Golden set + promote/prune lifecycle
- **M10**: Scraper-level fallback driver + escalation (4 reason types)
- **M11**: Cold start path end-to-end

## Key Config

- `BRIGHT_DATA_KEY` — BrightData API key (required)
- `DEEPSEEK_KEY` — DeepSeek API key (for repair, M8)
- `SCRAPING_DB_PATH` — SQLite database path (default: `scraping.db`)
- All spec §7 config items in `ScrapingConfig` (pydantic-settings)

## External Dependencies

- **BrightData Web Unlocker** — raw HTML extraction (Tesco, Argos)
- **BrightData Datasets API** — structured JSON (Amazon)
- **BrightData DCA** — structured JSON (Tesco backup)
- **DeepSeek** — repair LLM (M8, flash=deepseek-chat, pro=deepseek-reasoner)

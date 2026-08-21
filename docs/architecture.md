# PriceScope Architecture

## Current MVP Flow (search module only)

```
User calls match_product_batch(...) or python -m src.search.batch
    │
    ▼
src.search.batch
    │
    ├── Receives full input/output paths and SKU/web/country column names
    ├── Creates one mode=batch DB run
    ├── Reads the input Excel
    │
    ▼
asyncio.Semaphore (16 concurrent rows by default)
    │
    ├── Per row: match_product()
    │       │
    │       ├── Resolves that row's website and country
    │       ├── Search provider chain (DuckDuckGo / Serper)
    │       ├── Domain, brand and numeric filtering
    │       ├── Batched LLM distinguishing step when needed
    │       └── Flushes one task trace to SQLite
    │
    └── Returns the enriched DataFrame and optionally writes Excel
```

## Planned Full Architecture

```
┌─────────────┐
│   API       │  REST endpoints for triggering searches, checking status,
│  (src/api)  │  retrieving results. Future web UI backend.
└──────┬──────┘
       │
       ▼
┌──────────────────┐
│   Orchestrator   │  Pipeline coordination: receives search requests,
│(src/orchestrator)│  dispatches to search/scraping/matching, manages
└──┬───┬───┬───────┘  retries and progress tracking.
   │   │   │
   ▼   │   ▼
┌──────┐ │ ┌──────────┐
│Search│ │ │ Scraping  │  Search: current Google+LLM URL finder.
│      │ │ │           │  Scraping: future product page data extraction.
└──┬───┘ │ └─────┬─────┘
   │     │       │
   │     ▼       │
   │  ┌──────────┐
   │  │ Matching  │  Compares SKU attributes against scraped product data
   │  │           │  for precise match scoring.
   │  └─────┬─────┘
   │        │
   ▼        ▼
┌─────────────────┐
│    Storage      │  temp_db: in-progress partition results
│  (src/storage)  │  main_db: finalized matched results
│                 │  trash_bin: discarded/archived entries
└─────────────────┘
       │
       ▼
┌─────────────────┐
│    Models       │  Shared data models: SKU, Product, MatchResult
│  (src/models)   │
└─────────────────┘

Cross-cutting:
┌─────────────────┐
│    Common       │  llm_client: shared LLM configuration
│  (src/common)   │  config: centralized config loading
│                 │  logging: structured logging
└─────────────────┘
```

## Module Status

> The scraping module has since been built out to Phase 0 (M1–M23). Its mechanism-level
> design — repair ladder, golden set, parser promotion/retirement, cold start — is documented
> in [scraping_design.md](scraping_design.md).

| Module | Status | MVP Required |
|--------|--------|-------------|
| search | Implemented | Yes |
| api | Skeleton | No (Phase 2) |
| orchestrator | Skeleton | No (Phase 2) |
| scraping | Implemented | No (Phase 2) |
| matching | Skeleton | No (Phase 3) |
| storage | Skeleton | No (Phase 2) |
| models | Skeleton | No (Phase 2) |
| common | Skeleton | No (Phase 2) |

## MVP vs Future Expansion

**MVP (current)**: The search module is fully functional as a standalone pipeline. Use `match_product()` for one product or `match_product_batch()` / `python -m src.search.batch` for Excel batches. Per-run settings are arguments, not YAML.

**Phase 2**: Move higher-level orchestration into `orchestrator/`. Add `api/` endpoints. Introduce `storage/` beyond the current search trace database and define cross-module models.

**Phase 3**: Add `matching/` for attribute-level comparison beyond URL matching. Together with the implemented scraping module, this enables the full pipeline: find URL → scrape product data → match against SKU attributes.

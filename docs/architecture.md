# PriceScope Architecture

## Current MVP Flow (search module only)

```
User runs: python run.py
    │
    ▼
run.py ──► src.search.main (via runpy)
    │
    ├── Reads config.yaml (input file, SKU column, country, marketplace)
    ├── Reads input Excel from input/
    ├── Splits DataFrame into partitions
    │
    ▼
ThreadPoolExecutor (16 workers)
    │
    ├── Per partition: find_url_llm()
    │       │
    │       ├── Per row: do_product_searching()
    │       │       │
    │       │       ├── Serper API → Google search (site:marketplace)
    │       │       ├── URL filtering (check_url)
    │       │       ├── Brand filtering (get_brand, check_found_brand)
    │       │       ├── LangChain agent (Qwen LLM) picks best URL
    │       │       └── Returns URL or 'not found'
    │       │
    │       └── Returns DataFrame with URL column added
    │
    ├── Saves partition results to output/output_partitions/
    ├── Combines all partitions
    └── Saves final result to output/result.xlsx
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

| Module | Status | MVP Required |
|--------|--------|-------------|
| search | Implemented | Yes |
| api | Skeleton | No (Phase 2) |
| orchestrator | Skeleton | No (Phase 2) |
| scraping | Skeleton | No (Phase 2) |
| matching | Skeleton | No (Phase 3) |
| storage | Skeleton | No (Phase 2) |
| models | Skeleton | No (Phase 2) |
| common | Skeleton | No (Phase 2) |

## MVP vs Future Expansion

**MVP (current)**: The search module is fully functional as a standalone pipeline. Run `python run.py` with a configured `config.yaml` and input Excel file.

**Phase 2**: Extract orchestration from `search/main.py` into `orchestrator/`. Add `api/` for programmatic access. Introduce `storage/` to replace file-based I/O. Define `models/` for structured data passing between modules.

**Phase 3**: Add `scraping/` for product page data extraction. Add `matching/` for attribute-level comparison beyond URL matching. This enables the full pipeline: find URL → scrape product data → match against SKU attributes.

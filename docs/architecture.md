# PriceScope Architecture

## Current end-to-end flow

```text
xlsx / csv / JSON / Sequence[InputItem]
                 │
                 ▼
          ┌──────────────┐
          │ Orchestrator │── batch/item lineage ──▶ orchestrator.db
          └──────┬───────┘
                 │
       ┌─────────┼──────────┐
       ▼         ▼          ▼
   Search     Scraping   Matching
  title+URL  ProductData rules + optional Vision + LLM
       │         │          │
       └─────────┴──────────┘
                 │
                 ▼
          Valid / Failure
```

Search and Scraping retain their standalone public APIs and their own trace databases. Orchestrator uses the typed in-memory Search batch API, calls Scraping per URL, and verifies a newly discovered URL through Matching before writing an append-only Valid snapshot.

## New Input

New Input validates the file structure before paid calls, records invalid rows individually, then runs Search → Scraping → Matching in batches. Search title and Scraping `ProductData.title` remain separate evidence. Only a successful identity verdict writes Valid.

## Rerun

Every Rerun creates `<root>-rN` and selects the latest Valid URL for each logical product in the requested batch's scope. Unchanged identity fields write a fresh ProductData snapshot without another model call. Changed identity triggers Matching; a stored-URL failure or identity No Match gets one full Search → Scrape → Match fallback in the same rerun batch.

## Module ownership

| Module | Responsibility | Persistent store |
|---|---|---|
| `src/search` | Marketplace candidate discovery and URL selection | `search.db` trace |
| `src/scraping` | ProductData extraction, validation, parser repair | `scraping.db` |
| `src/matching` | Exact identity verification | Embedded in orchestrator results |
| `src/orchestrator` | Input parsing, workflow state, rerun lineage, terminal outcomes | `orchestrator.db` |
| `src/models` | Shared InputItem and ProductMatchResult contracts | None |
| `src/common` | Shared Search/Matching LLM provider routing | None |
| `src/api` | Future REST interface | Not implemented |

The former project-level `src/storage` skeleton was removed. In-progress state, Valid results, and failures now have one clear owner in `orchestrator.db`; no temporary or trash database is required.

## Configuration

- Search tuning: `src/search/maintain/search_config.yaml`
- Shared Search/Matching LLM vendors: `src/common/llm_router_config.yaml`
- Matching text and Vision models: `src/matching/matching_config.yaml`
- Scraping runtime: `src/scraping/config.py`, `hosts.yaml`, and `sites.yaml`

Generated database references live in `docs/search_storage.md`, `docs/scraping_storage.md`, and `docs/orchestrator_storage.md`.

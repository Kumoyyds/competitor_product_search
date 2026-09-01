# Orchestrator

Orchestrator implements New Input and Rerun over Search, Scraping, and Matching.

## New Input

```bash
uv run python -m src.orchestrator new --input input/products.xlsx [--vision] 
```
no vision by default

Accepted files are xlsx, UTF-8 CSV, and a JSON array of objects. Required columns/keys are `title`, `country` (or `region`), and `site_name`; optional `gtin` must be stored as text in spreadsheets, and `image_urls` accepts a JSON array string or one URL. Invalid rows become `fail_node=input`; missing required columns, malformed JSON roots, and empty inputs abort before paid calls.

Python callers can pass a path or `Sequence[InputItem]` to `run_new_input()`. Search failures have `search_title=NULL`; later failures retain the Search-selected title.

## Rerun

```bash
uv run python -m src.orchestrator rerun --batch-id b-... \
    [--search-title "Exact title"] [--vision|--no-vision]
```

Every run receives `<root>-rN`. Selection starts from the requested batch's logical item scope, then uses each item's latest Valid URL across the lineage. Title matching is trimmed/case-insensitive exact matching; duplicates all run, while any missing requested title rejects the operation before a child batch is created.

An unchanged scraped identity writes a new snapshot directly. Changed title/brand/GTIN/variant triggers Matching; a stored-URL failure or identity No Match performs one Search→Scrape→Match fallback in the same rerun batch. Intermediate failures stay in `stage_trace`; only terminal failures enter `failure_results`.

## Storage and exits

`orchestrator.db` is the default; set `ORCHESTRATOR_DB_PATH` or pass `db_path`. The database is append-only and stores full ProductData JSON. See [the generated schema](../../docs/orchestrator_storage.md).

CLI exits are 0 for all Valid, 2 for a completed batch with failures, and 1 for fatal input/configuration errors. Python APIs return `BatchResult`.

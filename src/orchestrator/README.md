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

## Concurrency

Both subcommands accept `--concurrency N` (default 8). It bounds Search, Scraping, and Matching alike, so lowering it throttles every paid call in the batch. Matching's own `llm.concurrency` in `src/matching/matching_config.yaml` applies only to direct `verify_products()` callers — the orchestrator always overrides it with this flag.

## Storage and exits

`orchestrator.db` is the default; set `ORCHESTRATOR_DB_PATH` or pass `db_path`. Terminal Valid/Failure rows and Matching history are append-only; batch and item lifecycle fields update in place. Full ProductData JSON lives only in `valid_results`, while `matching_decisions` is the single source for every Matching invocation, technical failure, and stored-URL identity reuse. Summary counts and joined terminal records are exposed through `batch_summary`, `item_outcomes`, and `latest_valid_results` views instead of duplicated columns.

Opening a v1/v2 database through an orchestrator command or `OrchestratorDB` transactionally migrates it to v3. The migration removes denormalized result columns, consolidates Rerun lineage on `batch_items.source_valid_result_id`, removes `vision_enabled` from `job_config`, and backfills recoverable Matching history. Opening the inspection notebook remains read-only and does not migrate the file. See [the generated schema](../../docs/orchestrator_storage.md).

To inspect a run, open `script/database_check.ipynb` (select the project's `.venv` kernel) and run it top to bottom. Set `BATCH_ID` and `ROW_LIMIT` once, then review database health, batch lineage, joined item outcomes, Matching replay, latest Valid snapshots, failure breakdown, and full JSON fields. Queries use short-lived `mode=ro` connections. Export is disabled unless an explicit path is configured; legacy v1/v2 files show a migration hint without being modified.

CLI exits are 0 for all Valid, 2 for a completed batch with failures, and 1 for fatal input/configuration errors. Python APIs return `BatchResult`.

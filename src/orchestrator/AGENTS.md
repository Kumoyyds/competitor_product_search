# Orchestrator Module

**Status**: Implemented

## Responsibility

Coordinates New Input and Rerun batches across Search, Scraping, Matching, and SQLite lifecycle/audit persistence.

## Inputs / Outputs

- **Input**: xlsx/csv/json paths or typed `InputItem` sequences; rerun batch ID plus optional titles
- **Output**: `BatchResult` and `orchestrator.db` Valid/Failure records

## Invariants

- File-structure errors abort before batch creation; row validation errors are terminal `input` failures while siblings continue.
- Search-selected title and scraped ProductData title remain distinct.
- Every rerun creates `<root>-rN`; selection uses the requested batch's item scope and latest Valid snapshots across the root lineage.
- Stored-URL and revalidation failures are intermediate when one fallback succeeds. Failure rows are terminal only.
- `operation` identifies New Input/Rerun; `fail_node` identifies the actual terminal stage.
- Valid/Failure rows and `matching_decisions` are append-only; batch/item lifecycle fields update in place. Common joined reads use the `batch_summary`, `item_outcomes`, and `latest_valid_results` views.
- `matching_decisions` is the only persisted Matching result source. It has one row per invocation, technical failure, or identity-guard skip; `attempt_no` orders revalidation followed by fallback.
- Rerun inputs point directly to the prior qualified snapshot through `batch_items.source_valid_result_id`.

## Files

- `workflow.py` — public APIs and both state machines
- `database.py` — documented v3 DDL, v1/v2 migration, lineage/result store, views, and Matching history
- `input.py` — canonical file parsing and per-row validation
- `__main__.py` — `new` / `rerun` CLI
- `script/database_check.ipynb` — read-only (`mode=ro`) notebook browsing every `orchestrator.db` table

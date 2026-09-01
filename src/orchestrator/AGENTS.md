# Orchestrator Module

**Status**: Implemented

## Responsibility

Coordinates New Input and Rerun batches across Search, Scraping, Matching, and append-only SQLite persistence.

## Inputs / Outputs

- **Input**: xlsx/csv/json paths or typed `InputItem` sequences; rerun batch ID plus optional titles
- **Output**: `BatchResult` and `orchestrator.db` Valid/Failure records

## Invariants

- File-structure errors abort before batch creation; row validation errors are terminal `input` failures while siblings continue.
- Search-selected title and scraped ProductData title remain distinct.
- Every rerun creates `<root>-rN`; selection uses the requested batch's item scope and latest Valid snapshots across the root lineage.
- Stored-URL and revalidation failures are intermediate when one fallback succeeds. Failure rows are terminal only.
- `operation` identifies New Input/Rerun; `fail_node` identifies the actual terminal stage.

## Files

- `workflow.py` — public APIs and both state machines
- `database.py` — documented v1 DDL and lineage/result store
- `input.py` — canonical file parsing and per-row validation
- `__main__.py` — `new` / `rerun` CLI

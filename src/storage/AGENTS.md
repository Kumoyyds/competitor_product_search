# Storage Module

**Status**: Skeleton (not yet implemented)

## Responsibility

Data persistence layer replacing the current file-based Excel I/O with structured storage.

## Planned Components

- `temp_db.py` — In-progress partition results (replaces `output/output_partitions/`)
- `main_db.py` — Finalized search/match results (replaces `output/result.xlsx`)
- `trash_bin.py` — Soft-delete and archival for discarded entries

## Inputs / Outputs

- **Input**: DataFrames or model objects from orchestrator/search/matching
- **Output**: Persisted records, queryable results

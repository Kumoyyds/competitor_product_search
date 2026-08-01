# Orchestrator Module

**Status**: Skeleton (not yet implemented)

## Responsibility

Pipeline coordination: receives search requests, dispatches work to search/scraping/matching modules, manages retries, concurrency, and progress tracking. Will eventually absorb the orchestration logic currently in `search/main.py`.

## Inputs / Outputs

- **Input**: Search requests (from API or CLI)
- **Output**: Completed search/match results written to storage

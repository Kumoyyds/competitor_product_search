# API Module

**Status**: Implemented

## Responsibility

The FastAPI app exposes orchestrator operations: start New Input and Rerun batches, list batches, inspect batch state, and read paginated item outcomes. It does not own the workflow state machine or a separate database schema.

## Data flow

- `routes/batches.py` validates HTTP input, calls synchronous `prepare_new_input()` / `prepare_rerun()` in a worker thread, submits the returned `PreparedBatch`, and responds with HTTP 202 and its batch ID.
- `runner.py` holds in-process tasks and an `asyncio.Semaphore` to bound executing batches. Its phase distinguishes queued work from executing work while the database batch status is `running`.
- `execute_batch()` in the orchestrator owns workflow execution and terminal database writes. The runner marks queued tasks interrupted if they are cancelled before execution starts.
- `queries.py` contains pure SQL readers over the existing `batch_summary` and `item_outcomes` views. GET requests use short-lived SQLite read-only connections; app startup initializes/migrates the database once.
- `schemas/batches.py` contains request and response models; `settings.py` reads `ORCHESTRATOR_DB_PATH`, `API_UPLOAD_DIR`, `API_MAX_CONCURRENT_BATCHES`, and `API_DEFAULT_CONCURRENCY`.
- `app.py` creates the app and manages lifespan. Startup marks orphaned running batches interrupted; shutdown cancels and joins runner tasks. `__main__.py` is the `python -m src.api` entry point.

## Runtime invariants

- One process, one worker, one API instance per orchestrator database. The semaphore and phase map are local to the process. Do not run the orchestrator CLI against that database at the same time as the API, because startup recovery would interrupt CLI-owned work.
- HTTP file submissions store their original contents under `API_UPLOAD_DIR` before preparation. File parsing accepts bad rows as terminal input failures while valid rows continue. Typed JSON `InputItem` submissions validate atomically.
- Batch and item progress comes from database lifecycle fields; stages may commit item statuses together. A pending count is not a count of work that has never begun.
- New Input and Rerun behavior remains in `src/orchestrator/`; API handlers translate expected input and batch lookup errors into HTTP responses at the route boundary.

## Operator documentation

See [README.md](README.md) for setup, endpoints, request examples, polling, and restart behavior.

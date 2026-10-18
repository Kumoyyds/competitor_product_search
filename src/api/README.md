# Operation API

The API starts orchestrator New Input and Rerun batches and reads their status and item outcomes. Start it from the repository root after following the [project setup](../../README.md#setup):

```bash
uv run python -m src.api --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for the interactive OpenAPI reference. This adds an HTTP entry point; existing CLI and Python calls do not need migration.

## Configuration

Set these variables in the root `.env` file or the process environment:

| Variable | Default | Purpose |
|---|---|---|
| `ORCHESTRATOR_DB_PATH` | `orchestrator.db` | Shared orchestrator SQLite database |
| `API_UPLOAD_DIR` | `uploads/` | Directory for submitted source files; created on startup |
| `API_MAX_CONCURRENT_BATCHES` | `1` | Maximum batches executing at once in this API process |
| `API_DEFAULT_CONCURRENCY` | `8` | Per-batch Search, Scraping, and Matching concurrency when a request omits it |

The upload directory is ignored by Git. The API keeps submitted files there, so the operator should manage its size and retention. Paths are resolved from the repository root when started there.

Run one API process with one worker for each `ORCHESTRATOR_DB_PATH`. The batch queue and its `queued` / `running` phase live in process memory. Do not run the orchestrator CLI or another API instance against the same database while this API is running: startup marks all database batches still marked `running` as `interrupted`, which would also affect work owned by another process. On a normal shutdown, active and queued batches are interrupted; after a crash, the next startup marks leftover running batches as interrupted. Interrupted batches are retained for inspection and can be used as a source for a later Rerun when they have qualified results.

## Start and inspect batches

Each POST returns HTTP 202 with a `batch_id` and `status_url`. Poll the status URL for the terminal batch status (`completed`, `completed_with_failures`, `failed`, or `interrupted`).

Upload an `.xlsx`, UTF-8 `.csv`, or `.json` file:

```bash
curl -X POST http://127.0.0.1:8000/batches/new-input \
  -F 'file=@input/products.csv' \
  -F 'vision_enabled=false' \
  -F 'concurrency=8'
```

Files require `title`, `country` (or `region`), and `site_name`; optional fields include text-formatted `gtin` and `image_urls`. A malformed file or missing required column rejects the request before creating a batch. Individual invalid rows become `input` failures while valid sibling rows continue. See the [Orchestrator README](../orchestrator/README.md#new-input) for file details.

Submit typed items as JSON:

```bash
curl -X POST http://127.0.0.1:8000/batches/new-input/items \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"title":"Example product","country":"uk","site_name":"tesco"}],"vision_enabled":false}'
```

The JSON route validates every `InputItem` before creating a batch; one invalid item rejects the entire request.

Rerun all qualified products from a prior batch, or select titles with `search_titles`:

```bash
curl -X POST http://127.0.0.1:8000/batches/b-123/rerun \
  -H 'Content-Type: application/json' \
  -d '{"search_titles":["Example product"]}'
```

Omitting `vision_enabled` inherits the selected parent batch's setting. A parent batch that is still running cannot be rerun until it finishes.

## Read status and outcomes

```bash
curl 'http://127.0.0.1:8000/batches?limit=20&offset=0'
curl 'http://127.0.0.1:8000/batches/b-123'
curl 'http://127.0.0.1:8000/batches/b-123/items?limit=20&offset=0'
```

`GET /batches` also accepts `operation`, `status`, and `root_batch_id` filters. `GET /batches/{batch_id}/items` accepts a `status` filter. Paginated responses include `total`, `limit`, `offset`, and `items`; the default page size is 50 and the maximum is 200. Batches sort newest first; items sort by input row. A missing batch returns 404, including on its items route.

The batch detail includes `total_items`, `valid_count`, `failure_count`, `finished_count`, pending/running item counts, and a process-local `phase` of `queued` or `running` while the server owns it. `finished_count` is `valid_count + failure_count`. Item statuses update as each workflow stage is committed, so `pending` does not necessarily mean an item has not started work. Polling gives batch-level progress, not a live per-item event stream.

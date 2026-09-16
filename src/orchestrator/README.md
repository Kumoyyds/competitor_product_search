# Orchestrator

Orchestrator implements New Input and Rerun over Search, Scraping, and Matching. See
[docs/orchestrator/design.md](../../docs/orchestrator/design.md) for how the two workflows
work internally (lineage fields, status/exit-code derivation, migration mechanics) and
[docs/orchestrator/storage.md](../../docs/orchestrator/storage.md) for the database schema.

## New Input

```bash
uv run python -m src.orchestrator new --input input/products.xlsx [--vision]
```

`--vision` is off by default.

Accepted files are xlsx, UTF-8 CSV, and a JSON array of objects. Required columns/keys are `title`, `country` (or `region`), and `site_name`; optional `gtin` must be stored as text in spreadsheets, and `image_urls` accepts a JSON array string or one URL. Invalid rows become `fail_node=input`; missing required columns, malformed JSON roots, and empty inputs abort before paid calls.

Python callers can pass a path or `Sequence[InputItem]` to `run_new_input()`.

## Rerun

```bash
uv run python -m src.orchestrator rerun --batch-id b-... \
    [--search-title "Exact title"] [--vision|--no-vision]
```

Every run receives `<root>-rN`. Selection starts from the requested batch's logical item scope, then uses each item's latest Valid URL across the lineage. Title matching is trimmed/case-insensitive exact matching; duplicates all run, while any missing requested title rejects the operation before a child batch is created.

`--vision` / `--no-vision` is optional. Omitting it inherits the parent batch's (the batch named by `--batch-id`) `vision_enabled` setting; passing either flag explicitly overrides that for this rerun and everything it writes. See [docs/orchestrator/design.md](../../docs/orchestrator/design.md) for exactly how Rerun decides between reusing a stored URL, revalidating identity, and falling back to a full Search → Scrape → Match pass.

## Concurrency

Both subcommands accept `--concurrency N` (default 8). It bounds Search, Scraping, and Matching alike, so lowering it throttles every paid call in the batch. Matching's own `llm.concurrency` in `src/matching/matching_config.yaml` applies only to direct `verify_products()` callers — the orchestrator always overrides it with this flag.

## Progress

Both subcommands print a `tqdm` progress bar per stage to stderr by default (`new_input search` → `new_input scrape` → `new_input match`; Rerun additionally shows `stored_url scrape`, `identity_revalidation`, and, for items that fall back, `fallback search`/`fallback scrape`/`fallback match`). Pass `--no-progress` to suppress it — useful when piping output or running in CI. Python callers get the same bars by passing `progress=True` to `run_new_input()` / `rerun()` (default `False`, matching Search's `match_products()` convention). Bar granularity mirrors real work: each scrape/match bar advances one tick per item as it finishes, not just per stage.

## Storage

`orchestrator.db` is the default database path. Override it with the `ORCHESTRATOR_DB_PATH` environment variable, or pass `--db-path PATH` on either CLI subcommand (Python callers pass `db_path=` to `run_new_input()` / `rerun()`). Terminal Valid/Failure rows and Matching history are append-only; batch and item lifecycle fields update in place.

Opening a v1/v2 database through an orchestrator command or `OrchestratorDB` transactionally migrates it to v3 automatically — no manual migration step is required. Opening the inspection notebook remains read-only and does not migrate the file. Schema-level migration facts live in [docs/orchestrator/storage.md](../../docs/orchestrator/storage.md#schema-compatibility); the mechanics of how the migration executes are in [docs/orchestrator/design.md](../../docs/orchestrator/design.md#4-migration-mechanics-v1v2--v3).

To inspect a run, open `script/database_check.ipynb` (select the project's `.venv` kernel) and run it top to bottom. Set `BATCH_ID` and `ROW_LIMIT` once, then review database health, batch lineage, joined item outcomes, Matching replay, latest Valid snapshots, failure breakdown, and full JSON fields. Queries use short-lived `mode=ro` connections. Export is disabled unless an explicit path is configured; legacy v1/v2 files show a migration hint without being modified.

Lineage and schema semantics (how `batch_items.source_valid_result_id`, `logical_item_id`, and the rest connect a rerun to its history) and the New-Input-vs-Rerun business logic are documented in [docs/orchestrator/design.md](../../docs/orchestrator/design.md) — this README stays operational.

## CLI output

Both subcommands print one JSON object to stdout on completion (whether or not the batch fully succeeded):

```json
{"batch_id": "b-...", "failed": 0, "status": "completed", "total": 12, "valid": 12}
```

| Field | Meaning |
|---|---|
| `batch_id` | The batch (or rerun child batch) this result describes |
| `status` | `completed`, `completed_with_failures`, `failed`, or `interrupted` |
| `total` | Item count in the batch |
| `valid` | Items that reached a Valid terminal outcome |
| `failed` | Items that reached a Failure terminal outcome |

A raised exception before any batch could be created (invocation error — bad CLI args, unreadable input file) instead prints `{"status": "failed", "error": "..."}` with no `batch_id`/`total`/`valid`/`failed`. Distinguish the two shapes by checking for `batch_id`.

## Exit codes

- `0` — batch completed with every item Valid.
- `2` — batch completed but at least one item ended in Failure (`status="completed_with_failures"`).
- `1` — everything else: a fatal input/configuration error before or during the run (bad file, bad flags, an exception mid-batch), **or** a batch that finished in `status="failed"` or `status="interrupted"` (e.g. items left unfinished after a normal exit, or the process was cancelled/interrupted) — a completed-but-not-fully-successful outcome, not necessarily an invocation mistake. Check the printed `status` field (see [CLI output](#cli-output)) to tell these apart.

Python APIs return `BatchResult` (`batch_id`, `status`, `total`, `valid`, `failed`, and an `exit_code` property with the same mapping) instead of exiting a process.

# Plan: Data-inspection notebook in `src/search/script/`

## Context

The project persists data in two SQLite files that currently have no easy way to browse:

- `search_db.sqlite` (repo root) — the run/task tracing DB written by `src/search/db.py` (`SearchDB`) via `src/search/trace.py`. Tables: `runs`, `tasks`, `attempts`, `node_events`, `candidates`, `llm_calls`, `meta`, plus 4 SQL views (`v_errors`, `v_task_result`, `v_funnel`, `v_run_summary`).
- `.cache/base_extraction.sqlite` — brand/numeric extraction cache (`src/search/cache.py`). Single table `base_cache` (hash → JSON payload).

The user wants a Jupyter notebook under `src/search/script/` (currently empty) that opens both databases and displays the contents of every table, clearly separated with headers, and with an upfront summary of row counts per table so it's easy to see how much data exists before scrolling through details.

## Notebook location

`src/search/script/inspect_tables.ipynb`

## Structure

1. **Title + intro markdown cell** — explains the notebook inspects `search_db.sqlite` and `.cache/base_extraction.sqlite`.

2. **Setup cell** — imports (`sqlite3`, `pandas`, `pathlib.Path`, `json`), resolves `PROJECT_ROOT` the same way `duckduckgo.ipynb` does (`os.path.abspath(os.path.join(os.getcwd(), "..", ".."))`), builds paths to `search_db.sqlite` and `.cache/base_extraction.sqlite`, opens both as read-only sqlite3 connections (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`), sets `pd.set_option("display.max_colwidth", ...)` for readability.

3. **"## Summary — row counts" markdown header + cell** — one cell that, for every table in both DBs (queried via `sqlite_master` so it's not hand-maintained, filtered to `type='table'` and excluding `sqlite_sequence`), runs `SELECT COUNT(*)` and displays a single summary DataFrame with columns `database`, `table`, `row_count`, sorted by database then table. This is the first thing the user sees.

4. **Per-table sections**, one markdown header (`## <db> — <table>`) + one code cell each, in this order:
   - `.cache/base_extraction.sqlite` → `base_cache` (show `.head(20)` given 224 rows, plus shape)
   - `search_db.sqlite` → `runs`, `tasks`, `attempts`, `node_events`, `candidates`, `llm_calls`, `meta` (show full table via `pd.read_sql_query`, since current row counts are tiny — no truncation needed, but still print shape for when it grows)
   - Each cell: `pd.read_sql_query("SELECT * FROM <table>", conn)`, print `df.shape`, then `display(df)`.

5. **Views section** — `## search_db.sqlite — views` markdown header, then one cell per view (`v_errors`, `v_task_result`, `v_funnel`, `v_run_summary`) querying `SELECT * FROM <view>` the same way, since these give the most human-readable rollups (funnel, run summary, task results, errors).

6. **Final markdown cell** — closes both connections (`conn.close()`), brief note this is a read-only inspection notebook.

## Conventions to match

- Same `PROJECT_ROOT`/`sys.path` bootstrap pattern as `src/search/duckduckgo.ipynb`.
- No plotting libs — plain pandas DataFrame display, consistent with existing notebook style in this module.
- Open DBs read-only (`mode=ro`) so running the notebook can never mutate trace data.

## Verification

- Execute the notebook top-to-bottom with `jupyter nbconvert --to notebook --execute` (or open in the IDE and Run All) and confirm: the summary cell lists all 8 tables (`base_cache`, `runs`, `tasks`, `attempts`, `node_events`, `candidates`, `llm_calls`, `meta`) with correct counts (224 / 1 / 1 / 1 / 5 / 10 / 1 / 1 against current data), every per-table and per-view cell renders without error, and no exceptions are raised (e.g. missing columns, locked DB).

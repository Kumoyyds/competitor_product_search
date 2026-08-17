# Populate `check_database.ipynb` with database-review code

## Context

Need to inspect what's actually stored in the project's database(s) and write review code into `src/scraping/scripts/check_database.ipynb` so the data can be browsed conveniently (per table, with drill-down for large fields) instead of querying ad hoc.

Investigation of the repo found:

- **Exactly one real database exists**: `scraping.db` at the repo root (SQLite), managed by `src/scraping/storage/database.py::ScrapeDB`, with **6 tables**: `parsers`, `golden_samples`, `scrape_runs`, `results`, `escalations`, `invalid_target_phrases` (full DDL in that file).
- **`src/storage/`** (the top-level module intended for finalized search results) is a **documented skeleton** — `main_db.py`/`temp_db.py`/`trash_bin.py` are TODO-only docstrings, no schema, no DB file. Its own `CLAUDE.md` says "Status: Skeleton (not yet implemented)". So there is nothing to query there yet.
- Both `check_database.ipynb` and `scraping.db` are gitignored (`*.ipynb`, `*.db` in `.gitignore`) and are currently **0 bytes** — the notebook is a blank placeholder, and the DB has never had `init_db()` run against it locally (no tables yet). The notebook code should still be fully correct and ready for when it's populated (and will create the empty schema on connect so it doesn't error on a fresh DB).
- The per-table `*Store` classes (`ParserStore`, `GoldenStore`, `ResultStore`, `EscalationStore`, `RunStore`, `PhraseStore` in `src/scraping/storage/`) are all narrowly scoped (site-filtered, purpose-built for the app's own logic), not a "dump everything" fit for a review notebook — so the notebook uses `ScrapeDB`'s connection directly with plain `SELECT *` + pandas for the main browsing views, and additionally demonstrates a couple of the store classes for common filtered questions (open escalations, best parser per site) since those are genuinely convenient and keep with the project's reuse-existing-code convention.
- `pandas` (3.0.3) is already available in the project's `.venv` (per `requirements.txt`) — used for tabular display. Note: the registered Jupyter kernel is currently only the Anaconda `python3` one; the user will need to pick the `.venv` interpreter as the notebook's kernel in VSCode for imports to resolve (flagged as a note in the notebook, not something fixable from the file itself).

## Approach

Write `src/scraping/scripts/check_database.ipynb` directly as a complete `.ipynb` v4 JSON document (via `Write`, since the file is currently invalid/empty JSON — there's nothing for a cell-level notebook editor to anchor into). Structure it as alternating markdown/code cells, one section per table, so the user can run/scroll/re-run table-by-table.

**Cell plan:**

1. **Markdown** — title + scope note: this notebook covers `scraping.db` (the only implemented DB); `src/storage/` is still a skeleton with nothing to inspect yet.
2. **Code — setup**: imports (`sqlite3`, `json`, `pandas as pd`, `Path`), robust repo-root resolution (walk up from `Path.cwd()` looking for `.git` + `config.yaml`, since notebook cwd varies by launcher), insert repo root onto `sys.path` (needed for the `from src.scraping...` absolute import, per this repo's import convention), resolve `DB_PATH = REPO_ROOT / "scraping.db"`, print the resolved path/size so it's obvious if it's empty.
3. **Code — connect + overview**: `from src.scraping.storage import ScrapeDB`; open it, call `init_db()` (idempotent `CREATE TABLE IF NOT EXISTS`, safe even on the current empty file — makes the rest of the notebook work whether or not a scrape has run yet); set a couple of pandas display options; print a one-row-per-table summary (`sqlite_master` table list + `COUNT(*)` per table) as a quick-glance DataFrame.
4. **Markdown + Code — `scrape_runs`**: raw `SELECT * ... ORDER BY id DESC` as a DataFrame (the operational log — what URL, path taken, outcome, latency, cost), plus an enriched view that LEFT JOINs `parsers` to show the winning parser's site/version instead of a bare id.
5. **Markdown + Code — `results`**: raw view (with `product_data` truncated for the overview), plus a flattened view via `pd.json_normalize` on `product_data` (matches the `ProductData` pydantic model: title/brand/price/currency/list_price/membership_price/in_stock/availability_raw/...) joined back with id/url/scraped_at — this is the main "actual scraped data" table so it gets the most reviewable treatment.
6. **Markdown + Code — `parsers`**: DataFrame with the `code` column truncated in the overview (full generated-code text is long).
7. **Markdown + Code — `golden_samples`**: DataFrame with `html_snapshot`/`expected_output` truncated, plus a flattened `expected_output` view.
8. **Markdown + Code — `escalations`**: raw DataFrame (`snapshot` truncated), plus `EscalationStore(db).get_open()` for the filtered "needs attention now" view.
9. **Markdown + Code — `invalid_target_phrases`**: straight DataFrame (small lookup table).
10. **Markdown + Code — drill-down helper**: a small `show_blob(table, row_id, column)` function that fetches and pretty-prints one full field (JSON pretty-printed when applicable, else raw text) — for reading a full parser's code, a full HTML snapshot, or a full `product_data`/`snapshot` blob without the truncation used in the overview tables.
11. **Markdown + Code — handy filtered queries (bonus)**: a `SITE = "tesco"` variable plus `ParserStore(db).get_active_ordered_by_hits(SITE)` and `RunStore(db).get_hit_rates(SITE)` as examples of reusing the app's own store classes for common per-site questions.

All `SELECT *` cells use plain SQL + `pd.read_sql_query(sql, db.conn)`, reusing `ScrapeDB` only for connection setup (WAL mode / `row_factory` already configured there) — not the narrow store methods, which don't fit a "show everything" use case.

## Critical files

- `src/scraping/scripts/check_database.ipynb` — the file being written (currently empty)
- `src/scraping/storage/database.py` — `ScrapeDB` + full DDL for all 6 tables (source of truth for schema)
- `src/scraping/storage/__init__.py` — confirms the store-class import surface (`from src.scraping.storage import ScrapeDB, EscalationStore, ParserStore, RunStore, ...`)
- `src/scraping/models/product_data.py` — `ProductData` fields, used to decide what `json_normalize` surfaces from `results.product_data`
- `src/scraping/scripts/reseed_goldens.py` — existing precedent script for locating `scraping.db` from within `scripts/`

## Verification

- After writing, open the notebook and confirm it's valid JSON (`python -c "import json; json.load(open('check_database.ipynb'))"`).
- Run it end-to-end non-interactively with the project's venv to confirm every cell executes without error against the current (empty-schema) `scraping.db`: `.venv/bin/jupyter execute --inplace src/scraping/scripts/check_database.ipynb` (or `nbconvert --to notebook --execute`). Expect all tables to show 0 rows (DB is currently empty) but no exceptions.
- Spot-check that the summary cell lists all 6 expected table names, and that the `results`/`golden_samples` flattening cells don't error when the tables are empty (i.e., `json_normalize` on an empty list).

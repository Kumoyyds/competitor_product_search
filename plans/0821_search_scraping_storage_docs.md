# Storage schema reference docs + auto-sync pre-commit hook

## Context

Neither module has a column-level schema reference. What exists today is scattered and partly stale:

- **scraping** — `src/scraping/README.md:281-292` lists the six tables with a one-line purpose each; `scraping_module_spec_v1_2.md:393-449` has field-name-only tables that are already wrong (`scrape_runs` there is missing `signature` / `error` / `escalation_id`, and its `path` enum lists `fallback_scraper`, which the DDL does not have). `docs/scraping_design.md` explains *who writes which table* but never the columns.
- **search** — nothing at all. `src/search/README.md` and `CLAUDE.md` only note that `db.py` / `trace.py` do "SQLite run/task persistence". The only schema copies live in stale plan files (`src/search/plans/0811_db_building.md`).

Nowhere is there types, NOT NULL / DEFAULT / CHECK / UNIQUE / FK, index lists, relationship maps, or enum value sets. And nothing keeps any of it honest — the spec drifted silently over M13–M28.

The fix is two generated reference documents plus a pre-commit step, so schema and docs can never diverge again: `docs/scraping_storage.md` and `docs/search_storage.md` are rebuilt from the real DDL on every commit, and a column added without a semantic description blocks the commit.

**Decisions taken with the user:** column meanings live as `--` comments inline in the DDL (SQLite ignores them; zero runtime effect, and the description sits at the column definition so it is hardest to forget). The hook rewrites + stages the docs like `gen_capability_docs.py` does, and hard-fails when a column has no comment. Documents are written in English, matching `docs/architecture.md` and `docs/scraping_design.md`.

## Design

### 1. Comment convention (the new source of truth for "字段含义")

Two accepted forms inside the DDL string literals, both parsed by the generator:

```sql
CREATE TABLE IF NOT EXISTS scrape_runs (          -- table purpose goes on the lines *above* CREATE
    id INTEGER PRIMARY KEY AUTOINCREMENT,          -- Surrogate PK; referenced by results.run_id
    site TEXT NOT NULL,                            -- Retailer key (tesco/argos/amazon), not the host — the aggregation key (D6)
    -- Terminal outcome of this execution. 'escalated' rows also carry signature + error.
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'escalated', 'invalid_target')),
    ...
);
```

- Trailing `-- …` on the column line, **or** one-or-more `-- …` lines immediately above it (used for the long `CHECK(...)` columns, where a trailing comment would blow the line length). Both present → joined.
- Comment lines immediately above `CREATE TABLE` / `CREATE VIEW` become that object's purpose paragraph.
- Indexes and views need a purpose comment; view columns do **not** need per-column comments (they are derived).

### 2. `scripts/gen_storage_docs.py` (new)

Modeled directly on `scripts/gen_capability_docs.py` — same CLI (`--root`, `--pre-commit`, `--check`, mutually exclusive), same `MARKER_RE` `<!-- BEGIN GENERATED: id -->` region injection, same `stage()` = write file then `git add -- <rel>`, same `fail()` / `warn()` / `repo_root()` helpers. Deliberately **no PyYAML dependency**, so this hook step never degrades the way the capability step does.

Pipeline:

1. **Extract DDL without importing app code** — `ast` walk for module-level string assignments (the `_literal_assignment` idea from `gen_capability_docs.py:139-159`, copied in so the script stays self-contained):
   - `src/scraping/storage/database.py` → `_DDL`, `_INDEX_DDL`, plus `_ADDED_COLUMNS`, `_RENAMED_COLUMNS`, `_REMOVED_COLUMNS` for the migration section.
   - `src/search/db.py` → `_DDL`, `_INDEX_DDL`, `_VIEW_DDL`, `SCHEMA_VERSION`.
   AST extraction (not import) keeps the hook fast (~0s vs 1.2s for `import src.search.db`) and free of `yaml`/`dotenv`.
2. **Introspect authoritatively** — `executescript` the DDL into `sqlite3.connect(":memory:")`, then `sqlite_master`, `PRAGMA table_info` (type / notnull / dflt_value / pk), `PRAGMA foreign_key_list` (target table+column, `on_delete`), `PRAGMA index_list` + `index_info` (columns, unique, origin `c`/`u`/`pk`). CHECK constraints and `AUTOINCREMENT` are not exposed by PRAGMA, so those come from the already-parsed per-column DDL text.
3. **Merge** structure + comments; a table column with no comment is collected as an error.
4. **Render** per table:
   - purpose paragraph
   - `| Column | Type | Nullable | Default | Key / Constraints | Meaning |`
   - indexes (name, columns, unique) and declared foreign keys with their `ON DELETE` action
   - a mermaid `erDiagram` of the declared FK graph for the whole database
   - a migration section: scraping renders `_ADDED_COLUMNS` / `_RENAMED_COLUMNS` / `_REMOVED_COLUMNS` and how `ScrapeDB._ensure_columns()` applies them; search renders `SCHEMA_VERSION` + the `meta` row + the `runs.mode` additive `ALTER`.
5. **Consistency check** (cheap, catches a real footgun): every column named in `_ADDED_COLUMNS` must also exist in `_DDL`, else a fresh database would be missing it. Fail if not.
6. **Inject + write/stage or `--check`**, exactly as `gen_capability_docs.update_docs()` does.

### 3. Document layout

Generated regions carry the mechanical truth; hand-written prose outside the markers carries what code cannot state. Block ids: `scraping-tables`, `scraping-er`, `scraping-migrations`, `search-tables`, `search-er`, `search-migrations`.

Hand-written sections to author now, from the research already done:

- **scraping** — `site` is the undeclared partition key across all six tables and what `ScrapeDB.clear_site()` (`database.py:235-325`) deletes on; `escalations` has **no `site` column**, so `clear_site` derives it by string-parsing the signature's first `|`-segment (`database.py:308-318`) — a hard convention any new signature format must honor; parser hit rate is never stored but derived live by `ParserStore.get_active_ordered_by_hits()` (D17); `results.site` is populated from `ProductData.website`; dead columns worth flagging: `parsers.page_type_scope` (never written), `scrape_runs.attempts` (always default 1), and `path='retried'` (in the CHECK, never written).
- **search** — the strict `runs → tasks → attempts → {node_events, candidates, llm_calls}` cascade; `run_id` / `task_id` are denormalized onto the leaf tables with no FK, purely for per-run filtering; JSON-blob columns and their exact shapes (`job_config`, `pipeline_config`, `layer_trace`, `query_variants`, `brands`, `numerics`, `detail`); `runs.matched_count` / `no_match_count` / `error_count` are aggregated from `tasks` at `finish_run` time, not incremented; `candidates` rows are skipped entirely when `db.store_candidates` is false and `llm_calls.prompt` / `raw_response` are NULLed when `db.store_llm_payload` is false; the four views.
- **Both** — enum value sets per column (`tasks.failure_kind`'s 12 values, `node_events.error_kind` being open-ended rather than a closed set, etc.), a "how to add a column" checklist, and reusable example queries (move the scraping ones from `README.md:294-322`).

### 4. Hook wiring

One line in `.githooks/pre-commit`, placed after `gen_capability_docs.py` and **before** `check_encoding.py` so the encoding check sees the newly staged docs:

```sh
"$PY" "$ROOT/scripts/gen_storage_docs.py" --pre-commit --root "$ROOT" || exit 1
```

No new hook file, no `.claude/settings.json` hook — this is exactly the job the existing `core.hooksPath .githooks` chain already does, so nothing extra to install per clone.

## Files

**Create**
- `scripts/gen_storage_docs.py`
- `docs/scraping_storage.md`, `docs/search_storage.md`
- `tests/unit/test_gen_storage_docs.py` — mirrors `tests/unit/test_gen_capability_docs.py`: unit tests for comment parsing (trailing / preceding / both / missing), PRAGMA→row rendering, marker injection idempotence, and a `test_real_docs_are_fresh()` that runs `--check` against the repo and asserts exit 0.

**Modify**
- `src/scraping/storage/database.py` — add `--` comments to all ~40 columns in `_DDL` and the 10 index lines in `_INDEX_DDL`. No structural change.
- `src/search/db.py` — hoist the inline `executescript` literal out of `_init_schema` (currently `db.py:62-247`) into module-level `_DDL` / `_INDEX_DDL` / `_VIEW_DDL`, mirroring the scraping module's shape, and add the same `--` comments. `_init_schema` then runs the three scripts in the same order it does today (tables → indexes → views), keeping the `ALTER TABLE runs ADD COLUMN mode` probe and the `meta` upsert (`db.py:248-259`) untouched. Behavior-neutral.
- `.githooks/pre-commit` — one line, per §4.
- `CLAUDE.md` (`AGENTS.md` follows via the sync hook) — add the generator to the pre-commit chain description, and add a Code Conventions rule: *a new column in either DDL must carry a `--` comment; the pre-commit hook rejects the commit otherwise.*
- `README.md` — the hook now runs three generators/checks, and link the two new docs from the documentation list near `README.md:108`.
- `src/scraping/README.md` — replace the §Storage six-row table with a pointer to `docs/scraping_storage.md` (keeping the example queries or moving them into the new doc).
- `src/search/README.md` — add a Storage pointer (it currently has none).
- `docs/scraping_design.md:10-16` — add the new doc to the "four documents" chooser table.

Not touched: `scraping_module_spec_v1_2.md` stays frozen; its §6 drift is already called out in `docs/scraping_design.md:21-35`, and the new doc supersedes it.

## Verification

```bash
# 1. Generator agrees with the DDL, and is idempotent
python scripts/gen_storage_docs.py --check          # exit 0, nothing stale
python scripts/gen_storage_docs.py && git diff --stat docs/   # empty

# 2. Unit tests
python -m pytest tests/unit/test_gen_storage_docs.py tests/unit/test_gen_capability_docs.py -v

# 3. The DDL comments did not break either schema (both build clean, in-memory)
python -c "import sqlite3, src.scraping.storage.database as d; c=sqlite3.connect(':memory:'); c.executescript(d._DDL); c.executescript(d._INDEX_DDL); print('scraping ok')"
python -c "from src.search.db import SearchDB; SearchDB('/tmp/x.db'); print('search ok')"

# 4. Existing suites still pass (schema-touching tests)
python -m pytest tests/unit/scraping/test_run_correlation.py tests/unit/scraping/test_clear_site.py tests/unit/search -q

# 5. Hook end-to-end — add a throwaway column WITHOUT a comment, expect the commit to be blocked
#    then add the comment, expect the doc to be regenerated and staged automatically
git add src/scraping/storage/database.py && git commit -m "probe"   # must fail with the missing-comment error
```

Also confirm against the live databases that the generated docs describe reality: `sqlite3 scraping.db '.schema'` and `sqlite3 search.db '.schema'` should match the generated column lists (modulo the migration columns the docs describe).

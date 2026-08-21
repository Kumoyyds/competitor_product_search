# Scraping DB — run correlation keys

## Context

The scraping module's six SQLite tables have exactly one foreign key between them:
`scrape_runs.winning_parser_id → parsers(id)`. `results` and `escalations` have none.
That leaves two real observability holes:

1. **`results` cannot name the run that produced it.** `RunStore.record()` already returns
   the new `scrape_runs.id`, but `BaseScraper._record_run` throws the return value away
   ([base.py:60](../../../programming/competitor_product_search/src/scraping/scrapers/base.py#L60)).
   `_store_result` then opens a *separate* connection and appends a `results` row with no
   link. Today the only way to correlate is fuzzy `(url, site, scraped_at)` matching —
   which is exactly what `scripts/live_batch_report.py:376` is forced to do
   (`WHERE url = ? ORDER BY id DESC LIMIT 1`).

2. **`escalations` loses every occurrence after the first.** The signature
   `{site}|{field_or_rule}|{parser_version}` has an empty `parser_version` at every raise
   site, so in practice a site only ever produces 4–6 distinct signatures, and
   `escalations.signature` is UNIQUE. On a repeat, `EscalationStore.upsert` runs
   `UPDATE escalations SET affected_count = ?` and **nothing else** — the snapshot stays
   frozen on the first failure forever. 100 failing Tesco URLs → one row, `affected_count
   = 100`, and a snapshot describing URL #1. The per-URL detail is not actually lost (M24
   added `scrape_runs.signature` + `scrape_runs.error`), it is just unjoinable.

Point 2 of the original report — "can `winning_parser_id` be the key into `parsers`?" — is
already true structurally: it is declared `INTEGER REFERENCES parsers(id)` and `parsers.id`
is the PK. The defect is **population**: only the fast path sets it
([html_scraper.py:122](../../../programming/competitor_product_search/src/scraping/scrapers/html_scraper.py#L122)).
`run_repair_ladder` returns `outcome.product` and drops `CandidateSucceeded.parser_id`
([agent.py:179](../../../programming/competitor_product_search/src/scraping/repair/agent.py#L179)),
so a freshly promoted parser is credited **zero** hits for the very scrape that created it —
biasing `get_active_ordered_by_hits` and `_prune_hard_cap` against new parsers.

**Outcome:** `scrape_runs.id` becomes the canonical run key that stitches all three tables
together, `winning_parser_id` is populated on every HTML success, and `scrape_runs` becomes
a true per-execution log.

## Decisions taken

- Link direction is **run → escalation** (`scrape_runs.escalation_id`), not the reverse.
  `escalations` is deliberately a `UNIQUE(signature)` aggregate/alarm (D24); a `run_id`
  column on it could only ever hold one of N runs.
- **No `trace_id`** grouping the router fallback chain, and **no change to escalation
  snapshot/last-seen semantics**. Out of scope for this pass.
- Success-run dedup is **removed** — every execution writes a run row, so every result
  correlates. Consequence: `scrape_runs_dedup_window_seconds` becomes dead and is deleted.
- Direct API extraction (including Amazon) is still a scraper execution and must write a
  `scrape_runs` row even though it does not use an HTML parser.
- Rename `scrape_runs.model_used` to the narrower `repair_model`: only HTML parser repair
  and API JSON healing populate it, using the actual configured LLM. Remove the unused
  `cost` column.
- Keep the `results` notebook view raw. `results.run_id` is the sole direct link to
  `scrape_runs.id`; run path/scraper/outcome/parser are queried from that table only when
  needed, rather than duplicated as JOIN-derived display columns.

---

## 1. Schema — `src/scraping/storage/database.py`

Two new nullable FK columns, one renamed column, and one removed column:

```sql
results.run_id          INTEGER REFERENCES scrape_runs(id)  ON DELETE SET NULL
scrape_runs.escalation_id INTEGER REFERENCES escalations(id) ON DELETE SET NULL
scrape_runs.repair_model   TEXT  -- renamed from model_used
-- scrape_runs.cost is removed
```

- Add both to `_DDL`, and reorder the `CREATE TABLE` blocks to
  `parsers → golden_samples → escalations → scrape_runs → results → invalid_target_phrases`
  so every FK points at an already-declared table (readability only — SQLite resolves FKs
  at DML time, not DDL time).
- Add both to `_ADDED_COLUMNS` so historical DBs migrate through the existing
  `_ensure_columns()` path. **Verified**: SQLite accepts
  `ALTER TABLE … ADD COLUMN … REFERENCES … ON DELETE SET NULL` when the default is NULL, and
  `ON DELETE SET NULL` fires correctly afterwards. No table rebuild needed.

### Index ordering — must fix

`init_db()` runs `executescript(_DDL)` **before** `_ensure_columns()`
([database.py:127-129](../../../programming/competitor_product_search/src/scraping/storage/database.py#L127-L129)).
A `CREATE INDEX … ON results(run_id)` left inside `_DDL` would therefore raise on a legacy
DB, because the column does not exist yet at that point.

Split all index statements out of `_DDL` into a new `_INDEX_DDL` constant and run it
**after** `_ensure_columns()`. Add:

```sql
CREATE INDEX IF NOT EXISTS idx_results_run           ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_escalation ON scrape_runs(escalation_id);
```

### `clear_site` — explicit detach

`test_clear_site.py` already covers `clear_site` running with `foreign_keys=OFF`, where
`ON DELETE SET NULL` does **not** fire. So add explicit detach `UPDATE`s inside the existing
`BEGIN IMMEDIATE` block, mirroring the `winning_parser_id` precedent at
[database.py:204-209](../../../programming/competitor_product_search/src/scraping/storage/database.py#L204-L209):

- before `DELETE FROM scrape_runs`: `UPDATE results SET run_id = NULL WHERE run_id IN (SELECT id FROM scrape_runs WHERE site = ?)` → report as `counts["results_detached"]`
- before `DELETE FROM escalations`: `UPDATE scrape_runs SET escalation_id = NULL WHERE escalation_id IN (SELECT id FROM escalations WHERE substr(signature,1,instr(signature||'|','|')-1) = ?)` → `counts["scrape_runs_escalation_detached"]`

Note the ordering constraint: the `results` detach must run *before* the `scrape_runs`
DELETE that currently sits first in the block.

## 2. Stores — `src/scraping/storage/`

- `result_store.py` — `append(self, product: ProductData, run_id: Optional[int] = None) -> int`; add the column to the INSERT.
- `run_store.py`:
  - `__init__(self, db)` — drop `dedup_window_seconds`; delete `is_duplicate()` (its only caller is being removed).
  - new `attach_escalation(self, run_ids: Sequence[int], escalation_id: int) -> int` —
    `UPDATE scrape_runs SET escalation_id = ? WHERE id IN (…)`, returns `rowcount`. No-op on empty input.
  - new `get_by_escalation(self, escalation_id: int) -> list[dict]` — the read the notebook needs.

## 3. Propagation

**`src/scraping/exceptions.py`** — append `run_id: Optional[int] = None` as the **last**
field of the `ScrapeFailed` dataclass (after `errors`), so existing positional/keyword
construction at all six raise sites is unaffected. This is the channel that carries the run
id back to the router, which otherwise sees nothing but the exception.

**`src/scraping/scrapers/base.py`**
- `_record_run(...) -> Optional[int]` — return `store.record(...)`; **delete** the
  `if outcome != "success" or not store.is_duplicate(url)` guard at
  [base.py:59](../../../programming/competitor_product_search/src/scraping/scrapers/base.py#L59).
  Returns `None` only if the write itself raised (still swallowed + logged).
- `_record_failure(...) -> Optional[int]` — return the id and set `failure.run_id = run_id`.
- `_store_result(self, product, run_id: Optional[int] = None)` — forward to `ResultStore.append`.

**`src/scraping/scrapers/html_scraper.py`** — capture the returned id at every
`_record_run` / `_record_failure` call and thread it onward:
- fast-path success ([:120-125](../../../programming/competitor_product_search/src/scraping/scrapers/html_scraper.py#L120-L125)) → `self._store_result(product, run_id)`
- agent-repaired success ([:151-156](../../../programming/competitor_product_search/src/scraping/scrapers/html_scraper.py#L151-L156)) → `_store_result(product, run_id)` **and** `winning_parser_id=` the promoted parser (see §4)
- both `invalid_target` sites → pass `run_id` into `_check_mass_invalid_target(host, url, run_id)` so the surge ticket attaches to the run that tripped it
- `_check_mass_invalid_target` ([:286-320](../../../programming/competitor_product_search/src/scraping/scrapers/html_scraper.py#L286-L320)) — `RunStore(db)` (drop the config arg), and after `EscalationStore.upsert` returns an id, `RunStore(db).attach_escalation([run_id], esc_id)` on the same connection

**`src/scraping/scrapers/api_scraper.py`** — same treatment at
[:103-104](../../../programming/competitor_product_search/src/scraping/scrapers/api_scraper.py#L103-L104) and
[:117-125](../../../programming/competitor_product_search/src/scraping/scrapers/api_scraper.py#L117-L125).
`winning_parser_id` stays NULL on this route — correct, the API route has no parser.

## 4. `winning_parser_id` on the repair path

**`src/scraping/repair/agent.py`** — widen the `run_repair_ladder` return union to include
`CandidateSucceeded` and change [:179](../../../programming/competitor_product_search/src/scraping/repair/agent.py#L179)
from `return outcome.product` to `return outcome`. `CandidateSucceeded` already carries
`product` + `parser_id` + `parser_source` ([:89-92](../../../programming/competitor_product_search/src/scraping/repair/agent.py#L89-L92)).

`html_scraper.py` then branches on `CandidateSucceeded` instead of `ProductData`, unpacking
`outcome.product` / `outcome.parser_id`. Do **not** look the parser up by
`ProductData.parser_version` — that value is `agent_attempt_N`
([agent.py:271](../../../programming/competitor_product_search/src/scraping/repair/agent.py#L271)), which is
unrelated to `parsers.version` from `_next_version()` and not unique across scrapes.

The legacy `verify_m8.py` assertion must unwrap `CandidateSucceeded.product`.
The referenced `verify_m6.py`, `verify_m24.py`, `verify_m25.py`, and
`verify_m4_m5.py` cases return `None` or `ScrapeFailed`, so they do not require a
return-type update. No defensive dual-branch is added to production code. Separately,
`verify_m1_m3.py` and `verify_m24.py` must replace their success-dedup expectations with
one-row-per-execution expectations.

## 5. Router — `src/scraping/router.py`

- Change `_write_escalation(signature, reason, snapshot, run_ids: Sequence[int] = ())` to
  capture `EscalationStore.upsert`'s returned id and call
  `RunStore(db).attach_escalation(run_ids, esc_id)` on the **same** connection before closing.
  Still fully best-effort inside the existing try/except.
- At the exhaustion site ([:93-109](../../../programming/competitor_product_search/src/scraping/router.py#L93-L109)) pass
  `run_ids=[f.run_id for f in failures if f.run_id is not None]`.
- The `BrightDataInfraError` site ([:70-80](../../../programming/competitor_product_search/src/scraping/router.py#L70-L80)) passes
  no run ids — both scrapers convert that error to `ScrapeFailed` internally, so this branch
  is defensive and has no run row to attach.

## 6. Config — `src/scraping/config.py`

Delete `scrape_runs_dedup_window_seconds` ([config.py:99-100](../../../programming/competitor_product_search/src/scraping/config.py#L99-L100)) and its
`# --- dedup ---` comment. After §3 nothing reads it.

## 7. Docs (mandatory per CLAUDE.md — config key removed + schema changed)

- **`src/scraping/README.md`**
  - §Database table (~L283-312): document `results.run_id` and `scrape_runs.escalation_id`;
    add the two join queries (result → producing run; ticket → affected URLs) alongside the
    existing example SQL; extend the migration note at L312 to name the new columns and state
    that historical rows keep NULL.
  - L78: rewrite the dedup half-sentence — `scrape_runs` is now one row per execution.
  - L342: remove the `scrape_runs_dedup_window_seconds` config-table row.
  - L152 (clear-site guidance): note that clearing `scrape_runs` detaches `results.run_id`
    and clearing `escalations` detaches `scrape_runs.escalation_id`.
- **`src/scraping/CLAUDE.md`** — new M28 section (schema/plumbing summary + the
  `winning_parser_id` repair fix). The pre-commit hook syncs `AGENTS.md`.
- **`src/scraping/scripts/check_database.ipynb`** — cell 5 (`scrape_runs`) add a LEFT JOIN to
  `escalations`; cell 7 (`results`) displays the raw table and documents `run_id` as its only
  direct relationship; cell 13 (`escalations`) adds an affected-URL drill-down via
  `RunStore.get_by_escalation`.
  `REVIEW_TABLES` in cell 17 is unchanged (no new tables).
- **`src/scraping/scripts/live_batch_report.py:376`** — replace the
  `WHERE url = ? ORDER BY id DESC LIMIT 1` guess with a lookup keyed on the run id now
  reachable from the result row.
- Root `README.md` needs no change (no CLI/entry-point/root-config delta).

## 8. Tests

New `tests/unit/scraping/test_run_correlation.py`, using the existing
`tests/_support/db.py` helpers (`temp_scrape_db`, `fetchall`) and offline mocks per the
module's verification discipline:

- HTML fast path, HTML agent-repaired, and API path each write a `results` row whose
  `run_id` equals the `scrape_runs.id` written in the same call
- agent-repaired success sets `winning_parser_id` to the promoted parser id (regression
  guard for §4)
- two successes on the same URL within 3600s now write **two** run rows, each with its own
  result — the dedup removal
- router exhaustion attaches one `escalation_id` to every failed run row in the fallback chain
- `_check_mass_invalid_target` attaches its surge ticket to the tripping run
- legacy-DB migration: build a DB from the pre-change DDL, run `init_db()`, assert both
  columns and both indexes appear and are idempotent on a second `init_db()`
- `clear_site` detaches `results.run_id` and `scrape_runs.escalation_id` with
  `foreign_keys` both ON and OFF

Update `tests/unit/scraping/test_clear_site.py`: `_seed` (raw SQL inserts) gains the new
columns, and `_schema_snapshot` expectations shift with the new columns/indexes.

## Verification

```bash
# unit suite (offline, excludes live/slow)
python -m pytest tests/unit/scraping -v

# legacy migration scripts still green after the API/semantics changes
# (run as modules because the scripts use package-relative imports)
python -m src.scraping.tests.verify_m1_m3
python -m src.scraping.tests.verify_m24
python -m src.scraping.tests.verify_m25

# optional paid/live return-type check (calls the configured LLM)
python -m src.scraping.tests.verify_m8

# real DB migrates in place, no rebuild, no data loss
cp scraping.db /tmp/scraping.db.bak
python -c "from src.scraping.storage import ScrapeDB; d=ScrapeDB('scraping.db'); d.init_db(); \
print([r['name'] for r in d.conn.execute('PRAGMA table_info(results)')]); \
print([r['name'] for r in d.conn.execute('PRAGMA table_info(scrape_runs)')]); \
print(d.conn.execute('SELECT COUNT(*) FROM results').fetchone()[0]); d.close()"

# optional paid/live end-to-end on one URL, then confirm the three tables join
python -c "import asyncio; from src.scraping import scrape; \
print(asyncio.run(scrape('https://www.argos.co.uk/product/3284476')))"
sqlite3 scraping.db "SELECT r.id, r.run_id, s.path, s.scraper, s.winning_parser_id, p.version \
FROM results r LEFT JOIN scrape_runs s ON s.id = r.run_id \
LEFT JOIN parsers p ON p.id = s.winning_parser_id ORDER BY r.id DESC LIMIT 5;"

# encoding hook precondition
python3 scripts/check_encoding.py --all
```

Manual check: open `src/scraping/scripts/check_database.ipynb`, run all cells, and confirm
the joined `scrape_runs` / `results` / `escalations` views render for pre-existing rows
(which will show NULL `run_id` / `escalation_id` — expected, historical rows are not
back-filled).

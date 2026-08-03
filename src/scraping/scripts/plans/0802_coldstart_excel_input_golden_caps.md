# Cold start: Excel input + config-driven page_type policy + golden-set caps

## Context

Cold start ([coldstart.py](src/scraping/coldstart.py), M11) is the one manual step in a new site's lifetime: the user supplies product URLs, an LLM writes the first parser, the human confirms each extraction, and confirmed outputs become that site's first golden samples. Every later automatic promotion is judged against those goldens (spec D22).

Today the input is a bare `urls.txt` (one URL per line, [coldstart.py:241](src/scraping/coldstart.py#L241)). Three consequences:

1. **No page-type signal.** Golden buckets are labelled by `classify_page_type(product)` ([coldstart.py:228](src/scraping/coldstart.py#L228)) — i.e. by what the *unverified first parser* extracted. If that parser misses a struck-through was-price, the row silently lands in `standard` and the `discounted` bucket stays empty forever.
2. **No coverage guarantee.** A site can be cold-started with five `standard` URLs; the gaps only surface much later as bad promotions.
3. **No sample-count policy.** `promote_min_samples_per_page_type = 1` sits in [config.py:62](src/scraping/config.py#L62) but is dead code (grep: defined, never read), and `maybe_seed_golden()` ([golden.py:78](src/scraping/repair/golden.py#L78)) only seeds when a bucket is *empty* — so every bucket holds exactly 1 sample forever, against spec §5.7's capacity plan of "每 site 4 页型 × 每型 ~3 样本".

This change moves the whole policy into `config.py` — which page types a cold start must cover, and how many goldens a bucket keeps — makes the cold-start input a validated Excel sheet declaring each URL's page type, and adds a deliberate shrink path for when the cap is lowered.

Decisions confirmed with the user:

- Policy lives in config as `cold_start_page_require_mandatory: {page_type: bool}`; the per-bucket **minimum is derived** from it (mandatory → 1, optional → 0), and the maximum is a separate scalar knob.
- The **declared** `page_type` labels the golden bucket; a classifier disagreement shows as a `MISMATCH` warning in the review prompt so the human can reject with `n`.
- The caps are **global** — cold start *and* runtime `maybe_seed_golden()`.
- If the human rejects every row of a mandatory type, cold start **warns, seeds what was accepted, and exits non-zero**.
- Shrinking is an **explicit dry-run script**, never an automatic delete, and **human-confirmed goldens are evicted last**.

### Engineering review amendments

- `golden_max_samples_per_page_type >= 1` means an optional bucket is not normally pruned to zero; optional means its coverage minimum is zero. Verification therefore checks that optional coverage does not block pruning to the configured cap, rather than the contradictory “optional bucket may reach 0” case.
- `add_membership_bucket` is made provenance-aware so either migration order preserves `created_by`; otherwise running the older table-rebuild migration after the new column migration would lose or conflict with the column.
- Live smoke testing found that cold start hard-coded `qwen-3.7-plus`, while the configured repair ladder uses `qwen3.7-plus`; cold start now uses `repair_model_ladder[0]` so the model name has one source of truth.

## Changes

### 1. Policy knobs — [config.py](src/scraping/config.py)

```python
# --- cold start / golden set page_type policy ---
# Which page_types a cold-start input sheet MUST cover, and (by derivation) the
# minimum number of golden samples each bucket retains:
#   True  -> >=1 URL required at cold start, bucket keeps >=1 golden
#   False -> optional at cold start, bucket may sit at 0
# A page_type absent from this map is treated as MANDATORY — fail-safe, so a
# partial env override can never silently drop a requirement.
cold_start_page_require_mandatory: dict[str, bool] = Field(
    default_factory=lambda: {
        "standard":     True,
        "discounted":   True,
        "out_of_stock": True,
        "membership":   True,
        "multipack":    False,
    }
)

# Upper bound on non-stale golden samples kept per (site, page_type).
golden_max_samples_per_page_type: int = 3
```

Accessors on `ScrapingConfig`, so no caller re-derives the rule:

| Method | Returns |
|---|---|
| `is_mandatory_page_type(pt)` | `cold_start_page_require_mandatory.get(pt, True)` |
| `golden_min_for(pt)` | `1 if mandatory else 0` |
| `golden_max_for(pt)` | `golden_max_samples_per_page_type` (per-type override is a one-line change here if ever needed) |
| `mandatory_page_types()` | mandatory types in `PAGE_TYPES` order |

Validators (pydantic `field_validator` — fail at startup, not mid-run):

- Unknown key in the map → `ValueError` naming the bad key and the 5 legal values.
- `golden_max_samples_per_page_type >= 1` — a 0 cap contradicts any mandatory type's min of 1.

Delete the dead `promote_min_samples_per_page_type`.

Add `PAGE_TYPES: tuple[str, ...] = get_args(PageType)` to [models/enums.py](src/scraping/models/enums.py) and use it everywhere the 5 values are currently spelled out (`config.py`, `coldstart.py`, and [golden.py:122](src/scraping/repair/golden.py#L122), which hard-codes the tuple today). No import cycle: nothing under `models/` imports `config`.

Env override example for the README:

```bash
SCRAPING_COLD_START_PAGE_REQUIRE_MANDATORY='{"multipack": false, "membership": false}'
SCRAPING_GOLDEN_MAX_SAMPLES_PER_PAGE_TYPE=2
```

### 2. Excel input contract — [coldstart.py](src/scraping/coldstart.py)

Replace `_read_urls(path) -> list[str]` with a validating Excel reader. Use `openpyxl` (already a dependency; the module's existing xlsx reader is [verify_m12.py:643](src/scraping/tests/verify_m12.py#L643) `load_urls` — same `load_workbook(..., read_only=True)` + `iter_rows(min_row=2)` shape). No pandas — nothing under `src/scraping` imports it.

```python
@dataclass(frozen=True)
class ColdStartRow:
    page_type: str   # normalized, guaranteed in PAGE_TYPES
    url: str
    row_no: int      # 1-based sheet row, for error messages

def read_coldstart_input(path: Path) -> list[ColdStartRow]: ...
```

- **Extension gate** — `.xlsx` / `.xlsm` only; a `.txt` path fails with the new contract spelled out.
- **Header lookup** — row 1, matched case-insensitively after `strip()`. Requires `page_type` and `url`; extra columns (e.g. `host`) ignored; column order irrelevant.
- **Normalization** — `str(v).strip().lower()`, then internal spaces/hyphens → `_`, so `"Out of Stock"` and `"out-of-stock"` both resolve to `out_of_stock`. Nothing else is coerced: `stand`, `out`, `disc` stay illegal.
- **Blank rows** skipped (both cells empty); a half-filled row is an error, not a silent skip.
- **Collect-then-raise** — every problem in one `ColdStartInputError`, never fail-on-first, so the sheet is fixed in one pass.

Add `ColdStartInputError(ValueError)` to [exceptions.py](src/scraping/exceptions.py), alongside `ScrapeFailed` / `BrightDataInfraError`.

```
ColdStartInputError: invalid page_type value(s) in tesco.xlsx:
  row 4:  'stand'
  row 9:  'out'
  row 12: '' (empty)
Legal values: standard, discounted, out_of_stock, membership, multipack

ColdStartInputError: missing required column(s): page_type
  found header: label, url, host
  the sheet's first row must contain: page_type, url

ColdStartInputError: expected an Excel file (.xlsx/.xlsm), got 'urls.txt'
  cold start input is a sheet with columns: page_type, url
```

### 3. Coverage requirement

`read_coldstart_input` ends with a coverage check against `cfg.mandatory_page_types()` — **before** any Bright Data fetch, so a bad sheet costs nothing:

```
ColdStartInputError: tesco.xlsx is missing required page_type(s): discounted, membership
  cold start needs >=1 URL for each of: standard, discounted, out_of_stock, membership
  optional (may be omitted): multipack
  found: standard x4, out_of_stock x1
  (edit cold_start_page_require_mandatory in config.py to change what is required)
```

More rows than the cap is fine and useful — extras are spares. Informational, non-fatal, at load time:

```
note: 5 rows for 'standard'; at most 3 goldens are seeded per page_type — extras are spares
      (used only if an earlier one fails extraction or is rejected during review)
```

### 4. Seeding within the caps

**Cold start** — [coldstart.py](src/scraping/coldstart.py):

- Before the review loop, read current per-bucket counts via the existing `GoldenStore.get_page_type_coverage(site)` ([golden_store.py:49](src/scraping/storage/golden_store.py#L49)) — handles a re-run against a site that already has goldens.
- Skip a candidate whose bucket is already at `existing + accepted_this_run >= golden_max_for(pt)` — print `bucket 'standard' already has 3 goldens — skipping (spare)` rather than asking the human a pointless question.
- Show the declared type, and a mismatch warning when `classify_page_type(product)` disagrees:

  ```
  [3/8] https://www.tesco.com/.../312841117
    declared page_type = discounted
    !! MISMATCH: extracted fields look like 'standard' (list_price is None)
    Extracted:
      title        = ...
      price        = 13.00
      list_price   = None
    Accept? [y/N/q]
  ```

- `_seed()` uses `row.page_type` (declared) for `gs.seed(...)`, not `classify_page_type(product)`, and stamps `created_by='coldstart'` (see §5).
- After the loop, shortfall = mandatory types below `golden_min_for(pt)`. If non-empty: print a `WARNING` block naming them, seed everything accepted anyway, return them as `result["coverage_shortfall"]`.
- `main()` exit codes: `0` clean · `1` aborted / nothing seeded / input error · `2` seeded with a coverage shortfall.

`run_coldstart(site, rows: list[ColdStartRow], input_fn=input)` — the `urls: list[str]` parameter is replaced, not overloaded; accepting bare strings would reopen the coverage hole. [verify_m11.py](src/scraping/tests/verify_m11.py) is updated to pass rows.

**Runtime auto-seed** — [golden.py](src/scraping/repair/golden.py):

`maybe_seed_golden()` returns early today when a bucket has *any* sample. Change to grow a bucket up to the cap, with a URL guard:

```python
existing = gs.get_by_site_and_type(site, page_type, exclude_stale=True)
if len(existing) >= cfg.golden_max_for(page_type):
    if len(existing) > cfg.golden_max_for(page_type):
        logger.warning(
            "golden bucket over cap: site=%s page_type=%s has %d > max %d — "
            "run `python -m src.scraping.scripts.prune_goldens --site %s` to shrink",
            site, page_type, len(existing), cfg.golden_max_for(page_type), site,
        )
    return None
if any(s["expected_output"].get("url") == product.url for s in existing):
    return None   # this URL already represents the bucket
```

The URL guard is **required**, not cosmetic: with `max > 1`, re-scraping one URL three times would otherwise fill a bucket with three snapshots of the same page, making the promote gate narrower rather than broader. `expected_output` is the serialized `ProductData`, so `["url"]` is always present (`ProductData.url` is a plain `str`, [product_data.py:47](src/scraping/models/product_data.py#L47)) — no extra column needed for dedup.

Factor these checks into one module-level helper and reuse it from `_maybe_seed_golden_inline()` ([golden.py:322](src/scraping/repair/golden.py#L322), the `_do_promote` path), which carries the identical "seed if empty" rule today.

**Known cost, accepted:** `promote_candidate()` runs a candidate against every non-stale golden, so a fully-populated site goes from 5 sandbox runs per promotion to up to 15 (~10s worst case at `sandbox_timeout`). That is the intended trade — exactly the breadth that catches the M15-class "parser tuned to one product's DOM" failures.

### 5. Shrink path — when the cap is lowered below what a bucket holds

**Provenance column.** `golden_samples` has no provenance today, so an eviction ranking cannot tell a human-confirmed sample from an auto-seeded one. Add, mirroring `parsers.created_by`:

```sql
created_by TEXT NOT NULL DEFAULT 'auto' CHECK(created_by IN ('coldstart', 'auto'))
```

- [storage/database.py](src/scraping/storage/database.py) — add to the `_DDL` for fresh DBs.
- [storage/golden_store.py](src/scraping/storage/golden_store.py) — `seed(..., created_by: str = "auto")`; cold start passes `"coldstart"`.
- `storage/migrations/add_golden_created_by.py` — new, following [add_membership_bucket.py](src/scraping/storage/migrations/add_membership_bucket.py)'s shape (`migration_needed()` → `run_migration()` → idempotent `main()`). Here it is a plain `ALTER TABLE golden_samples ADD COLUMN …` (SQLite permits a CHECK on ADD COLUMN when a satisfying default exists) — no table rebuild.
- **Existing rows backfill to `'auto'`.** Cold-start and auto-seeded goldens are indistinguishable retroactively, so on pre-migration DBs eviction degrades to pure age order. Stated in the script's output, and visible in the dry run before anything is deleted.

**The prune script** — `src/scraping/scripts/prune_goldens.py`, new, beside the existing [reseed_goldens.py](src/scraping/scripts/reseed_goldens.py):

```bash
python -m src.scraping.scripts.prune_goldens                 # all sites, dry run
python -m src.scraping.scripts.prune_goldens --site tesco    # one site, dry run
python -m src.scraping.scripts.prune_goldens --site tesco --apply
```

Dry run is the default; `--apply` is the only thing that deletes. Eviction order **within** an over-cap bucket:

```
1. is_stale = 1              (already excluded from the promote gate — pure dead weight)
2. created_by = 'auto'       oldest captured_at first
3. created_by = 'coldstart'  oldest captured_at first   <- human-verified, dies last
```

and it never takes a bucket below `cfg.golden_min_for(page_type)` — mandatory types keep ≥1, optional types may reach 0. If a bucket is already below its minimum, the script reports it as an under-coverage warning rather than touching it.

```
$ python -m src.scraping.scripts.prune_goldens --site tesco

cap = 2 per page_type   (DRY RUN — nothing deleted)

  tesco/standard      4 -> 2   evict id=7 (stale), id=12 (auto, 2026-06-02)
  tesco/discounted    3 -> 2   evict id=19 (auto, 2026-06-11)
  tesco/membership    1 -> 1   ok
  tesco/out_of_stock  0 -> 0   WARNING: mandatory page_type has no golden
  tesco/multipack     0 -> 0   ok (not mandatory)

3 samples would be deleted (~4.8 MB).
Re-run with --apply to execute.
```

Deletion is a hard `DELETE` (each snapshot is ~1.6MB and the point of lowering the cap is to reduce promote cost and storage). `is_stale` is deliberately **not** reused as a soft-delete marker — it means "rotten, no active parser reproduces it", and overloading it would corrupt the 补样 signal described in spec §5.7.

### 6. Sample data + docs

- Convert `src/scraping/data/cold_start/tesco.txt` → `tesco.xlsx` with `page_type` / `url` columns (extra page types available in `data/test_data/initial_url.xlsx`, which already carries `membership` / `normal` labels for Tesco). Delete the `.txt`.
- **[src/scraping/README.md](src/scraping/README.md)** — the file this started from:
  - §"What it does" *Golden set* bullet (~line 76): per-bucket min/max, config-driven.
  - §"What it does" *Cold start* bullet (~line 77): declared-page_type Excel input.
  - §4 "Cold-start a new site" (~lines 120–131): new command, input-format table (columns, 5 legal values, mandatory-vs-optional, spares), a worked example sheet, exit codes.
  - §"Adding a new site" step 4 (~line 192): `--input waitrose.xlsx`.
  - §Configuration table: `cold_start_page_require_mandatory`, `golden_max_samples_per_page_type` (+ the removal of `promote_min_samples_per_page_type`).
  - New short §"Shrinking the golden set" documenting `prune_goldens` + the migration.
  - §Verification: add the `verify_m17` line.
- **[src/scraping/CLAUDE.md](src/scraping/CLAUDE.md)** — cold-start section (~line 186), Key Config list, file-structure block, M17 milestone row. Edit `CLAUDE.md` only; the pre-commit hook (`scripts/sync_agent_docs.py`) regenerates `AGENTS.md` byte-identically.
- **[src/scraping/tests/README.md](src/scraping/tests/README.md)** — M17 narrative section + artifact-table row (mandated by the Verification Discipline in `src/scraping/CLAUDE.md`).

All files stay UTF-8 without BOM; the `—` / `→` / `≥` characters above are intentional and must survive editing.

## Critical files

| File | Change |
|---|---|
| [config.py](src/scraping/config.py) | `+ cold_start_page_require_mandatory`, `+ golden_max_samples_per_page_type`, 4 accessors, 2 validators; delete dead `promote_min_samples_per_page_type` |
| [models/enums.py](src/scraping/models/enums.py) | `+ PAGE_TYPES = get_args(PageType)` |
| [exceptions.py](src/scraping/exceptions.py) | `+ ColdStartInputError` |
| [coldstart.py](src/scraping/coldstart.py) | `ColdStartRow`, `read_coldstart_input()` (replaces `_read_urls`), review-loop cap + mismatch warning, declared-type seeding with `created_by='coldstart'`, shortfall report, `--input` CLI flag, exit codes |
| [repair/golden.py](src/scraping/repair/golden.py) | `maybe_seed_golden` + `_maybe_seed_golden_inline` → shared cap / URL-dedup helper + over-cap WARN; use `PAGE_TYPES` at line 122 |
| [storage/database.py](src/scraping/storage/database.py), [storage/golden_store.py](src/scraping/storage/golden_store.py) | `golden_samples.created_by` column + `seed(created_by=...)` |
| `storage/migrations/add_golden_created_by.py` | new, idempotent `ALTER TABLE ADD COLUMN` |
| `scripts/prune_goldens.py` | new, dry-run-default eviction |
| [tests/verify_m11.py](src/scraping/tests/verify_m11.py) | pass `ColdStartRow`s; swap `_read_urls` checks for `read_coldstart_input` |
| `tests/verify_m17.py` + `.log` | new (see Verification) |
| `data/cold_start/tesco.xlsx` | new sample sheet; delete `tesco.txt` |
| `README.md`, `CLAUDE.md`, `tests/README.md` (all under `src/scraping/`) | docs |

CLI: `--urls-file` becomes `--input` (it is no longer a URL list). Keep `--urls-file` as a hidden alias routed to the same validator, so an old command line fails with the *Excel-required* message instead of an argparse error.

## Verification

New `src/scraping/tests/verify_m17.py`, **fully offline** — stub `_gen_initial_parser` to return a fixed parser and monkeypatch `BrightDataUnlocker.fetch` against local `data/html_sample/*.html` (the pattern already in [verify_m11.py:116](src/scraping/tests/verify_m11.py#L116)). No Qwen, no Bright Data spend.

1. **Config policy** — defaults give mandatory = {standard, discounted, out_of_stock, membership} and optional = {multipack} · `golden_min_for` is 1/0 by mandatory flag · unknown key → `ValueError` · `max = 0` → `ValueError` · a page_type missing from the map reads as mandatory.
2. **Input validation** — valid sheet → N normalized rows · `stand` / `out` / empty → one error listing *every* bad row plus the legal values · `Out of Stock` normalizes · missing `page_type` column → error · `.txt` path → error · blank rows skipped · half-filled row → error.
3. **Coverage** — sheet without `membership` → error naming the missing types and echoing what was found · sheet without `multipack` → parses fine · flipping `membership` to `False` in config makes the first sheet valid (proves the map drives it).
4. **Cap at seed time** — 5 `standard` rows all accepted → 3 goldens, rows 4–5 auto-skipped without prompting (assert `input_fn` call count).
5. **Declared bucket wins** — a row declared `discounted` whose extraction classifies as `standard` seeds into `discounted`, with the `MISMATCH` line in stdout (`contextlib.redirect_stdout`) and `created_by='coldstart'` in the row.
6. **Shortfall** — reject every `membership` row → parser row and other goldens still present, `result["coverage_shortfall"] == ["membership"]`, exit code 2.
7. **Runtime policy** — `maybe_seed_golden` seeds a fresh bucket, seeds 2nd/3rd distinct URLs, returns `None` at the 4th, and returns `None` for a repeat of an already-stored URL at count 1.
8. **Eviction** — bucket of 4 (1 stale, 2 auto, 1 coldstart) with cap 2: dry run deletes nothing and reports the plan; `--apply` leaves exactly 2 rows, the survivors being the coldstart row and the newest auto row · a mandatory bucket at 1 is never emptied · optional coverage does not block pruning to the cap · the migration is idempotent on a DB that already has the column.

Run and capture (per the module's mandatory verification discipline):

```bash
PYTHONIOENCODING=utf-8 python -m src.scraping.tests.verify_m17 | tee src/scraping/tests/verify_m17_output.log
PYTHONIOENCODING=utf-8 python -m src.scraping.tests.verify_m9     # golden lifecycle regression
PYTHONIOENCODING=utf-8 python -m src.scraping.tests.verify_m11    # real Qwen — new signature end-to-end
```

M9 is the regression gate that matters: it covers `classify_page_type` / `promote_candidate` / prune, all downstream of the seeding-rule change. M11 needs `QWEN_KEY`; unset, it self-skips its LLM sections while the input-reader checks still run.

Manual smoke test against the new sheet (spends Bright Data + Qwen, run once):

```bash
python -m src.scraping.storage.migrations.add_golden_created_by
python -m src.scraping.coldstart --site tesco --input src/scraping/data/cold_start/tesco.xlsx -v
python -m src.scraping.scripts.prune_goldens --site tesco
```

## Notes — out of scope

- `_pick_representative_html()` ([coldstart.py:156](src/scraping/coldstart.py#L156)) still picks the largest fetched page. Now that page types are declared, preferring a `discounted` or `membership` page would teach the first parser more fields — a separate behavioural change with its own LLM-cost evaluation.
- Per-page_type **maximums** (only the minimum varies by type today). `golden_max_for(pt)` is the single seam if that is ever wanted.
- Backfilling existing DBs *up* to 3 samples/bucket — buckets grow naturally on subsequent successful scrapes.

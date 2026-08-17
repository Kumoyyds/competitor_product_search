# Per-row `website` / `country` in the Excel batch

## Context

Today `match_product_batch()` takes one `website` and one `country` for the whole
spreadsheet ([batch.py:68-79](src/search/batch.py#L68-L79)), passed via `--web` /
`--country`. That forces one run per (marketplace, country) pair. A real input
sheet already carries a target marketplace and country per SKU, so these two
values should be read from the input table like `sku_col` is.

Change: replace the scalar `website` / `country` arguments with `web_col` /
`country_col` column names, resolve them per row, and keep everything downstream
(pipeline, provider chain, tracing) untouched — `match_product()` already accepts
per-call `website` / `country`, and `tasks.website` / `tasks.country` in the trace
DB are already per-row ([db.py:90-116](src/search/db.py#L90-L116)).

## Decisions (confirmed with user)

- Flags: `--web-col` / `--country-col`, matching the existing `--sku-col`
  convention; `--web_col` / `--country_col` registered as accepted aliases on the
  same argparse dest.
- Blank / NaN cell in either column → that **row** fails (`match_verdict=error`,
  `url_search_1="not found"`, reason names the offending column) and no search
  call is spent. Other rows keep running; the run still finishes `completed`.
- A missing *column* is still a hard `KeyError` before any work starts.
- `runs.website` / `runs.country` get the distinct values joined
  (`"amazon"` or `"amazon,tesco"`); no schema change. Column names go into
  `job_config`.

## Implementation

### 1. `src/search/batch.py` — signature and per-row resolution

- Signature: replace `website: str` and `country: str = "uk"` with required
  keyword-only `web_col: str` and `country_col: str`.
- After `pd.read_excel`, validate all three columns in one pass and raise
  `KeyError` naming every missing one (replaces the single `sku_col` check at
  [batch.py:91-92](src/search/batch.py#L91-L92)).
- Add a small module-level helper for cell → value:

  ```python
  def _cell(value) -> str | None:
      if value is None or pd.isna(value):
          return None
      text = str(value).strip()
      return text.lower() or None
  ```

  Lowercasing matches what the pipeline does anyway (`website.lower()` in
  [pipeline.py:53](src/search/pipeline.py#L53), `_to_gl()` in
  [providers/serper.py:37-39](src/search/providers/serper.py#L37-L39)) and keeps
  the distinct-value summary tidy.
- Build parallel lists of per-row `(product_name, website, country)` before
  creating tasks, and derive the run summary with an order-preserving dedupe
  (`",".join(dict.fromkeys(v for v in values if v))`, `None` when empty).
- `job_config`: swap the `"website"` / `"country"` scalars for `"web_col"` /
  `"country_col"`; pass the joined summaries as `run_scope(..., website=..., country=...)`.
  `run_scope` itself is unchanged.
- `_run_row` takes `website: str | None`, `country: str | None`. Inside the
  existing `record_task` block, before calling `match_product`, raise
  `ValueError(f"missing {web_col} value")` (or country) when the value is `None`.
  This deliberately reuses the existing failure path: `record_task` flushes an
  `error` task ([trace.py:382-388](src/search/trace.py#L382-L388)) and the
  `except Exception` in `_run_row` turns it into the `{"_error": ...}` dict that
  the result loop already renders as `not found` / `error`
  ([batch.py:178-193](src/search/batch.py#L178-L193)). No new result-shaping code.
  Pass `website=website or ""` / `country=country or ""` into `record_task` so the
  recorder's non-optional fields stay strings.

### 2. `src/search/batch.py` — CLI

- Drop `--web` / `--country`; add:
  ```python
  parser.add_argument("--web-col", "--web_col", required=True,
                      help="Column holding the target marketplace code")
  parser.add_argument("--country-col", "--country_col", required=True,
                      help="Column holding the country code")
  ```
  (dest is `web_col` / `country_col` from the first option string, so both
  spellings work.) Update `main()` to forward `web_col=args.web_col`,
  `country_col=args.country_col`.

### 3. Tests — `tests/unit/search/test_batch_recording.py`

- Update the three existing `match_product_batch(...)` call sites (lines ~104,
  153, 245) to build frames with `web` / `country` columns and pass
  `web_col="web", country_col="country"`.
- New cases:
  - blank `country` cell → that row is `error` with a reason naming the column,
    neighbouring rows `no_match`, `runs.status == "completed"`, `error_count == 1`,
    and the provider call count unchanged for the skipped row.
  - missing `web_col` → `KeyError`.
  - mixed values → `SELECT website, country FROM runs` returns the joined
    distinct strings.

### 4. Docs

Same edit in each place the batch invocation or argument list appears:

- [README.md:36](README.md#L36)
- [CLAUDE.md:21-22](CLAUDE.md#L21-L22) **and** [AGENTS.md:21-22](AGENTS.md#L21-L22)
  (a pre-commit hook keeps these two in sync — edit both identically)
- [src/search/CLAUDE.md:85-92](src/search/CLAUDE.md#L85-L92) **and**
  [src/search/AGENTS.md:85-92](src/search/AGENTS.md#L85-L92)
- [src/search/README.md:42-43](src/search/README.md#L42-L43) and the batch-input
  section at [src/search/README.md:84-88](src/search/README.md#L84-L88) — list the
  three required columns (sku / web / country) and note the blank-cell → row-error
  behaviour
- [docs/architecture.md:90](docs/architecture.md#L90) if it names the scalar args

`scripts/validate_search.py` uses single-product `match_product()` and needs no change.

## Verification

```bash
python -m pytest tests/unit/search/ -v          # offline, no API cost

# end-to-end smoke on a 2-row sheet with two marketplaces/countries
python - <<'PY'
import pandas as pd
pd.DataFrame({
    "product_name": ["Magic Rock Saucery 4 X 330ML", "Kopparberg Mixed Fruit 500ml"],
    "web": ["tesco", "amazon"],
    "country": ["uk", "de"],
}).to_excel("/private/tmp/claude-501/-Users-kumo-programming-competitor-product-search/531ae9e2-fb1f-4fdf-83b0-b4d96fcdc260/scratchpad/mixed.xlsx", index=False)
PY

python -m src.search.batch --input <scratchpad>/mixed.xlsx --sku-col product_name \
    --web-col web --country-col country --output <scratchpad>/out.xlsx
```

Then confirm the trace DB:

```sql
SELECT mode, website, country, job_config FROM runs ORDER BY started_at DESC LIMIT 1;
SELECT row_index, website, country, verdict FROM tasks WHERE run_id = '<run_id>';
```

`runs.website` should read `tesco,amazon`, `runs.country` `uk,de`, and each task
row should carry its own pair. Re-run with one country cell blanked to confirm the
row-level error and `runs.status = 'completed'`.

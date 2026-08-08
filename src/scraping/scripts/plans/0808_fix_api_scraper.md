# M24 — Amazon price-contract normalization + failed-run observability

## Context

Two problems surfaced from one playground session.

**1. Amazon 抓取直接失败。** `scrape("https://www.amazon.co.uk/LIVIVO-Heated-Electric-Over-Blanket/dp/B0772VMN9Y")`
raises `ScrapeFailed(stage=scraper_fallback_exhausted)`. This is **not** an infra/key problem — the
BrightData Datasets fetch succeeded. `escalations` id=2 (`amazon|gate_validation|`, `api_malformed`)
records the real cause:

```
list_price must be greater than price — list_price is only a higher Was/RRP reference price
```

[amazon_uk.py:87](src/scraping/scrapers/sites/amazon_uk.py#L87) maps BrightData's `initial_price` →
`list_price` unconditionally. On a non-discounted Amazon listing BD returns `initial_price ==
final_price`, so Gate 2's `_structural_price_rule` ([gate2.py:28-33](src/scraping/validation/gate2.py#L28-L33))
rejects it. Amazon has exactly one registered scraper, so one `ScrapeFailed` immediately exhausts the
router chain. The M20 contract already says `list_price` exists *only* as a separately displayed
higher Was/RRP reference — so the correct behaviour is to **omit** it when there is no discount, not
to relax the gate.

Same unguarded shape exists at [argos_dca.py:47-52](src/scraping/scrapers/sites/argos_dca.py#L47-L52)
and [tesco_dca.py:50-54](src/scraping/scrapers/sites/tesco_dca.py#L50-L54) — latent, same class of bug.

**2. 纯 API scraper 的执行记录不可查。** `scrape_runs` holds 12 rows, all `outcome=success/path=fast`,
**zero amazon rows** — yet we know an Amazon execution ran today. Root cause is not "API scrapers skip
logging" (`DirectAPIScraper` has its own `_record_run`); it is that **every failure path raises before
`_record_run` is reachable**, in both routes:

- [api_scraper.py:61-68](src/scraping/scrapers/api_scraper.py#L61-L68) (`api_infra`), [:69-77](src/scraping/scrapers/api_scraper.py#L69-L77) (`api_fetch`), [:118-125](src/scraping/scrapers/api_scraper.py#L118-L125) (`api_malformed`)
- [html_scraper.py:72](src/scraping/scrapers/html_scraper.py#L72), [:81](src/scraping/scrapers/html_scraper.py#L81), [:152](src/scraping/scrapers/html_scraper.py#L152)

The DDL already reserves `outcome='escalated'` / `path='escalated'`
([database.py:37-38](src/scraping/storage/database.py#L37-L38), [enums.py:7,15](src/scraping/models/enums.py#L7))
but **no executable code ever writes those literals**. `scrape_runs` is a success ledger, not an
execution log. Aggravating factors: the 1-hour `RunStore.is_duplicate` window suppresses re-runs, and
`ScrapeFailed` from the API route never passes `snapshot=`, so `escalations.snapshot_preview` is `null`
— the raw BrightData JSON that caused the failure is stored nowhere.

Intended outcome: the Amazon URL scrapes successfully, and every scrape attempt — success or failure,
HTML or API — leaves one queryable row in `scrape_runs` carrying enough detail to diagnose it.

---

## Part A — Price-contract normalization at the API mapping layer

### A1. New shared normalizer

New file `src/scraping/scrapers/price_fields.py`:

```python
def normalize_price_fields(mapped: dict) -> tuple[dict, list[str]]:
    """Drop price qualifiers that violate the M20 canonical contract.

    Mirrors gate2._structural_price_rule exactly, including its
    ``price is not None`` guard: when price is absent we keep list_price /
    membership_price, because _out_of_stock_signal_rule accepts them as the
    only surviving product signal on an out-of-stock page.
    """
```

Rules (only when `price is not None`):
- `list_price <= price` → set `None` (no discount displayed; `initial_price == final_price`)
- `membership_price >= price` → set `None`
- `membership_price <= 0` → set `None` (unguarded by price, matches [gate2.py:42-43](src/scraping/validation/gate2.py#L42-L43))

Returns the normalized dict plus a list of `"list_price=19.99 (== price)"`-style notes for logging, so
a dropped field is still visible in logs rather than silently vanishing.

### A2. Wire it as a single choke point

In [api_scraper.py:86-88](src/scraping/scrapers/api_scraper.py#L86-L88), between `_apply_heal_cache`
and `validate`:

```python
mapped = self._map_fields(json_data, url)
mapped = self._apply_heal_cache(json_data, mapped)
mapped, dropped = normalize_price_fields(mapped)
if dropped:
    logger.info("price contract dropped %s (url=%s)", dropped, url)
product, errors = validate(mapped)
```

One call site fixes Amazon **and** both DCA backups **and** any future API scraper — the same
choke-point idiom the module already uses for the M15 `availability_raw` normalizer and the M16 UTF-8
decode. No per-site edits to `amazon_uk.py` / `argos_dca.py` / `tesco_dca.py` are needed.

**Do not apply this to the HTML route.** HTML parsers are LLM-generated; Gate 2 catching their
inverted price mappings is precisely what drives the repair ladder and the fast-path distrust guard.
API mappings are hand-written against known-stable response keys, so normalizing there is safe.

Side benefit: the ordering error can no longer reach `heal_json`, which today wastes an LLM call
because [`_extract_missing_fields`](src/scraping/repair/json_healer.py#L162-L173) substring-matches
`"price"`/`"list_price"` inside an *ordering* message and thinks the fields are missing. No change to
the healer is required once A2 lands.

---

## Part B — Failed-run observability

### B1. Hoist `_record_run` / `_store_result` to `BaseScraper`

[html_scraper.py:317-356](src/scraping/scrapers/html_scraper.py#L317-L356) and
[api_scraper.py:160-197](src/scraping/scrapers/api_scraper.py#L160-L197) are duplicates (HTML's
`_record_run` differs only by an extra `winning_parser_id` param; `_store_result` is identical). Move
both to `scrapers/base.py` with the superset signature and delete both copies — every subsequent change
in this plan then has one place to land.

Extend the hoisted signature with `signature: Optional[str] = None` and `error: Optional[str] = None`.

### B2. Record failed runs

Before each `raise ScrapeFailed`, call `_record_run(..., "escalated", "escalated", signature=..., error=...)`:

| file | sites |
|---|---|
| `api_scraper.py` | `:61-68` api_infra, `:69-77` api_fetch, `:118-125` api_malformed |
| `html_scraper.py` | `:72` extraction_infra, `:81` extraction, `:152` repair-ladder terminal |

`signature` is the `"{site}\|{field_or_rule}\|{parser_version}"` string (same composition as
[router._derive_signature](src/scraping/router.py#L142-L149) — factor that formatting into a small
helper and reuse it from both places rather than duplicating the f-string). `error` is
`" | ".join(errors)` truncated to 2000 chars. Both are best-effort and stay inside the existing
`try/except` swallow — recording must never change control flow.

For the `:152` HTML case the exception object already carries `signature`/`errors`; read them off
`outcome` before re-raising.

### B3. Failures bypass the dedup window

In the hoisted `_record_run`:

```python
if outcome != "success" or not store.is_duplicate(url):
    store.record(...)
```

Rationale: `is_duplicate` ([run_store.py:14-22](src/scraping/storage/run_store.py#L14-L22)) exists to
keep `get_hit_rates` from double-counting successes; a failure is an incident and must always be
recorded, or a repeatedly-failing URL stays invisible for an hour.

**Known behaviour change to flag:** `RunStore.count_total_runs`
([run_store.py:68-76](src/scraping/storage/run_store.py#L68-L76)) is the denominator of the
mass-invalid-target ratio ([html_scraper.py:265-300](src/scraping/scrapers/html_scraper.py#L265-L300)).
Adding `escalated` rows enlarges it, making that alarm slightly *less* trigger-happy. This is the
semantically correct denominator ("invalid targets among all attempts") — the old one silently omitted
every failure. Keep the change; assert the new ratio behaviour explicitly in the verify script.

### B4. Two new `scrape_runs` columns

Generalize the existing incremental-migration mechanism at
[database.py:83-136](src/scraping/storage/database.py#L83-L136): replace `_GOLDEN_ADDED_COLUMNS` with
`_ADDED_COLUMNS: dict[str, dict[str, str]]` keyed by table name, and loop tables in `_ensure_columns`
(keeping the existing `BEGIN IMMEDIATE` serialization). Add to both `_DDL`'s `scrape_runs` block and
the migration map:

- `signature TEXT` — joins a run row to `escalations.signature`
- `error TEXT` — the truncated failure message; without it a failed row says *when* but not *why*

Plus `CREATE INDEX IF NOT EXISTS idx_scrape_runs_site_outcome ON scrape_runs(site, outcome, scraped_at)`.

Existing DBs migrate automatically on the next `init_db()` — no manual step, no data loss. Also extend
`RunStore.record` to accept and insert the two columns.

### B5. Attach the raw API payload to the failure

At [api_scraper.py:118-125](src/scraping/scrapers/api_scraper.py#L118-L125), pass
`snapshot=json.dumps(json_data, default=str)[:8000]`. [router.py:107](src/scraping/router.py#L107)
already slices `snapshot[:2000]` into `escalations.snapshot_preview`, which is `null` today — this is
the only way the offending BrightData JSON ever gets persisted. (The two fetch-stage failures have no
JSON yet; nothing to attach there.)

### B6. Make the traceback self-explanatory

[`ScrapeFailed.__str__`](src/scraping/exceptions.py#L19-L23) deliberately omits `errors`, which is why
the playground traceback was undiagnosable. Append the signature's rule and the first error (truncated
~200 chars).

### B7. `escalations` stays as-is — deliberately

Do **not** touch the `UNIQUE(signature)` dedup ([escalation_store.py:22-33](src/scraping/storage/escalation_store.py#L22-L33)).
D24 dedup is intentional. With B2 in place the split becomes clean and answers the original question:
`scrape_runs` = per-execution log (every attempt, every URL, every timestamp);
`escalations` = deduped aggregate/alarm. Document this split in the module CLAUDE.md.

---

## Verification

Module discipline is mandatory: persistent artifacts under `src/scraping/tests/`.

1. **`src/scraping/tests/verify_m24.py`** — offline, named `[PASS]`/`[FAIL]` checks, ends with
   `SUMMARY: N passed, M failed`, non-zero exit on failure:
   - `normalize_price_fields`: `initial == final` drops `list_price`; `initial > final` keeps it;
     `price=None` keeps both (out-of-stock signal preserved); `membership >= price` drops;
     `membership <= 0` drops.
   - `AmazonUKScraper._map_fields` + normalizer on a fixture BD JSON with `initial_price ==
     final_price` → passes both gates (the regression that started this).
   - Fake `DirectAPIScraper` against a temp DB: each of the three failure paths writes exactly one
     `scrape_runs` row with `outcome='escalated'`, non-null `signature` and `error`.
   - Same for a fake `HTMLScraper` on its three raise sites.
   - Dedup: two failures on the same URL inside the window → 2 rows; two successes → 1 row.
   - Migration: create a DB from the *old* `scrape_runs` DDL, run `init_db()`, assert `signature` and
     `error` columns appear and existing rows survive.
   - `escalations.snapshot_preview` is non-null after an `api_malformed` failure.
   - `str(ScrapeFailed)` contains the first error.
   - `count_total_runs` includes escalated rows (documents the B3 ratio change).
2. **Capture the log**: `python src/scraping/tests/verify_m24.py | tee src/scraping/tests/verify_m24_output.log`
3. **Run the full offline suite** (`verify_m20`–`verify_m23` at minimum) — B1's hoist and B4's schema
   change touch shared code.
4. **Live check** (one BrightData call): re-run the LIVIVO URL in `playground.ipynb`; expect
   `ProductData` with `list_price=None`. Then confirm the record is queryable:
   ```sql
   SELECT scraped_at, site, scraper, outcome, path, signature, substr(error,1,120)
   FROM scrape_runs WHERE site='amazon' ORDER BY scraped_at DESC LIMIT 10;
   ```

## Docs to update in the same commit

- `src/scraping/README.md` — operator-facing: failed scrapes are now queryable in `scrape_runs`
  (`outcome='escalated'`), with the example SQL above; note the schema migrates automatically.
- `src/scraping/CLAUDE.md` — M24 row in the milestone table + a short section covering the price
  contract choke point and the `scrape_runs`-log / `escalations`-aggregate split. (The `AGENTS.md`
  sibling syncs via the pre-commit hook.)
- `src/scraping/tests/README.md` — add `verify_m24.py` / `verify_m24_output.log`.

## Explicitly out of scope

- **No `order=2` backup scraper for Amazon.** It needs a new BD channel plus a cold-start parser and
  golden samples — a milestone of its own. The single-scraper fragility remains, and is the reason one
  mapping bug was terminal; worth a follow-up.
- No change to `escalations` dedup (B7), and no change to `json_healer._extract_missing_fields` —
  A2 makes that path unreachable from the API route.

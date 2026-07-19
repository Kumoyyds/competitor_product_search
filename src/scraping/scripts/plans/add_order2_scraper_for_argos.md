# Plan: Add Argos DCA fallback scraper (order=2)

## Context

Argos currently has a single scraper: `ArgosScraper` (HTML route via BrightData Web
Unlocker, `order=1`). When the Web Unlocker route fails terminally (blocked page, parser
can't extract, etc.), the router has nothing to fall through to and writes an escalation
ticket.

We want a **fallback scraper** for Argos, tried second (`order=2`), using BrightData's
**DCA (Data Collection API)** method — the code the user validated in
`src/scraping/scripts/html_extractor_test.ipynb` under the `argos dca` session. That
session triggers collector `c_mrkepcie19jse9x5xb` and returns **structured JSON** (not
HTML), e.g.:

```json
{"product_title": "McGregor 30cm Electric Hover Collect Lawnmower - 1700W",
 "image_urls": ["https://media.4rgos.it/..."],
 "price": {"value": 85, "currency": "GBP", "symbol": "£"},
 "list_price": {"value": 95, "currency": "GBP", "symbol": "£"},
 "currency": "£", "discount": true, "in_stock": true,
 "input": {"url": "https://www.argos.co.uk/product/4490582"}}
```

Because DCA returns JSON, this is a **`DirectAPIScraper`** (the JSON route), exactly
mirroring the existing `TescoDCAScraper` (Tesco's `order=2` DCA backup). This pattern is
already documented as a worked example in [src/scraping/README.md](src/scraping/README.md#L197-L232)
(§"Adding a fallback scaper"), which even shows an `ArgosDCAScraper` skeleton — our job is
to fill it with the real Argos collector id and the real Argos DCA field schema.

Intended outcome: `await scrape("https://www.argos.co.uk/product/...")` tries the HTML
Unlocker first and, on terminal failure, automatically falls through to the DCA route
before escalating.

## How the fallback wires in (no router/registry/config changes needed)

- Router loops `get_scrapers("argos")` in ascending `order`, falling through on terminal
  `ScrapeFailed` — see [router.py:63-87](src/scraping/router.py#L63-L87).
- Registration is purely the `@register_scraper("argos", order=2)` decorator plus importing
  the new module in [scrapers/sites/__init__.py](src/scraping/scrapers/sites/__init__.py).
- The DCA client, key, poll budget, and retry are all already in place. `config.py` reads
  `BRIGHT_UNLOCKER_KEY` as an alias for `bright_data_key`, so the notebook's key already works.

## Changes

### 1. New file: `src/scraping/scrapers/sites/argos_dca.py`

Mirror [tesco_dca.py](src/scraping/scrapers/sites/tesco_dca.py) — same imports, same
`_to_decimal` helper, same trigger/poll split (`with_extraction_retry(self._client._trigger, url)`
then `self._client._poll(...)`). Differences from Tesco:

- `@register_scraper("argos", order=2)`, class `ArgosDCAScraper(DirectAPIScraper)`.
- `__init__`: `self._client = BrightDataDCA(collector_id="c_mrkepcie19jse9x5xb")` — the
  **Argos** collector from the notebook (Tesco uses the default `c_mr6mrw40d614thtpd`;
  `BrightDataDCA.__init__` already accepts a `collector_id` arg — see
  [bright_data.py:237](src/scraping/extraction/bright_data.py#L237)).
- `_is_not_found`: `return not json_data.get("product_title")` (Argos uses `product_title`,
  not Tesco's `product_name`).
- `_map_fields` mapped to the Argos DCA schema → `ProductData`-compatible dict:
  - `title` ← `product_title`
  - `price` ← `price.value` via `_to_decimal`; `currency` ← `price.currency` (`"GBP"`),
    falling back to top-level `currency`
  - `list_price` ← `list_price.value` via `_to_decimal`
  - `image_urls` ← `image_urls` (already a list; guard str/None like Tesco does)
  - `in_stock` ← `bool(json_data.get("in_stock", False))`
  - `brand` = `None`, `availability_raw` = `None` (Argos DCA has neither field)
  - `url` ← `(json_data.get("input") or {}).get("url", url)`; `website` = `"argos"`;
    `source_type` = `"api"`; `scraped_at` = now(UTC); `raw` = `json_data`

  (`discount` has no `ProductData` field — it stays in `raw`.)

### 2. Register: `src/scraping/scrapers/sites/__init__.py`

Add `argos_dca` to the import line:
```python
from . import amazon_uk, argos, argos_dca, tesco, tesco_dca
```

### 3. Doc touch: `src/scraping/CLAUDE.md`

Add `ArgosDCAScraper (DCA API, order=2, Argos backup)` under `DirectAPIScraper` in the
Class Hierarchy block so the enumerated hierarchy stays accurate.

## Verification (offline only — no BrightData calls)

Mirror the existing `TescoDCA` offline tests; then re-run and refresh the captured logs.

1. **Mapping test** — add `verify_argos_dca_mapping()` to
   [tests/verify_m4_m5.py](src/scraping/tests/verify_m4_m5.py) (mirror
   `verify_tesco_dca_mapping`, lines 96-133) using the **exact** notebook payload above.
   Assert: `title` contains "McGregor"; `price == Decimal("85")`; `list_price == Decimal("95")`;
   `currency == "GBP"`; `in_stock is True`; `image_urls` populated; and `validate(mapped)`
   passes both gates. Register it in the `TESTS`/main list at the bottom of the file.
2. **Not-found test** — extend `verify_api_not_found()` (lines 140-160): `product_title`
   present → not flagged; missing `product_title` → flagged.
3. **Ordering test** — update [tests/verify_m1_m3.py:105-113](src/scraping/tests/verify_m1_m3.py#L105-L113)
   to assert Argos now has **2** scrapers with `ArgosScraper` first (order=1) and
   `ArgosDCAScraper` second (order=2), mirroring the existing Tesco assertions.
4. **Re-run + capture logs** (per the module's mandatory verification discipline):
   ```powershell
   python src/scraping/tests/verify_m1_m3.py  | Tee-Object src/scraping/tests/verify_m1_m3_output.log
   python src/scraping/tests/verify_m4_m5.py  | Tee-Object src/scraping/tests/verify_m4_m5_output.log
   ```
   Confirm each ends with `SUMMARY: N passed, 0 failed` and exits zero.

## Notes

- A stale `scrapers/sites/__pycache__/argos_dca.cpython-312.pyc` exists with no matching
  source (a prior attempt). Creating the new `argos_dca.py` regenerates it; no cleanup needed.
- No changes to `router.py`, `registry.py`, `hosts.yaml`, `config.py`, or `bright_data.py`
  — the decorator + import fully wire the fallback in.

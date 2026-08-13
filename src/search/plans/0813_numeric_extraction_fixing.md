# Fix numeric extraction (storage / power / voltage / inch all silently dropped)

## Context

The latest batch run (`output/0813_tesco_algos_amazon_results.xlsx`, run `1b82f20e…`, 81 SKUs / 1455 candidates) shows the numeric layer contributing almost nothing:

| numeric verdict | count |
|---|---|
| `unknown` | 54 |
| `pass` | 11 |
| `fail` | **0** |
| `null` (short-circuited earlier) | 16 |

Of 1455 candidate titles, **1397 extracted zero numerics**. Across the whole `.cache/base_extraction.sqlite` (478 titles) the only attributes ever produced are `weight_g`, `volume_ml`, `count`, `length_mm`, `abv_percent`. `storage_gb`, `ram_gb`, `screen_inch`, `voltage_v` have **never been produced once**, despite being listed in `discrete_attrs`.

Verified directly:

```
extract_numerics('SanDisk Ultra 64GB, USB 3.0 Flash Drive') -> {}
extract_numerics('Duracell 30W USB-A + USB-C PPS Charger')  -> {}
extract_numerics('Samsung 55 inch TV 1TB')                  -> {}
extract_numerics('Campo Viejo Rioja Reserva 75cl')          -> {'volume_ml': 750.0}   # volume/weight/length DO work
```

So it isn't "全部失灵" — mass/volume/length/ABV/count work. Everything **electronics-shaped** is dropped, which is most of this input file. Goal: make numeric actually gate electronics candidates instead of always returning `unknown`.

## Root causes (all confirmed against quantulum3 output)

1. **Wrong entity name — the main bug.** `search_config.yaml:59` maps `"digital storage": storage_gb`, but quantulum3 emits entity **`data storage`**. `_entity_to_attr()` ([numeric.py:33-35](src/search/layers/numeric.py#L33-L35)) misses, `attr is None`, `continue` → every GB/TB/MB value is discarded. `ambiguity_rules` at `search_config.yaml:117` has the same wrong key, so the RAM/storage disambiguation is dead code too.

2. **Ambiguity window is bidirectional + first-rule-wins.** `_disambiguate()` ([numeric.py:38-50](src/search/layers/numeric.py#L38-L50)) scans ±20 chars and returns the first matching rule in dict order. Verified on the real SKU `realme C75 … 8GB RAM + 128GB ROM`: **both** `8GB` and `128GB` resolve to `ram_gb`; `if attr in out: continue` ([numeric.py:129](src/search/layers/numeric.py#L129)) then throws the 128GB away. Same failure on `Nextorage … 256GB Memory Card` → `ram_gb` (rule `memory: ram_gb`). Fixing cause 1 alone would produce *wrong* values here.

3. **Silent drops when a unit is absent from `unit_conversions`.** `_convert()` returns `None` and the quantity is skipped with no trace ([numeric.py:147-148](src/search/layers/numeric.py#L147-L148)). Missing units that actually occur in this run's titles: `inch` (length), `ounce`/`pound` (mass, 16 titles), `fluid ounce` (volume).

4. **Whole entities unmapped.** `power` (W — 12 titles), `electric potential` (V — 20 titles), `charge` (mAh — 4 titles). `voltage_v` is declared discrete but nothing ever emits it.

5. **Screen size is unreachable.** quantulum3 parses `55"` / `6.72"` as entity **`angle`** (`second of arc`), and `23.8 inch` as `length`. Neither can yield `screen_inch`. 22 candidate titles carry an inch spec.

6. **`count` under-extracted.** `_PACK_RE` ([numeric.py:26](src/search/layers/numeric.py#L26)) only matches `pack of N`. `4 Pack`, `4-pack`, `2 pk` are missed (~70 candidate titles carry a pack/xN pattern).

7. **Stale cache will mask the fix.** `BaseExtractionCache` keys on `md5(title)` only ([cache.py:41-43](src/search/cache.py#L41-L43)). 478 rows are cached, 339 with `{}` numerics. Without invalidation, the fixed extractor never runs for any previously-seen title.

8. **Dead/incorrect config.** `entity_to_attr: dimensionless: count` and `unit_conversions.count` are unreachable (`numeric.py:126-127` skips `count`). `storage_gb.tb: 1024` is inconsistent with `mb: 0.001` — product marketing storage is decimal.

Why tests missed it: [test_numeric.py:24-27](tests/unit/search/test_numeric.py#L24-L27) tests `compare_numerics` on a hand-built `{"storage_gb": …}` dict. There is no test that `extract_numerics("… 64GB …")` produces anything.

## Changes

### 1. `src/search/maintain/search_config.yaml` — `numeric` section

- Rename `"digital storage"` → `"data storage"` in both `entity_to_attr` and `ambiguity_rules`.
- Add entity mappings: `power: power_w`, `electric potential: voltage_v`, `charge: charge_mah`.
- Add `unit_conversions` tables for the new attrs (`watt: 1`, `kilowatt: 1000`; `volt: 1`, `millivolt: 0.001`; `milliampere-hour: 1`, `ampere-hour: 1000`) and fill the gaps in existing ones: `weight_g.ounce: 28.3495`, `weight_g.pound: 453.592`; `volume_ml.fluid ounce: 29.5735`; `screen_inch.inch: 1`.
- Fix `storage_gb.tb: 1024` → `1000`.
- Extend `ambiguity_rules["data storage"]` with `rom: storage_gb`, `ssd/hdd/card/sd/microsd/flash drive: storage_gb` and make ordering irrelevant (see change 2).
- Drop the dead `dimensionless: count` mapping and `unit_conversions.count` table, or comment them as reserved.
- `discrete_attrs`: keep `storage_gb, ram_gb, count, screen_inch, voltage_v, abv_percent`, add `power_w`. Leave `charge_mah` **continuous** (±10%) — listing text is noisy there.

### 2. `src/search/layers/numeric.py`

- **`_disambiguate()`** — replace first-rule-wins over a symmetric window with **nearest-keyword-wins, forward-biased**: search the text after the quantity span first (qualifiers follow the number: `128GB ROM`, `8GB RAM`), only fall back to the preceding window if nothing matches forward. Return the keyword with the smallest distance to the span rather than the first key in dict order. This is what makes `8GB RAM + 128GB ROM` resolve to `{ram_gb: 8, storage_gb: 128}`.
- Add a `screen_inch` regex pre-pass alongside `_ABV_RE` / `_COUNT_X_RE`, matching `N"`, `N inch`, `N-inch`, `N in` with a display/TV/monitor-agnostic word boundary. Deliberately **do not** add `inch` to `length_mm` — folding screen inches into the same bucket as `38cm` doll heights would produce false `FAIL`s.
- Widen `_PACK_RE` to also match `(\d+)\s*[-\s]?(pack|pk)\b` while keeping `pack of N`.
- Guard the new pre-passes the same way as the existing ones (`if "count" not in out`) so regex keeps priority over quantulum3.

### 3. `src/search/cache.py` — invalidate on extractor change

Add a module-level `EXTRACTOR_VERSION` constant and fold it into `_key()`: `md5(f"{EXTRACTOR_VERSION}|{title}")`. Bump it in this change. Old rows become unreachable (harmless, ~478 rows); no migration needed. Note the constant in [src/search/CLAUDE.md](src/search/CLAUDE.md) under "Cache key" so future extractor edits remember to bump it.

### 4. Tests — `tests/unit/search/test_numeric.py`

Add extraction-level tests (the missing coverage that hid this):

- `64GB` / `64 GB` / `1TB` → `storage_gb` 64 / 64 / 1000
- `8GB RAM + 128GB ROM` → `{ram_gb: 8, storage_gb: 128}` (the disambiguation regression)
- `256GB Memory Card` → `storage_gb`, not `ram_gb`
- `30W` → `power_w: 30`; `3V` → `voltage_v: 3`; `6000mAh` → `charge_mah: 6000`
- `55"` and `23.8 inch` → `screen_inch`
- `4 Pack` / `4-pack` / `Pack of 4` → `count: 4`
- Negative guard: `Max Read Speed 300MB/s` must **not** yield `storage_gb` (quantulum3 already classifies it as `data transfer rate` — lock that in)
- Negative guard: `USB 3.0`, `iPhone 16` must not yield any attribute

## Risks

Numeric is a hard kill-gate before the LLM ([base_match.py:35-38](src/search/layers/base_match.py#L35-L38)) — this run produced **zero** `fail`s, so after the fix candidates will start dying at this layer for the first time. That is the intent, but it is the thing to watch. Mitigations already in the design: comparison only uses **shared** keys, so a query without a storage spec can never fail a candidate that has one; and `charge_mah` stays continuous.

## Verification

1. `python -m pytest tests/unit/search/ -v` — offline, no API cost.
2. Numeric pre-pass over real SKUs, no search calls:
   `python scripts/validate_search.py --sample 20 --budget 0` (`print_numeric_prepass`, [scripts/validate_search.py:53](scripts/validate_search.py#L53)).
3. Re-extract the recorded titles from the last run and diff coverage — read the 1455 candidate titles out of `search_db.sqlite` (`candidates` table, `run_id='1b82f20e73ee4c4e8a7b8e388a0aed72'`), run `extract_numerics` on each, and confirm non-empty extractions rise from 58/1455 and that `storage_gb`/`power_w`/`voltage_v`/`screen_inch` are now non-zero. No network needed.
4. Re-run the batch on the same input and compare the `numeric` field in `match_layer_trace`: expect `unknown` to fall well below 54 and some `fail`s to appear. Spot-check every new `fail` against its candidate title to confirm none is a false kill.

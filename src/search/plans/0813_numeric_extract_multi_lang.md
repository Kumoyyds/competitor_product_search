# Multilingual numeric extraction (de / fr / nl) — fix decimal-comma corruption

## Context

Follow-up to `src/search/plans/0813_numeric_extraction_fixing.md`, which is already applied
(`numeric.py`, `cache.py`, `search_config.yaml` are modified in the working tree).

quantulum3 ships **only the `en_US` language pack** (`.venv/…/quantulum3/_lang/` contains
`en_US` and nothing else). Its `regex.py` sets `GROUPING_OPERATORS = {",", " "}` and
`DECIMAL_OPERATORS = {"."}` — comma is a thousands separator, period is the decimal point.
de / fr / nl / es / it / pt / se / pl / br use the opposite convention, and **9 of the 15
countries in `providers/countries.py::COUNTRY_NAMES` are decimal-comma locales**.

The result is not a missed extraction — it is a **wrong value**, which is worse, because
numeric is a hard kill-gate before the LLM ([base_match.py:35-38](src/search/layers/base_match.py#L35-L38)).
Measured against live quantulum3:

| input | correct | actual | attr class | effect |
|---|---|---|---|---|
| `1,5 l` | 1500 ml | **5000 ml** | continuous | `FAIL` (>±10%) |
| `2,5 kg` | 2500 g | **5000 g** | continuous | `FAIL` |
| `0,33 l` | 330 ml | **33000 ml** | continuous | `FAIL` |
| `4,1 liter` | 4100 ml | **1000 ml** | continuous | `FAIL` |
| `1,5V` | 1.5 V | **5 V** | discrete | `FAIL` |
| `1.000 g` (DE thousands) | 1000 g | **1 g** | continuous | `FAIL` |

quantulum splits `1,5 l` into `1` (dimensionless) + `5 l`. Verified end-to-end on real
candidate titles pulled from run `1b82f20e…`:

```
'Lee Kum Kee Panda Oyster Sauce 2.27kg'  -> {'weight_g': 2270.0}
'Lee Kum Kee Panda Oestersaus - 2,27 Kg' -> {'weight_g': 27000.0}  => Verdict.FAIL
'Coca-Cola 1.5L' vs 'Coca-Cola Zero 1,5 l fles'  -> 1500 vs 5000    => Verdict.FAIL
'Varta AA batteries 1.5V' vs 'Varta Batterien 1,5V AA 4er-Pack'     => Verdict.FAIL
```

Correct candidates die at base_match and never reach `distinguishing`. The just-applied fix
made this *more* dangerous: activating `voltage_v` / `power_w` removed the accidental shield
that came from those attributes never being produced at all.

Exposure in the last run was low (4 candidate titles) only because it was 65 uk / 16 nl with
English-formatted SKU names. An amazon.de / carrefour.fr / ah.nl batch hits this constantly —
decimal comma is the default notation for European grocery, beverage and appliance listings.

## Root causes

1. **Decimal comma parsed as thousands separator** — the dominant issue. Inflates values by
   ~2–100x. `_COUNT_X_RE` also misses `6x2,27kg` and `24 x 0,33 l` for the same reason.
2. **German thousands dot parsed as decimal point** — `1.000 g` → 1 g. Deflation; rarer than
   (1) but the same class of bug.
3. **Pack/count keywords are English-only** — `_PACK_RE` ([numeric.py:26-29](src/search/layers/numeric.py#L26-L29))
   misses DE `24er Pack` / `6er-Pack` / `Packung mit 6` / `N Stück`, NL `4 stuks` / `Doos van 12` /
   `set van 4`, FR `Lot de 6` / `Paquet de 4` / `Pack de 6` / `Boîte de 12`.
4. **Screen-size words are English-only** — `_SCREEN_INCH_RE` ([numeric.py:30-36](src/search/layers/numeric.py#L30-L36))
   misses DE `Zoll` and FR `pouces` / `pouce`. (NL uses `inch`, already works.)
5. **`_ABV_RE` requires the literal token `ABV`** ([numeric.py:21](src/search/layers/numeric.py#L21)) —
   misses DE/FR/NL `4,9% vol`, `13,5% vol`, `vol.%`, `Alk. 5,0%`, `alc. 5%`.

Not a defect, no action needed: spelled-out foreign unit words (`Gramm`, `Kilogramm`, `stuks`)
land in quantulum entity `unknown` and are silently dropped — no false value is produced, and
symbol forms (`g`, `kg`, `ml`, `l`, `W`, `V`) are language-neutral and already work.

## Changes

### 1. `src/search/layers/numeric.py` — separator normalization pre-pass (the critical fix)

Add `_normalize_separators(text) -> str`, run it **first** in `extract_numerics()` so both the
regex pre-passes and quantulum3 see normalized text. Use a **digit-count heuristic** rather than
country gating, because it is self-disambiguating and does not require knowing the source
language — SKU names in an `nl` batch are frequently written in English (confirmed in the last
run's input), so a country switch would mis-fire on the query side.

- `,` followed by exactly **3** digits and not followed by another digit → English thousands
  separator. **Leave unchanged** (quantulum already handles `1,000 g` correctly).
- `,` followed by **1, 2, or 4+** digits, between two digits → decimal comma. Rewrite to `.`.
  Covers `1,5` `2,27` `36,5` `13,5` `0,08`.
- `.` where the integer part is 1–3 digits and exactly **3** digits follow, not followed by
  another digit, and immediately followed by a unit token → German thousands dot. **Strip it.**
  The unit-adjacency requirement is what keeps `USB 3.0`, `Nikon 1.4`, version numbers, and
  `3.000` sitting alone out of scope. Gate this rule on the decimal-comma country set (see
  change 2) since it is genuinely ambiguous with an English 3-decimal-place number.

Because normalization runs before the pre-passes, `6x2,27kg` and `24 x 0,33 l` start matching
`_COUNT_X_RE` for free — no change needed there.

### 2. Plumb `country` into the numeric layer

`country` is already in the graph state ([pipeline.py:54](src/search/pipeline.py#L54),
[pipeline.py:142](src/search/pipeline.py#L142)) but never reaches numeric. Thread it through
`base_match_node` → `_evaluate_one` → `_extract` → `extract_numerics(text, country=None)`,
keeping the parameter optional so existing callers ([scripts/validate_search.py:57](scripts/validate_search.py#L57)
and the tests) keep working. Used only for the change-1 thousands-dot rule and to pick the
keyword sets in change 3.

**`cache.py` must key on country too** — `EXTRACTOR_VERSION|country|title`, and bump
`EXTRACTOR_VERSION` to `"3"`. Otherwise a title cached under one locale is reused under another.

### 3. `src/search/maintain/search_config.yaml` — move language keywords into config

Add a `numeric.locale` section so adding a market is a config edit, not a code edit — matching
the existing convention for `domain_map` and `retailer_keywords`:

```yaml
numeric:
  locale:
    decimal_comma_countries: [de, fr, nl, es, it, pt, se, pl, br]
    pack_keywords:      # matched around a number, case-insensitive
      en: [pack, pk, pcs, count]
      de: [er pack, er-pack, packung, stück, stueck, stk, set mit]
      nl: [stuks, stuk, st, doos van, set van, pak van]
      fr: [lot de, paquet de, pack de, boîte de, boite de, pièces, pieces]
    inch_keywords:
      en: [inch, inches, in]
      de: [zoll]
      fr: [pouces, pouce]
    abv_keywords: [abv, vol, vol., alc, alc., alk, alk., alcohol, alkoholgehalt]
```

Build `_PACK_RE` / `_SCREEN_INCH_RE` / `_ABV_RE` from these lists at module load (all locales
in one alternation — a German title can appear in a `nl` search result, so do not restrict by
country here). Keep the compiled patterns module-level so per-call cost stays zero.

DE `24er Pack` needs the number-before form `(\d+)\s*er[-\s]?pack`; FR/NL `Lot de 6` / `Doos van 12`
need the number-after form. Both shapes already exist in `_PACK_RE`'s two alternatives — extend
each with the config keyword lists rather than adding a third branch.

### 4. Tests — `tests/unit/search/test_numeric.py`

Separator normalization (the regression that matters):

- `1,5 l` → `volume_ml: 1500`; `2,5 kg` → `weight_g: 2500`; `0,33 l` → `volume_ml: 330`
- `1,5V` → `voltage_v: 1.5`; `4,1 liter` → `volume_ml: 4100`; `2,27 Kg` → `weight_g: 2270`
- `1,000 g` → `weight_g: 1000` (English thousands must still work — the guard against
  over-correcting)
- `1.000 g` with `country='de'` → `weight_g: 1000`; with `country='uk'` → unchanged
- `USB 3.0`, `Nikon 1.4` → no attribute (thousands-dot rule must not fire)
- `6x2,27kg` → `count: 6`, `weight_g: 2270`; `24 x 0,33 l` → `count: 24`, `volume_ml: 330`

Cross-locale comparison, asserting the real-world bug is gone:

- `compare_numerics(E('… Oyster Sauce 2.27kg'), E('… Oestersaus - 2,27 Kg'))` → `PASS`
- `compare_numerics(E('Coca-Cola 1.5L'), E('Coca-Cola Zero 1,5 l fles'))` → `PASS`
- `compare_numerics(E('Varta AA 1.5V'), E('Varta Batterien 1,5V AA'))` → `PASS`

Multilingual keywords:

- `24er Pack`, `Packung mit 6`, `4 stuks`, `Doos van 12`, `Lot de 6`, `Paquet de 4` → `count`
- `55 Zoll`, `27 pouces` → `screen_inch`
- `4,9% vol`, `13,5% vol` → `abv_percent`
- Negative: `Gramm` / `Kilogramm` / `stuks` alone still yield nothing (no false mass)

## Risks

Over-correcting a legitimate English thousands separator would deflate values 1000x — the
exactly-3-digits rule is the guard, and the `1,000 g` test locks it in. The thousands-dot rule
is the riskier of the two, which is why it is country-gated and unit-adjacency-gated.

Numeric remains a hard kill-gate, so any new `FAIL` on a de/fr/nl run should be spot-checked
before the change is trusted.

## Verification

1. `python -m pytest tests/unit/search/ -v` — offline, no API cost.
2. Re-extract the recorded titles: read the 1455 candidate titles from `search_db.sqlite`
   (`candidates`, `run_id='1b82f20e73ee4c4e8a7b8e388a0aed72'`), run `extract_numerics` on each
   with its task's country, and confirm the 4 known decimal-comma titles now produce correct
   values (`2,27 Kg` → 2270, not 27000). No network needed.
3. `python scripts/validate_search.py --sample 20 --budget 0` for the numeric pre-pass dump.
4. Live check on a decimal-comma market — run a small batch against `amazon.de` or `amazon.nl`
   with German/Dutch SKU names, and inspect every `numeric: fail` in `match_layer_trace` against
   its candidate title to confirm none is a separator artifact.
